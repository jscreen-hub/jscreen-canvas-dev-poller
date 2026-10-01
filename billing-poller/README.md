billing-poller
==============

A **local** integration that watches Canvas for lab results a provider has
**signed off**, and writes the complete billing packet for the order behind each
one as a JSON file on this machine. It runs here (not inside Canvas), so it can
write directly to a local directory — no public webhook endpoint required.

Sibling of `../order-poller`, which polls committed lab **orders**. This one
polls signed-off lab **results** and compiles what a biller needs: patient
demographics, insurance/coverage, ICD-10 diagnoses and CPT service lines —
scoped to the single order that produced the result, not everything the patient
has open.

## How it works

1. **Authenticate** to Canvas via OAuth 2.0 `client_credentials` against
   `{CANVAS_BASE_URL}/auth/token/` (token cached until near expiry).
2. **Sweep** `GET {FHIR base}/DiagnosticReport`, following pagination.
3. **Keep only signed-off results.** A report qualifies when its status is
   `final` **and** it carries an `encounter` reference whose type is
   *Lab Results Review* and whose status is not `cancelled`. Canvas attaches
   that encounter only once a provider commits the review note, so its presence
   **is** the sign-off. Everything else is left alone and re-checked next cycle.
4. **Dedupe** against a local processed-id store (`.state/processed_ids.json`).
   Only signed-off reports are recorded, so a result seen before review is
   picked up on the cycle after the provider signs it.
5. **Resolve the order** behind the result (see *Order matching* below).
6. **Gather the billing data**: patient demographics + billing address, all
   active coverages with payors ranked primary-first, ICD-10 from the matched
   order's `reasonReference` conditions, and CPT service lines from the claim
   tied to the review encounter.
7. **Write** each record to `<BILLING_OUTPUT_DIR>/<DiagnosticReport id>.json`.

## Order matching

Canvas does **not** populate `DiagnosticReport.basedOn`, so there is no id-level
link from a result back to the ServiceRequest that produced it. The link is
inferred, and every record reports how it was reached in `order_match` so a
guess is never mistaken for a fact:

| `method`          | `confidence` | Basis                                                        |
|-------------------|--------------|--------------------------------------------------------------|
| `lookup_order_code` | exact      | Order codes from the `lab-billing-lookup` plugin, resolved through Canvas's own foreign keys |
| `code`            | high         | Report and order share a coding (same system **and** code)   |
| `name`            | medium       | Report test name matches an order's test name                |
| `sole_open_order` | medium       | Exactly one committed lab order predates the result          |
| `nearest_prior`   | low          | Several candidates, none matched; took the closest preceding |
| `none`            | none         | No committed lab order predates the result; `order` is null  |

Candidates are always restricted to **committed** (`active`/`completed`) lab
orders **for the same patient** authored **on or before** the result date — that
is what keeps a patient's other orders out of the record.

Records are never dropped for failing to match. An unmatched result is still
exported with `order: null` and `no_order_match` in `data_gaps`.

> On the `jlab-dev` instance every signed-off result had either zero or exactly
> one candidate order — matching was never ambiguous. Name matching is exact on
> normalized tokens (case, punctuation and filler words like "panel"/"test" are
> ignored); a partial overlap such as *Core Panel* vs *Core Panel Extended* is
> deliberately **not** treated as a match, since a wrong order means a wrong claim.

## Configuration

Reads from the repo-root `.env`, shared with `order-poller`:

| Variable                 | Required | Default                                                              |
|--------------------------|----------|----------------------------------------------------------------------|
| `CANVAS_BASE_URL`        | yes      | e.g. `https://jlab-dev.canvasmedical.com`                            |
| `CANVAS_CLIENT_ID`       | yes      | OAuth client id                                                      |
| `CANVAS_CLIENT_SECRET`   | yes      | OAuth client secret                                                  |
| `CANVAS_SCOPE`           | no       | e.g. `system/*.read`                                                 |
| `CANVAS_FHIR_BASE_URL`   | no       | derived as `fumage-<host>` from `CANVAS_BASE_URL`                    |
| `BILLING_OUTPUT_DIR`     | no       | `C:\Projects\canvas-orders\billing`                                  |
| `BILLING_STATE_FILE`     | no       | `C:\Projects\canvas-orders\billing-poller\.state\processed_ids.json` |
| `BILLING_POLL_INTERVAL`  | no       | `300` (seconds between cycles in loop mode)                          |
| `BILLING_PAGE_SIZE`      | no       | `100` (FHIR `_count` per page)                                       |
| `BILLING_LOOKBACK_DAYS`  | no       | `0` — see below                                                      |

### Why the default is a full sweep

