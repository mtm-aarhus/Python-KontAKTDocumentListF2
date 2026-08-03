from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote

import requests

from robot_framework import reset
from robot_framework.exceptions import CaseDeleted
from oomtm import go as oomtm_go


# ----- Document title sanitization (preserved from legacy robot) -------------
_TITLE_BAD_CHARS = re.compile(r'[~#%&*{}\:\\<>?/+|\"\'\t\[\]`^@=!$();\€£¥₹]')


# ----- Deleted in KontAKT ----------------------------------------------------


def _check_gone(resp) -> None:
    """Stop cleanly if what this queue element is about was deleted in KontAKT.

    KontAKT answers HTTP 410 with ``{"deleted": "case"|"reference"|"document"}``
    when the caseworker deleted the KontAKT case, the sag/mappe or the document
    while this element waited in the queue. Not an error and not retryable, so
    the queue framework marks the element done and takes the next one.
    """
    if resp is None or resp.status_code != 410:
        return
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    if body.get("deleted"):
        raise CaseDeleted(body.get("note") or f"{body['deleted']} deleted in KontAKT")


def process(
    orchestrator_connection: OrchestratorConnection,
    queue_element: QueueElement | None = None,
    client: "reset.Client | None" = None,
) -> None:
    orchestrator_connection.log_trace("Running process.")
    if client is None:  # e.g. a manual run outside the queue framework
        client = reset.open_all(orchestrator_connection)
    payload = json.loads(queue_element.data or "{}")
    kontakt_case_id = int(payload["kontakt_case_id"])
    kontakt_ref_id = payload.get("kontakt_reference_id")
    source_case_id = str(payload["source_case_id"]).strip()

    orchestrator_connection.log_info(
        f"KontAKT case={kontakt_case_id} ref={kontakt_ref_id} GO case={source_case_id}"
    )

    _set_ref_status(orchestrator_connection, client, kontakt_case_id, kontakt_ref_id, "fetching")

    try:
        sags_title, sags_dato, documents, warnings = _fetch_go(
            orchestrator_connection, client, source_case_id)
    except Exception as exc:
        orchestrator_connection.log_info(f"GO document fetch failed: {exc!r}")
        _set_ref_status(orchestrator_connection, client, kontakt_case_id, kontakt_ref_id, "error", str(exc))
        raise

    orchestrator_connection.log_info(
        f"Fetched {len(documents)} documents from GO ({len(warnings)} warnings), "
        f"sagsdato={sags_dato} — posting to KontAKT."
    )

    import_payload = {
        "source_system": "go",
        "source_case_id": source_case_id,
        "source_case_title": sags_title,
        # The sag's own date in GO — KontAKT shows it in the applicant's
        # sagsoversigt, the way the old AktBob robot did.
        "source_case_date": sags_dato,
        "documents": documents,
        "warnings": warnings,
    }
    r = _kontakt_post(
        client,
        f"/api/v1/cases/{kontakt_case_id}/documents/import",
        import_payload,
        timeout=120,
    )
    if r.status_code not in (200, 201):
        msg = f"KontAKT import failed: HTTP {r.status_code} body={r.text[:400]!r}"
        _set_ref_status(orchestrator_connection, client, kontakt_case_id, kontakt_ref_id, "error", msg)
        raise RuntimeError(msg)

    # The import endpoint already sets ref.status = 'docs_loaded' when
    # source_case_id matches a reference — no extra status call needed.
    orchestrator_connection.log_info(f"Done. Response: {r.json()}")


# ----- Helpers ---------------------------------------------------------------


def _shorten_title(title: str) -> str:
    """Trim long titles. Matches legacy ``shorten_document_title``."""
    if title and len(title) > 99:
        return title[:95]
    return title


def _looks_like_redacted(title: str) -> bool:
    """memo / tunnel / fletteliste detection — these auto-mark Nej."""
    t = (title or "").lower()
    return ("tunnel_marking" in t) or ("memometadata" in t) or ("fletteliste" in t)


