lab_billing_lookup
==================

A read-only Canvas plugin that exposes two pieces of lab billing data the FHIR
API does not carry. It exists so `billing-poller/` (a local, non-plugin service)
can finish a billing packet it otherwise cannot complete.

## Why this plugin exists

Probing a live Canvas instance showed both facts are unreachable over HTTP:

| Needed | Where it actually lives | Why FHIR can't give it |
|---|---|---|
| CPT code for a test | `LabPartnerTest.cpt_code` | No catalog/definition/charge resource is exposed (40 resource types, none of them). `Claim.item` is empty until a claim is coded; `Procedure` is unpopulated; `ServiceRequest.code` carries only the lab partner's order code. |
| Which order produced a result | `DiagnosticReport.lab` → `LabReport.tests` → `LabTest.order` | Canvas does not populate `DiagnosticReport.basedOn`, so a FHIR client has to guess. |

Both are ordinary SDK model reads — they just require running inside Canvas.

## Endpoints

Base path: `/plugin-io/api/lab_billing_lookup/billing`

Authentication is an API key in the `Authorization` header, compared against the
`BILLING_LOOKUP_API_KEY` secret with `hmac.compare_digest`. An unset secret
**fails closed** — every request is rejected rather than waved through.

### `GET /compendium`

Every orderable test with its CPT code. The poller caches this so a full sweep
doesn't issue one lookup per test.

```json
{
  "count": 412,
  "tests": [
    {"order_code": "100002", "order_name": "Core Panel", "cpt_code": "80053", "lab_partner": "Labcorp"}
  ]
}
```

### `GET /report/<diagnostic_report_id>`

The exact order(s) behind a diagnostic report, with CPT per test. The id is the
FHIR `DiagnosticReport` id, so the poller can call this with the id it already
has.

```json
{
  "diagnostic_report_id": "0c2c6400-...",
  "lab_report_id": "...",
  "requisition_number": "REQ-9",
  "orders": [
    {
      "lab_order_id": "...",
      "requisition_number": "REQ-9",
      "lab_partner": "Labcorp",
      "date_ordered": "2026-08-01T14:00:00+00:00",
      "is_patient_bill": false,
      "tests": [
        {"name": "Core Panel", "order_code": "100002",
         "cpt_code": "80053", "compendium_name": "Core Panel"}
      ]
    }
  ]
}
```

`orders` is a list because one Lab Results Review can cover results from more
than one order; each test is grouped under the order it came from.

## What it deliberately does not return

No patient demographics, no lab values, no clinical content — only billing codes
and identifiers. The poller already reads demographics over FHIR under its own
OAuth client, so putting PHI here would widen what a leaked API key exposes for
no benefit.

## Configuration

| Secret | Purpose |
|---|---|
| `BILLING_LOOKUP_API_KEY` | Shared secret the poller sends in `Authorization`. Generate a long random value. |

Set it in the Canvas UI under the plugin's configuration after install.

## CPT resolution

`LabTest.ontology_test_code` (on the order) joins to `LabPartnerTest.order_code`
(in the compendium), which carries `cpt_code`. The index is keyed on
**(lab partner, order code)** because two partners can reuse the same order code
for different tests; a partner-less fallback still resolves when the order's
partner string doesn't exactly match the compendium's.

`cpt_code` is nullable in Canvas, so a test with no CPT configured returns
`"cpt_code": null` rather than being omitted — the poller reports that as a gap.

## Development

```bash
uv run pytest --cov=lab_billing_lookup --cov-report=term-missing --cov-branch
uv run mypy lab_billing_lookup
```

The SDK models need a live Canvas database, so the tests patch the model
managers and exercise the handler's own logic: the CPT index, the partner
fallback, order grouping, and auth (including the fail-closed path).
