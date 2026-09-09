# Python-KontAKTDocumentListF2

Henter **én F2-sags dokumentliste** og lægger den i KontAKT, så en sagsbehandler
kan gennemgå den og beslutte, hvad der udleveres.

Køstyret, ét element pr. sag på en aktindsigt. Sat i kø af KontAKT, når en sag
tilføjes til aktindsigten, og af "Genopfrisk" bagefter.

> Robotten hed `Python-KontAKTDocumentListGO` og læste GetOrganized. F2 erstatter
> GO på alle parametre. **Køen heder nu `KontAKTDocumentListF2`** - se
> "Udrulning" nederst.

## 340 linjer blev 100, og det er F2's fortjeneste

GO-udgaven skulle:

1. hente sagens metadata,
2. gætte sig frem til hvilke SharePoint-**visninger** der fandtes
   (`UdenMapper.aspx`, ellers `IkkeJournaliseret` + `Journaliseret`),
3. skrabe et view-id ud af en HTML-side, når API'et svarede `null`,
4. paginere `RenderListDataAsStream`,
5. og slå bilagsrelationer op pr. dokument.

F2 svarer på det hele med **ét kald**: `rel/structure` giver sagen med akter og
deres dokumenter. Resten af robotten er kortlægning fra F2's begreber til
KontAKTs.

Den ene ting, der koster mere end ét kald: strukturens akter har **ikke** `Type`
(Inbound/Outbound/Internal), og det er den kolonne, aktlisten kalder "Kategori".
Den står kun på aktens fulde repræsentation, og `/cases/<id>/matters` giver
*mindre* end strukturen (kun Id og Titel). Så der er ét GET pr. akt - målt til
~175 ms, altså ni sekunder for en sag med 50 akter. Det er en robot; det er fint.

## Kortlægningen

En **akt** i F2 er et stykke korrespondance - et brev, en mail, en note. Den har
ét hoveddokument (aktens egen tekst) og et vilkårligt antal bilag.

| KontAKT | F2 |
|---|---|
| `akt_id` | aktens `CaseMatterNumber` |
| `dok_id` | dokumentets `Id` |
| `title` | aktens titel (hoveddokumentet) / dokumentets titel (bilag) |
| `doc_category` | aktens `Type` → Indgående / Udgående / Internt |
| `doc_date` | aktens `LetterDate`, ellers modtaget / sendt / oprettet |
| `bilag_til_dok_id` | hoveddokumentets `Id`, for hvert bilag |
| `link_to_doc` | `f2t://document/<id>` |

To ting er værd at vide, fordi de ikke står i nogen dokumentation:

**`CaseMatterNumber` fås først, når akten er arkiveret.** Målt på 42 akter: hver
arkiveret akt har et nummer, ingen uarkiveret har et. En kladde i F2 har altså
heller ikke et aktnummer for et menneske - så `akt_id` er tom, og robotten
skriver en advarsel om hvor mange. Det er den rigtige oversættelse, ikke et hul.

**Hoveddokumentet står ikke i strukturens dokumentliste.** Det er kun nåeligt
gennem aktens egen `rel/pdf-content`, som peger på det
(`…/documents/253530/pdf-content`). Målt på tre akter, hvor hver pegede på sit
eget. Robotten lægger det først i listen, og bilagene peger på det.

## Hvad den IKKE gør længere

GO-udgaven gættede `grant_access = "Nej"` på dokumenter, hvis titel indeholdt
`tunnel_marking`, `memometadata` eller `fletteliste`. Det var
SharePoint-artefakter fra den gamle løsning, og de findes ikke i F2. Beslutningen
om aktindsigt er sagsbehandlerens, og et gæt, der ser ud som en beslutning, er
værre end et tomt felt.

## Input

| Felt | Betydning |
|---|---|
| `kontakt_case_id` | KontAKT-sagen, dokumenterne skal lægges på |
| `kontakt_reference_id` | rækken i `case_references`, hvis status skal opdateres |
| `source_case_id` | F2-sagsnummeret, fx `2026 - 559` |

Sagen slås op med `case-by-case-number`, som **ikke** går gennem søgeindekset.
Det er vigtigt her: indekset er forsinket med minutter, og en sagsbehandler, der
lige har fået et sagsnummer, søger aktindsigt i det med det samme.

## Output

`POST /api/v1/cases/{id}/documents/import` med
`{source_system: "f2", source_case_id, source_case_title, source_case_date,
documents, warnings}`. Import-endpointet sætter selv referencens status til
`docs_loaded`.

En akt, robotten ikke kan hente, koster ikke hele sagen: den bliver en advarsel,
og resten af dokumenterne kommer ind. `403` og `404` kan ikke skelnes i F2 - et
adgangsstyret system afslører ikke, at noget findes, som man ikke må se - så
beskeden dækker begge.

## Konfiguration

| | |
|---|---|
| Constant `F2Miljoe` | `test` eller `prod` |
| Constant `F2RestTestURL` / `F2RestProdURL` | F2's vært, uden `https://` |
| Credential `F2TESTRestAkt` / `F2PRODRestAkt` | F2REST-klientens id + hemmelighed |
| Credential `KontAKTAPI` | username = base URL, password = X-API-Key |

## Afhængigheder

`oomtm.f2` - en ordret kopi af `app/integrations/f2_rest.py` i KontAKT-repoet.
Ret der, og kopiér ud igen.

## Udrulning

Mappen er omdøbt, og **`QUEUE_NAME` er skiftet fra `KontAKTDocumentListGO` til
`KontAKTDocumentListF2`.** Det er ikke kun en kodeændring:

1. git-remoten i `.git/config` peger stadig på det gamle repositorienavn.
2. OO-processen og dens udløser skal pege på den nye kø.
3. Køelementer, der ligger i den gamle kø, bliver ikke læst af nogen.

## Uafklaret

**API-brugeren kan kun finde sager, den selv står på.** Adgang til at *finde* en
sag = adgang til mindst én af dens akter, og robotten er ikke part på Teknik og
Miljøs sager. Det er den vigtigste af de rettigheder, der er spurgt cBrain om -
se `F2-SPOERGSMAAL-TIL-CBRAIN.md`, del 1, punkt 1. Indtil den er på plads kan
robotten kun hente dokumentlister for de sager, den har adgang til.