def _coerce_doc_date(raw) -> str | None:
    """Try several date formats; return ISO YYYY-MM-DD or None."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s or s.lower() in {"none", "null"}:
        return None
    if "T" in s:
        s = s.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(
                s if fmt != "%Y-%m-%dT%H:%M:%S" else str(raw), fmt
            ).date().isoformat()
        except ValueError:
            continue
    return None


def _coerce_case_date(raw) -> str | None:
    """The sag's own date as ISO ``YYYY-MM-DD``, or None.

    GO gives ``ows_Modtaget`` as "YYYY-MM-DD HH:MM:SS" and Nova gives an ISO
    timestamp, so drop anything after the day before parsing.
    """
    if not raw:
        return None
    head = str(raw).strip().replace("T", " ").split(" ")[0]
    return _coerce_doc_date(head)


# ----- GO document fetch -----------------------------------------------------


def _fetch_go(
    orchestrator_connection: OrchestratorConnection, client, sags_id: str
) -> tuple[str, str | None, list[dict], list[str]]:
    """Return (case_title, case_date, documents, warnings) for a GO case.

    Multi-call sequence (KontAKT-specific orchestration over GO's case-list
    API; not in the lib):
      1. ``/Cases/Metadata/{id}``       — sagstitel + sagsdato + SagsURL
      2. ``/Administration/GetLeftMenuCounter`` — discover views
         (UdenMapper.aspx OR IkkeJournaliseret + Journaliseret)
      3. For each view, paginate ``RenderListDataAsStream`` to collect rows
      4. For each row, look up Parents via ``oomtm.go`` to discover bilag.
    """
    session = client.go_session
    go_url = client.go_url

    # --- 1. Case metadata ---
    meta_url = f"{go_url}/_goapi/Cases/Metadata/{sags_id}"
    try:
        r = session.get(meta_url, timeout=500)
        r.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Kan ikke hente sagstitel på {sags_id}: {exc}") from exc

    meta = r.json()
    metadata_xml = meta.get("Metadata")
    if not metadata_xml:
        raise RuntimeError(f"Metadata mangler i GO-svar for {sags_id}")

    xdoc = ET.fromstring(metadata_xml)
    sags_url = xdoc.attrib.get("ows_CaseUrl") or ""
    sags_title = xdoc.attrib.get("ows_Title") or sags_id
    sags_title = _TITLE_BAD_CHARS.sub("", str(sags_title))
    sags_title = " ".join(sags_title.split())
    # The sag's own date. ows_Modtaget is "YYYY-MM-DD HH:MM:SS" — same field the
    # AktBob sagsoversigt robot read.
    sags_dato = _coerce_case_date(xdoc.attrib.get("ows_Modtaget"))

    if "cases/" not in sags_url:
        raise RuntimeError(f"GO-sag {sags_id} mangler 'cases/' i SagsURL")
    akt = sags_url.split("cases/")[1].split("/")[0]
    encoded_sags_id = sags_id.replace("-", "%2D")
    list_url = f"%27%2Fcases%2F{akt}%2F{encoded_sags_id}%2FDokumenter%27"

    # --- 2. Discover views ---
    menu_resp = session.get(
        f"{go_url}/{sags_url}/_goapi/Administration/GetLeftMenuCounter", timeout=500
    )
    menu_resp.raise_for_status()
    views_array = menu_resp.json()

    view_id = None
    ikke_journaliseret_id = None
    journaliseret_id = None
    for item in views_array:
        name = (item.get("ViewName") or "").strip()
        if name == "UdenMapper.aspx":
            view_id = item["ViewId"]
            break
        elif name.lower() == "ikkejournaliseret.aspx":
            ikke_journaliseret_id = item.get("ViewId")
            if ikke_journaliseret_id is None and item.get("LinkUrl"):
                ikke_journaliseret_id = _scrape_view_id(session, go_url, item["LinkUrl"])
        elif name == "Journaliseret.aspx":
            journaliseret_id = item.get("ViewId")
            if journaliseret_id is None and item.get("LinkUrl"):
                journaliseret_id = _scrape_view_id(session, go_url, item["LinkUrl"])

    view_ids_to_use = [view_id] if view_id else [v for v in (ikke_journaliseret_id, journaliseret_id) if v]
    if not view_ids_to_use:
        raise RuntimeError(f"Ingen brugbar visning fundet for {sags_id}")

    documents: list[dict] = []
    warnings: list[str] = []
    has_missing_date = False
    has_nul_doc = False

    # --- 3 + 4. Paginate rows, expand bilag relationships ---
    for current_view_id in view_ids_to_use:
        first_run = True
        next_href = None
        more_pages = True
        while more_pages:
            if first_run:
                url = (
                    f"{go_url}/{sags_url}/_api/web/GetList(@listUrl)/RenderListDataAsStream"
                    f"?@listUrl={list_url}&View={current_view_id}"
                )
            else:
                url = (
                    f"{go_url}/{sags_url}/_api/web/GetList(@listUrl)/RenderListDataAsStream"
                    f"?@listUrl={list_url}{(next_href or '').replace('?', '&')}"
                )
            resp = session.post(url, timeout=500)
            resp.raise_for_status()
            payload = resp.json()
            rows = payload.get("Row", []) or []
            next_href = payload.get("NextHref")
            more_pages = bool(next_href)

            for item in rows:
                dokument_url = go_url.replace("ad.", "") + quote(item.get("FileRef", ""), safe="/")
                akt_id_raw = (item.get("CaseRecordNumber") or "").replace(".", "")
                try:
                    akt_id = int(akt_id_raw) if akt_id_raw else None
                except ValueError:
                    akt_id = None
                if akt_id_raw and akt_id_raw.strip() == "0":
                    has_nul_doc = True

                dokument_dato = _coerce_doc_date(item.get("Dato"))
                if not dokument_dato:
                    has_missing_date = True

                title = item.get("Title") or ""
                if len(title) < 2:
                    title = item.get("FileLeafRef.Name", "") or title
                dok_id = str(item.get("DocID") or "").strip()
                kategori = item.get("Korrespondance")

                # Bilag relationships via oomtm.go
                bilag_til = ""
                if dok_id:
                    parents = oomtm_go.fetch_parents(session, base_url=go_url, dok_id=dok_id)
                    bilag_til = ", ".join(p for p in parents if p)

                redacted = _looks_like_redacted(title)
                documents.append({
                    "dok_id": dok_id,
                    "akt_id": akt_id,
                    "title": _shorten_title(title),
                    "doc_category": kategori,
                    "doc_date": dokument_dato,
                    "bilag_til_dok_id": bilag_til or None,
                    "bilag_index": None,
                    "link_to_doc": dokument_url,
                    "included_in_request": "Ja",
                    "grant_access": "Nej" if redacted else None,
                    "justification": "Tavshedsbelagte oplysninger - om private forhold" if redacted else None,
                })
            first_run = False

    if has_missing_date:
        warnings.append("Et eller flere dokumenter mangler dato i GO.")
    if has_nul_doc:
        warnings.append("Sagen indeholder nul-dokumenter (AktID = 0).")

    return sags_title, sags_dato, documents, warnings


def _scrape_view_id(session: requests.Session, go_url: str, link_url: str) -> str | None:
    """Fallback when GetLeftMenuCounter returns a null ViewId — scrape it from the page."""
    try:
        r = session.get(f"{go_url}{link_url}")
        m = re.search(r"_spPageContextInfo\s*=\s*({.*?});", r.text, re.DOTALL)
        if not m:
            return None
        ctx = json.loads(m.group(1))
        return (ctx.get("viewId") or "").strip("{}") or None
    except Exception:  # pylint: disable=broad-except
        return None


# ----- KontAKT API client ----------------------------------------------------


def _kontakt_post(client, path: str, payload: dict, *, timeout: int = 60) -> requests.Response:
    resp = requests.post(
        f"{client.kontakt_base}{path}",
        headers={"X-API-Key": client.kontakt_key, "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    _check_gone(resp)
    return resp


def _set_ref_status(orchestrator_connection, client, case_id: int, ref_id: int | None, status: str, message: str = "") -> None:
    if not ref_id:
        return
    try:
        _kontakt_post(
            client,
            f"/api/v1/cases/{case_id}/refs/{ref_id}/status",
            {"status": status, "message": message},
            timeout=10,
        )
    except CaseDeleted:
        raise           # the sag is gone — don't bury it in the broad except
    except Exception as exc:  # pylint: disable=broad-except
        orchestrator_connection.log_info(f"Could not update ref status to {status!r}: {exc!r}")
