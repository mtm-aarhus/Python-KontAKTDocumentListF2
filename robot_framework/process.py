"""KontAKT: hent én F2-sags dokumentliste og læg den i KontAKT.

Queue-driven, ét køelement pr. sag på en aktindsigt. Robotten slår sagen op i
F2, læser dens struktur og sender dokumentlisten til KontAKTs import-endpoint.

HVOR MEGET DEN HER FIL BLEV MINDRE, OG HVORFOR
    GO-udgaven var 340 linjer. Den skulle hente sagens metadata, gætte sig frem
    til hvilke SharePoint-VISNINGER der fandtes (``UdenMapper.aspx``, ellers
    ``IkkeJournaliseret`` + ``Journaliseret``), skrabe et view-id ud af en
    HTML-side når API'et svarede null, paginere ``RenderListDataAsStream`` og
    slå bilagsrelationer op pr. dokument.

    F2 kan svare på det samme med ét kald: ``rel/structure`` giver hele sagen -
    akter med deres felter og dokumenter med Id, Titel, filtype og størrelse.
    Resten af filen er derfor kortlægning: F2's begreber over i KontAKTs.

    Den ene ting, der koster mere end ét kald: strukturens akter har IKKE
    ``Type`` (Inbound/Outbound/Internal), og det er den kolonne, aktlisten
    kalder "Kategori". Den står kun på aktens fulde repræsentation, og
    ``/cases/<id>/matters`` giver MINDRE end strukturen (kun Id og Titel), så
    der er ét GET pr. akt. Målt til ~175 ms, så en sag med 50 akter koster ni
    sekunder - det er en robot, og det er fint.

F2'S MODEL OVER I KONTAKTS
    En AKT er et stykke korrespondance - et brev, en mail, en note. Den har
    ét hoveddokument (aktens egen tekst) og et vilkårligt antal bilag.

        akt_id            <- aktens CaseMatterNumber
        dok_id            <- dokumentets Id
        doc_category      <- aktens Type
        doc_date          <- aktens LetterDate (ellers modtaget/sendt/oprettet)
        bilag_til_dok_id  <- hoveddokumentets Id, for hvert bilag
        link_to_doc       <- dokumentets alternate (f2t://document/<id>)

    ``CaseMatterNumber`` fås FØRST når akten er arkiveret. Målt på 42 akter:
    hver arkiveret akt har et nummer, ingen uarkiveret har et. En kladde i F2
    har altså heller ikke et aktnummer for et menneske, så ``akt_id`` er tom -
    og det er den rigtige oversættelse, ikke et hul.

    Hoveddokumentet står IKKE i strukturens dokumentliste. Det er kun nåeligt
    gennem aktens egen ``rel/pdf-content``, som peger på det. Så det hentes
    derfra og lægges først, og bilagene peger på det.

OO config:
    Constant   F2Miljoe        "test" eller "prod"
    Constant   F2RestTestURL   F2's vaert, uden https://
    Constant   F2RestProdURL   ditto
    Credential F2TESTRestAkt   F2REST-klientens id + hemmelighed
    Credential F2PRODRestAkt   ditto
    Credential KontAKTAPI      username = base URL, password = X-API-Key
"""
from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement
import json
import re

import requests

from robot_framework import reset
from robot_framework.exceptions import CaseDeleted
from oomtm import f2 as oomtm_f2

# Tegn, KontAKT ikke skal have i en dokumenttitel. Bevaret fra GO-udgaven: de
# gav problemer i filnavne og i regneark, og en titel fra et ESDH kan indeholde
# hvad som helst.
_TITEL_BAD = re.compile(r'[~#%&*{}\:\\<>?/+|\"\'\t\[\]`^@=!$();\€£¥₹]')

