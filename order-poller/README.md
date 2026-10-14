order-poller
============

A **local** integration that pulls committed lab orders out of Canvas and writes
them as JSON files on this machine. It runs here (not inside Canvas), so it can
write directly to a local directory — no public webhook endpoint required.

This is an alternative to the `lab-order-export` Canvas plugin: the plugin
**pushes** on commit (real-time), this **pulls** on an interval (polling).

## How it works

1. **Read signed lab orders** from the `../lab-billing-lookup` Canvas plugin's
   `/plugin-io/api/lab_billing_lookup/billing/lab-orders` endpoint
   (`LabOrderSource.signed_orders()`), authenticated with a plugin API key
   (`BILLING_LOOKUP_URL` / `BILLING_LOOKUP_API_KEY`) — **not** FHIR
   `ServiceRequest`. See `order_poller/source.py`'s module docstring for why:
   `ServiceRequest.id` and the Canvas `LabOrder.id` the plugin reads directly
   are separate identifier spaces with no reliable join (62% of committed
   ServiceRequests matched more than one LabOrder on test codes alone on this
   instance), and the plugin's own ORM access to `LabOrder` resolves the
   vendor id, requisition number, CPT codes, and ICD-10 diagnoses from the
   same row instead of guessing across two.
2. **Dedupe** against a local processed-id store (`.state/processed_ids.json`)
   — the plugin returns every currently-signed order each call, so this store
   (not a timestamp cursor) is what prevents reprocessing.
3. **Attach Finch genetic/family history**, best-effort, for the order's
   patient (see below) — reads `QuestionnaireResponse` and `DocumentReference`
   directly over FHIR, using the same OAuth credentials as before.
4. **Write** each order to `<ORDER_OUTPUT_DIR>/<LabOrder id>.json`.

> **Cutover note:** this poller used to read FHIR `ServiceRequest` directly,
> with its own `_offset`-based pagination fix for Canvas's unstable `next`-link
> search ordering. That approach, and the pagination workaround, are gone along
> with `canvas_client.py`/`mapper.py` — `--seed` exists specifically to migrate
> the dedupe store from `ServiceRequest` ids to `LabOrder` ids without
> re-delivering every order downstream on the first run after the cutover.
> `billing_poller.canvas_client.CanvasClient.search()` still carries that same
> pagination fix and is the reference if a future FHIR-reading feature needs it
> again (as `genetic_history.py`'s own client now does, for a different pair of
> resources).

## Finch genetic/family-history enrichment