Canvas **ignores** `_lastUpdated` and `_sort` on `DiagnosticReport` (both return
the full set unchanged), so there is no server-side cursor for "what is new".
The only working date param is `date`, which filters `effectiveDateTime` — the
date the **specimen was collected**, not the date the provider signed off. Since
results are reviewed at a later date, a result collected in January and reviewed
in March would fall outside any short window and be missed forever.

So `BILLING_LOOKBACK_DAYS=0` (sweep every report each cycle) is the default, and
the id store — not a timestamp — is what prevents reprocessing. Set a lookback
only if report volume makes the sweep too slow, and set it generously (a value
must exceed your longest collection-to-review gap).

### API client scope

The OAuth client must read `DiagnosticReport`, `Encounter`, `Patient`,
`Practitioner`, `ServiceRequest`, `Condition`, `Coverage`, `Organization` and
`Claim` (a `system/*.read` backend client covers all of them). Create/manage the
client at `{CANVAS_BASE_URL}/auth/applications/`.

## Running

```bash
cd billing-poller
uv sync

# One cycle (Windows Task Scheduler / cron / a manual run):
uv run python -m billing_poller --once

# Continuous loop, every BILLING_POLL_INTERVAL seconds (Ctrl+C to stop):
uv run python -m billing_poller
```

Output files land in `BILLING_OUTPUT_DIR`; processed ids are tracked in
`BILLING_STATE_FILE`. Delete the state file to force a full re-export.

### Scheduled Task (this is how it runs in production)

Registered as **`CanvasLabBillingPoller`**, firing `run_billing_poller.cmd`
every 5 minutes — the same cadence and shape as `CanvasLabOrderPoller`. The
wrapper runs one `--once` cycle and appends stdout/stderr to
`.state\poller.log`.

| Setting              | Value                                                  |
|----------------------|--------------------------------------------------------|
| Trigger              | every 5 min, duration 10 years                          |
| Multiple instances   | `IgnoreNew` (a slow cycle can never stack up)           |
| Execution time limit | 10 min                                                  |
| Start when available | yes (catches up after the machine is asleep or off)     |
| Logon type           | `S4U` — runs whether or not the user is logged on, with no stored password |

A cycle takes ~8s cold and ~2s in steady state, so the 5-minute interval has
plenty of headroom.

```powershell
Get-ScheduledTaskInfo -TaskName CanvasLabBillingPoller   # LastTaskResult 0 = success
Start-ScheduledTask   -TaskName CanvasLabBillingPoller   # force a cycle now
Disable-ScheduledTask -TaskName CanvasLabBillingPoller   # pause the feed
```

`CanvasLabOrderPoller` uses `Password` logon; this one uses `S4U` so no
credential is stored. If the task ever reports `2147943726`
(`ERROR_LOGON_FAILURE`), the account has lost its batch-logon right — re-register
with `-LogonType Password` and supply the account password.

> The output directory holds PHI. `/billing/` is git-ignored at the repo root.

## Billing record shape

```json
{
  "event": "lab_billing_ready",
  "captured_at": "<iso timestamp>",
  "result": {
    "diagnostic_report_id": "...",
    "name": "Core Panel",
    "status": "final",
    "effective_date": "...",
    "issued": "...",
    "report_pdf_url": "https://fumage-.../DiagnosticReport/<id>/files/presentedForm"
  },
  "sign_off": {
    "encounter_id": "...",
    "encounter_status": "finished",
    "encounter_type": "Lab Results Review",
    "reviewed_at": "...",
    "reviewed_by": { "id": "...", "name": "...", "npi": "..." }
  },
  "order": {
    "id": "<ServiceRequest id>",
    "status": "active",
    "authored_on": "...",
    "tests": [ { "system": "http://loinc.org", "code": "...", "display": "..." } ],
    "ordering_provider": { "id": "...", "name": "...", "npi": "..." }
  },
  "order_match": { "method": "name", "confidence": "medium", "candidates_considered": 1 },
  "patient": {
    "id": "...", "mrn": "...", "name": "...", "date_of_birth": "YYYY-MM-DD",
    "gender": "female",
    "address": { "line": ["..."], "city": "...", "state": "..", "postal_code": "...", "country": "US" },
    "phone": "...",
    "ccda_url": "https://jlab-dev.canvasmedical.com/api/data-export/ccda/<patient key>?document=continuity",
    "ccda_xml": "<ClinicalDocument>...</ClinicalDocument>"
  },
  "insurance": [
    {
      "rank": 1,
      "coverage_id": "...",
      "status": "active",
      "payor": { "id": "...", "name": "Aetna" },
      "member_id": "...", "subscriber_id": "...",
      "relationship": { "code": "self", "display": "Self", "cms_code": "18" },
      "plan": "...", "group": "...",
      "period": { "start": "YYYY-MM-DD" }
    }
  ],
  "diagnoses": [ { "code": "<ICD-10>", "display": "..." } ],
  "procedures": [
    {
      "code": "80053", "display": "...", "system": "http://www.ama-assn.org/go/cpt",
      "is_cpt": true, "quantity": 1, "unit_price": 47,
      "modifiers": ["90"], "diagnosis_pointers": [1]
    }
  ],
  "claim": { "id": "...", "queue": "NeedsCodingReview", "date_of_service": "...", "diagnoses": [...] },
  "data_gaps": []
}
```