# F2's akttyper over i den tekst, aktlisten viser i "Kategori"-kolonnen. Samme
# ord som GO brugte i sit Korrespondance-felt, så aktlisten ser ud som før.
KATEGORI = {"Inbound": "Indgående", "Outbound": "Udgående",
            "Internal": "Internt"}


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
        f"KontAKT case={kontakt_case_id} ref={kontakt_ref_id} F2 case={source_case_id}"
    )

    _set_ref_status(orchestrator_connection, client, kontakt_case_id,
                    kontakt_ref_id, "fetching")

    try:
        titel, dato, dokumenter, advarsler = _hent_f2(
            orchestrator_connection, client, source_case_id)
    except Exception as exc:
        orchestrator_connection.log_info(f"F2 document fetch failed: {exc!r}")
        _set_ref_status(orchestrator_connection, client, kontakt_case_id,
                        kontakt_ref_id, "error", str(exc))
        raise

    orchestrator_connection.log_info(
        f"Fetched {len(dokumenter)} documents from F2 ({len(advarsler)} warnings), "
        f"sagsdato={dato} — posting to KontAKT."
    )

    r = _kontakt_post(
        client,
        f"/api/v1/cases/{kontakt_case_id}/documents/import",
        {
            "source_system": "f2",
            "source_case_id": source_case_id,
            "source_case_title": titel,
            # Sagens egen dato - KontAKT viser den i ansøgerens sagsoversigt.
            "source_case_date": dato,
            "documents": dokumenter,
            "warnings": advarsler,
        },
        timeout=120,
    )
    if r.status_code not in (200, 201):
        msg = f"KontAKT import failed: HTTP {r.status_code} body={r.text[:400]!r}"
        _set_ref_status(orchestrator_connection, client, kontakt_case_id,
                        kontakt_ref_id, "error", msg)
        raise RuntimeError(msg)

    # The import endpoint already sets ref.status = 'docs_loaded' when
    # source_case_id matches a reference — no extra status call needed.
    orchestrator_connection.log_info(f"Done. Response: {r.json()}")


# ----- F2 document fetch -----------------------------------------------------


def _hent_f2(oc, client, sagsnummer: str):
    """(sagstitel, sagsdato, dokumenter, advarsler) for én F2-sag.

    ``case_by_number`` går IKKE gennem søgeindekset, så en sag, der blev
    oprettet for et minut siden, kan findes. Det er vigtigt her: en
    sagsbehandler, der lige har fået sagsnummeret, søger aktindsigt i den med
    det samme.
    """
    f2 = client.f2
    sag = f2.case_by_number(sagsnummer)
    if sag is None:
        # 403 og 404 kan ikke skelnes i F2 - et adgangsstyret system afslører
        # ikke, at noget FINDES, som man ikke må se. Så beskeden må dække begge.
        raise RuntimeError(
            f"Sagen {sagsnummer!r} kan ikke hentes fra F2. Den findes ikke, "
            f"eller API-brugeren har ikke adgang til nogen af dens akter.")

    titel = _ren_titel(oomtm_f2.text(sag, "Title")) or sagsnummer
    sags_dato = _dato(oomtm_f2.text(sag, "CreatedDate"))

    dokumenter: list[dict] = []
    advarsler: list[str] = []
    mangler_dato = False
    uden_aktnummer = 0

    akter = oomtm_f2.matters(f2.structure(sag))
    oc.log_info(f"{sagsnummer}: {len(akter)} akter i F2.")
    for kort in akter:
        try:
            akt = f2.get(oomtm_f2.links(kort)["self"])
        except oomtm_f2.F2Error as exc:
            # Én utilgængelig akt må ikke koste hele sagen: sagsbehandleren skal
            # kunne se de dokumenter, der ER adgang til, og få det at vide.
            oc.log_info(f"akt {oomtm_f2.text(kort, 'Id')} kunne ikke hentes: "
                        f"{exc.status}")
            advarsler.append(
                f"Akt {oomtm_f2.text(kort, 'Title') or oomtm_f2.text(kort, 'Id')} "
                f"kunne ikke hentes fra F2 (adgang eller sletning).")
            continue

        akt_nr = f2.matter_number(akt)
        if akt_nr is None:
            uden_aktnummer += 1
        kategori = KATEGORI.get(oomtm_f2.text(akt, "Type"), "")
        akt_dato = _akt_dato(akt)
        if not akt_dato:
            mangler_dato = True

        # Hoveddokumentet først: aktens egen tekst. Den står ikke i strukturens
        # dokumentliste, kun bag aktens rel/pdf-content.
        hoved_id = _hoveddokument_id(akt)
        if hoved_id:
            dokumenter.append(_raekke(
                dok_id=hoved_id, akt_id=akt_nr,
                titel=_ren_titel(oomtm_f2.text(akt, "Title")) or f"Akt {akt_nr or ''}".strip(),
                kategori=kategori, dato=akt_dato, bilag_til=None))

        for dok in oomtm_f2.documents(kort):
            dok_id = oomtm_f2.text(dok, "Id")
            if not dok_id or dok_id == hoved_id:
                continue
            dokumenter.append(_raekke(
                dok_id=dok_id, akt_id=akt_nr,
                titel=_ren_titel(oomtm_f2.text(dok, "Title")) or dok_id,
                kategori=kategori, dato=akt_dato,
                # Bilag hænger på aktens hoveddokument. Er der intet
                # hoveddokument, er der ikke noget at hænge dem på, og så står
                # de for sig - hellere det end en henvisning, der peger i luften.
                bilag_til=hoved_id or None))

    if mangler_dato:
        advarsler.append("En eller flere akter mangler dato i F2.")
    if uden_aktnummer:
        advarsler.append(
            f"{uden_aktnummer} akt(er) er ikke arkiveret i F2 og har derfor "
            f"intet aktnummer endnu.")
    if not dokumenter:
        advarsler.append("Sagen har ingen dokumenter, API-brugeren kan se.")
    return titel, sags_dato, dokumenter, advarsler


