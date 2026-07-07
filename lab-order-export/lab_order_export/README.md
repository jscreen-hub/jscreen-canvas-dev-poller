lab-order-export
================

## Description

Exports every committed lab and genetic test order out of Canvas the moment a
provider signs the **Lab Order** command in a note. Each order is normalized into
a structured JSON payload and delivered to two destinations:

1. **External HTTP API** (authoritative) — a `POST` of the payload to a configured
   endpoint with bearer-token auth.
2. **Local NDJSON file** (best-effort audit/fallback) — one JSON object appended
   per line.

All committed orders are in scope — there is no filtering. Genetic panels are
placed as Lab Order commands, so they flow through the same handler with no
special handling. This plugin handles **orders only**, not results.

## How it works

- **Trigger:** `LAB_ORDER_COMMAND__POST_COMMIT` — fires on sign/commit regardless
  of how the order is later transmitted (electronic, fax, or print).
- **Handler:** `lab_order_export.handlers.order_export:OrderExportHandler`
  1. Resolves the committed `LabOrder` from the triggering command (via the
     command's note + patient).
  2. Builds the payload (`utils/payload.py`).
  3. Delivers it to the API and the audit file (`utils/delivery.py`). Each
     delivery is isolated: a failed API POST still records the order in the file,
     and neither failure crashes the handler.
  4. Returns no Canvas effect — this is a side-effect integration.

## Payload shape

```json
{
  "event": "lab_order_committed",
  "captured_at": "2026-07-07T14:03:00+00:00",
  "order": {
    "id": "<LabOrder uuid>",
    "note_id": "<note uuid>",
    "command_id": "<command uuid>",
    "date_ordered": "2026-07-07T14:02:55+00:00",
    "lab_partner": "<ontology_lab_partner>",
    "requisition_number": "<requisition_number>",
    "transmission_type": "<transmission_type>"
  },
  "patient": {
    "id": "<patient uuid>", "mrn": "<mrn>",
    "first_name": "...", "last_name": "...", "date_of_birth": "YYYY-MM-DD"
  },
  "ordering_provider": { "id": "<staff uuid>", "name": "<full_name>", "npi": "<npi>" },
  "tests": [ { "name": "...", "code": "...", "status": "..." } ],
  "diagnoses": [ { "code": "<ICD-10 code>", "display": "<condition display>" } ]
}
```

`patient` and `ordering_provider` may be `null`, and `tests` / `diagnoses` may be
empty, when the source order lacks that data. Only ICD-10 codings are emitted in
`diagnoses`.

## Configuration (plugin secrets)

Set these on the plugin configuration page after installation:

| Variable                 | Sensitive | Purpose                                             |
|--------------------------|-----------|-----------------------------------------------------|
| `ORDER_EXPORT_API_URL`   | no        | Destination endpoint for the `POST`.                |
| `ORDER_EXPORT_API_TOKEN` | yes       | Bearer token sent as `Authorization: Bearer <...>`. |
| `ORDER_EXPORT_FILE_PATH` | no        | Path for the NDJSON audit file.                     |

If a variable is unset, its corresponding delivery is skipped and logged.

> **Note:** Canvas plugin filesystem access is limited and may be ephemeral across
> deploys/restarts. The NDJSON file is a best-effort local log, not the system of
> record — the external API is authoritative.

## Development

```bash
uv run pytest --cov=lab_order_export --cov-report=term-missing --cov-branch
uv run mypy lab_order_export
```

### CANVAS_MANIFEST

The CANVAS_MANIFEST.json is used when installing your plugin. Please ensure it gets updated if you add, remove, or rename file or class names.