### `data_gaps`

Rather than silently emitting a thin record, the poller names what is missing:
`no_order_match`, `low_confidence_order_match`, `no_active_coverage`,
`no_icd10`, `no_cpt_codes`, `no_patient_address`, `no_ccda`. A downstream
consumer can route on this field instead of re-deriving it.

### Where CPT codes come from

CPT is tied to the **test**, in the lab partner compendium:
`LabPartnerTest.cpt_code`, joined from the ordered test's `ontology_test_code`
via `LabPartnerTest.order_code`. That is also why your orders carry codes like
`100002` in the LOINC slot — those are lab partner order codes, not LOINCs.

That model is **SDK-only**. There is no FHIR or REST route to it: the instance
exposes 40 resource types with no catalog/definition/charge resource among them,
and every plausible `/api/...` compendium path 404s. `Claim.item` (the only FHIR
home for billed codes) is empty on all 49 `jlab-dev` claims, on search *and*
individual read, across every queue including `NeedsCodingReview`; `Procedure`
is empty too.

So CPT arrives via the optional **`../lab-billing-lookup/`** Canvas plugin:

| Variable | Purpose |
|---|---|
| `BILLING_LOOKUP_URL` | Defaults to `{CANVAS_BASE_URL}/plugin-io/api/lab_billing_lookup/billing` |
| `BILLING_LOOKUP_API_KEY` | Must match the plugin's `BILLING_LOOKUP_API_KEY` secret |

**The plugin is optional and the poller degrades cleanly without it.** With no
key configured, `enabled` is False and behavior is exactly the FHIR-only feed:
heuristic order matching, `no_cpt_codes` in `data_gaps`. A mid-run failure
(unreachable host, rejected key) logs once, stops retrying, and lets the cycle
finish — a billing feed that halts because an optional enrichment is down is
worse than one that keeps flowing with a gap flag.

With the plugin installed, two things improve:

- **CPT is populated** from the compendium, as `procedures` entries tagged
  `"source": "compendium"`. A coded `Claim` still wins when one exists, since it
  reflects what was actually billed.
- **Order matching becomes exact.** The plugin resolves the result's order
  through Canvas's own foreign keys (`DiagnosticReport.lab` → `LabReport.tests`
  → `LabTest.order`), and the poller matches those authoritative order codes
  against the candidate ServiceRequests — `method: "lookup_order_code"`,
  `confidence: "exact"`. Records also gain a `lab_order` block with the real
  `lab_order_id` and requisition number. The heuristic remains as the fallback.

### `patient.ccda_url` / `patient.ccda_xml`

Canvas has no endpoint that returns a PDF of the whole chart. The closest
equivalent is a continuity-of-care **C-CDA** (XML), generated on request at:

```
GET {CANVAS_BASE_URL}/api/data-export/ccda/{patient_key}?document=continuity
```

This is a different host than the rest of the poller (the auth/instance host,
not the `fumage-` FHIR host) and a different, much wider scope than everything
else in the record: the patient's whole problem/med/allergy/encounter history,
not just the order behind this result.

Every record carries **both**:

- `patient.ccda_url` — the export link itself, built from `Patient.id` (the
  "patient key"), for provenance. It is unauthenticated by itself; fetching it
  yourself needs the same OAuth client credentials this poller uses.
- `patient.ccda_xml` — the fetched document content, verbatim, as a string.
  This is fetched once per patient per poll cycle (`CanvasClient.get_ccda`,
  cached for the cycle) and embedded directly, so a downstream consumer never
  has to make its own authenticated call to Canvas to get it.

Both are `null` when `patient` could not be resolved. `ccda_xml` is also
`null` — with `no_ccda` in `data_gaps` — when the export could not be fetched
(unreachable host, rejected token, 404, etc.); this is treated as optional
enrichment the same way the CPT compendium lookup is: a failure is logged once
and degrades to a gap flag rather than failing the record or the cycle.

**Be aware of what this means for record size and scope.** Every signed-off
lab result now carries the patient's *entire* chart history, not just the
CPT/ICD-10 data scoped to that one order — a much larger PHI footprint per
file than the rest of this feed. If a downstream consumer only needs the
order-scoped billing data most of the time, consider having it read
`ccda_xml` only when it actually needs the wider context, same as it would
for any other field.

## Development

```bash
uv run pytest --cov=billing_poller --cov-report=term-missing --cov-branch
uv run mypy billing_poller
```