def _raekke(*, dok_id, akt_id, titel, kategori, dato, bilag_til) -> dict:
    """Én række, som KontAKTs import-endpoint vil have den.

    ``grant_access`` sættes IKKE. GO-udgaven gættede "Nej" på titler, der lignede
    overstregningsfiler (``tunnel_marking``, ``memometadata``, ``fletteliste``) -
    det var SharePoint-artefakter fra den gamle løsning, og de findes ikke i F2.
    Beslutningen om aktindsigt er sagsbehandlerens, og et gæt, der ser ud som en
    beslutning, er værre end et tomt felt.
    """
    return {
        "dok_id": str(dok_id),
        "akt_id": akt_id,
        "title": _kort_titel(titel),
        "doc_category": kategori or None,
        "doc_date": dato,
        "bilag_til_dok_id": bilag_til,
        "bilag_index": None,
        "link_to_doc": f"f2t://document/{dok_id}",
        "included_in_request": "Ja",
        "grant_access": None,
        "justification": None,
    }


def _hoveddokument_id(akt) -> str:
    """Aktens eget dokument - brevet, notatet, mailteksten.

    F2 oplyser det ikke som et felt. Aktens ``rel/pdf-content`` peger på det
    (``…/documents/253530/pdf-content``), og det er den eneste vej dertil vi har
    fundet. Målt på tre akter, hvor hver pegede på sit eget.
    """
    url = oomtm_f2.links(akt).get(oomtm_f2.REL + "pdf-content", "")
    if "/documents/" not in url:
        return ""
    rest = url.split("/documents/", 1)[1]
    kandidat = rest.split("/", 1)[0].split("?", 1)[0]
    return kandidat if kandidat.isdigit() else ""


def _akt_dato(akt) -> str | None:
    """Aktens dato, i den rækkefølge et menneske ville vælge den.

    ``LetterDate`` er brevdatoen og står på både ind- og udgående; den er den
    rigtige på aktlisten. Falder tilbage på modtaget/sendt og til sidst på, hvornår
    akten blev oprettet - så kolonnen er tom så sjældent som muligt.
    """
    for felt in ("LetterDate", "ReceivedDate", "SentDate", "CreatedDate"):
        d = _dato(oomtm_f2.text(akt, felt))
        if d:
            return d
    return None


def _dato(raw) -> str | None:
    """F2's "2026-09-07T15:24:55.773" -> "2026-09-07"."""
    s = str(raw or "").strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def _ren_titel(titel: str) -> str:
    return " ".join(_TITEL_BAD.sub("", str(titel or "")).split())


def _kort_titel(titel: str) -> str:
    """Klip lange titler. Samme grænse som GO-udgaven, fordi det er KontAKTs
    kolonne, der sætter den - ikke kildesystemets."""
    t = str(titel or "")
    return t[:95] if len(t) > 99 else t


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


def _set_ref_status(orchestrator_connection, client, case_id: int, ref_id: int | None,
                    status: str, message: str = "") -> None:
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