JLab's genetic-test purchase flow has patients complete a third-party
("Finch") intake, which Canvas stores as `QuestionnaireResponse` resources —
one **"Finch Cancer History"** response per reported cancer occurrence
(personal or per relative), one **"Finch Genetic Assessment"** covering
reproductive/surgical/testing history, and one **"Finch Genetic Assessment
Results"** with NCCN per-guideline eligibility and Tyrer-Cuzick breast-cancer
risk scores — plus PDF `DocumentReference`s under Medical Documents (a
complete record, a pedigree, a clinical summary, and a letter of medical
necessity, as of this writing; see below for why that list isn't hardcoded).
Confirmed live on jlab-dev against test patient
`07851fbbbe90498abac0a97597d9da3f`.

Every lab order record for a patient gets a `genetic_history` block attached
(by patient id, not by matching the order's own note/encounter — a Finch
intake is a one-time event per patient, not tied to any single order, and
Canvas's `QuestionnaireResponse` note-id extension doesn't necessarily point
at the same note as any specific order anyway). This means the same block is
duplicated across every order a patient has, which was a deliberate choice —
see `order_poller/genetic_history.py`'s module docstring.

This is optional, best-effort enrichment: a fetch failure for one patient
logs and leaves `genetic_history: null` on that order rather than failing the
record or the poll cycle, the same way `lab_billing_lookup` CPT lookups
degrade in `billing-poller`. It reuses the FHIR OAuth credentials `Settings`
already carries (unused for order data itself since the cutover above) — no
new plugin, deploy, or credentials needed.

```json
{
  "event": "lab_order_polled",
  "captured_at": "...",
  "order": { "...": "..." },
  "patient": { "...": "..." },
  "ordering_provider": { "...": "..." },
  "diagnoses": [ "..." ],
  "genetic_history": {
    "cancer_history": [
      {
        "history_record_kind": "Cancer occurrence",
        "personal_or_family_history": "Personal",
        "reported_cancer_type_finch_glossary": "Ampulla of Vater",
        "...": "one dict per reported occurrence; field set varies -- a family occurrence also carries relationship/family_side, a personal one doesn't"
      }
    ],
    "genetic_assessment": { "personal_history_of_cancer_patient_reported": "Yes", "...": "..." },
    "assessment_results": { "nccn_breast_guidelines_eligibility": "Eligible", "...": "..." },
    "documents": [
      { "description": "Finch Pedigree", "content_type": "application/pdf", "url": "https://fumage-.../DocumentReference/<id>/files/content" }
    ]
  }
}
```

**Field names are generated, not hand-mapped.** Each Finch questionnaire
carries 15-40 questions, and Finch can add or rename questions in a future
version; `genetic_history.py` flattens `QuestionnaireResponse.item` into
`{slugified question text: answer}` generically rather than via a hardcoded
per-field mapping, so a schema change there surfaces as a differently-named
key instead of silently dropped data. An item with no answer at all (common —
e.g. "Recorded relationship to the patient" is blank for a personal, non-family
occurrence) is omitted rather than written as `null`. The same reasoning
applies to `documents`: matched by description prefix ("Finch"), not a
hardcoded list of the 3-4 documents seen so far, so a new Finch document type
is picked up automatically rather than silently missed.

Documents are **links only, never fetched/embedded** — same reasoning as
`billing-poller`'s `patient.ccda_url`: this is the patient's whole Finch
record, not scoped to the order, and embedding every PDF into every order
record would multiply that PHI footprint across every order a patient has.

## Configuration

Reads from the repo-root `.env` (already present):

| Variable                 | Required | Default                                            |
|--------------------------|----------|----------------------------------------------------|
| `CANVAS_BASE_URL`        | yes      | e.g. `https://jlab-dev.canvasmedical.com`          |
| `CANVAS_CLIENT_ID`       | yes      | OAuth client id                                    |
| `CANVAS_CLIENT_SECRET`   | yes      | OAuth client secret                                |
| `CANVAS_SCOPE`           | no       | e.g. `system/*.read`                               |
| `CANVAS_FHIR_BASE_URL`   | no       | derived as `fumage-<host>` from `CANVAS_BASE_URL`  |
| `BILLING_LOOKUP_URL`     | no       | defaults to `{CANVAS_BASE_URL}/plugin-io/api/lab_billing_lookup/billing` |
| `BILLING_LOOKUP_API_KEY` | yes      | must match the `lab-billing-lookup` plugin's own secret |
| `ORDER_OUTPUT_DIR`       | no       | `C:\Projects\canvas-orders\orders`                 |
| `ORDER_STATE_FILE`       | no       | `C:\Projects\canvas-orders\order-poller\.state\processed_ids.json` |
| `ORDER_POLL_INTERVAL`    | no       | `300` (seconds between cycles in loop mode)        |

The FHIR base URL (used only by the Finch enrichment now, not for reading
orders) is derived by inserting the Canvas `fumage-` prefix:
`https://jlab-dev.canvasmedical.com` → `https://fumage-jlab-dev.canvasmedical.com`.

### API client scope

The OAuth client (`CANVAS_CLIENT_ID`/`CANVAS_CLIENT_SECRET`) must be able to
read `QuestionnaireResponse` and `DocumentReference` for the Finch enrichment
(a `system/*.read` backend client covers both). Create/manage the client at
`{CANVAS_BASE_URL}/auth/applications/`. The lab order feed itself
authenticates separately, with `BILLING_LOOKUP_API_KEY` against the
`lab-billing-lookup` plugin.

## Running

```bash
cd order-poller
uv sync

# One cycle (good for a manual run or Windows Task Scheduler / cron):
uv run python -m order_poller --once

# Continuous loop, every ORDER_POLL_INTERVAL seconds (Ctrl+C to stop):
uv run python -m order_poller
```

Output files land in `ORDER_OUTPUT_DIR`; already-processed ids are tracked in
`ORDER_STATE_FILE`.

### `--seed`: cutting over from the old ServiceRequest-keyed store

Pass `--seed` **once**, after upgrading past the `lab_billing_lookup` cutover,
to mark every currently-signed order as already-processed without writing any
files. The dedupe store held `ServiceRequest` ids before the cutover;
`LabOrder` ids are a different identifier space, so without this step every
existing order looks brand new and gets re-delivered downstream on the first
post-cutover run.

## Development

```bash
uv run pytest --cov=order_poller --cov-report=term-missing --cov-branch
uv run mypy order_poller
```

## Order record shape

```json
{
  "event": "lab_order_polled",
  "captured_at": "<iso timestamp>",
  "order": {
    "id": "<LabOrder id>",
    "requisition_number": "...",
    "status": "active",
    "authored_on": "<date ordered>",
    "lab_partner": "...",
    "tests": [ { "system": "http://loinc.org", "code": "...", "display": "...", "cpt_code": "..." } ]
  },
  "patient": { "id": "...", "mrn": "...", "name": "...", "date_of_birth": "YYYY-MM-DD" },
  "ordering_provider": { "id": "...", "name": "...", "npi": "..." },
  "diagnoses": [ { "code": "<ICD-10>", "display": "..." } ],
  "genetic_history": { "...": "see above; null when the patient has no Finch data or the fetch failed" }
}
```

`patient` / `ordering_provider` are `null` and `diagnoses` / `tests` /
`genetic_history` empty or `null` when the source order/patient lacks that
data.
