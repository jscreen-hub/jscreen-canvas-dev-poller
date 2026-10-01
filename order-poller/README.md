order-poller
============

A **local** integration that pulls committed lab orders out of Canvas and writes
them as JSON files on this machine. It runs here (not inside Canvas), so it can
write directly to a local directory — no public webhook endpoint required.

This is an alternative to the `lab-order-export` Canvas plugin: the plugin
**pushes** on commit (real-time), this **pulls** on an interval (polling).

## How it works

1. **Authenticate** to Canvas via OAuth 2.0 `client_credentials` against
   `{CANVAS_BASE_URL}/auth/token/` (token cached until near expiry).
2. **Search** `GET {FHIR base}/ServiceRequest?category=http://snomed.info/sct|108252007`
   (SNOMED "Laboratory procedure") over a recent `authored` window, paging by
   explicit `_offset` (see *Pagination* below).
3. **Filter to committed orders.** Only `active` + `completed` orders are
   exported; `draft` (staged/unsigned) and `entered-in-error` are skipped. This
   matches the "doctor signs/commits the order" requirement. Canvas has no
   server-side `status` search param, so the filter is applied client-side.
4. **Dedupe** against a local processed-id store (`.state/processed_ids.json`).
   Canvas's ServiceRequest search has no `_lastUpdated` filter, so this store —
   not a timestamp cursor — is what prevents reprocessing.
5. **Enrich** each new order by reading the referenced `Patient`, `Practitioner`,
   and `Condition` resources (MRN, name, DOB, NPI, ICD-10).
6. **Write** each order to `<ORDER_OUTPUT_DIR>/<ServiceRequest id>.json`.

> **Note on granularity:** Canvas exposes each ordered test as its own
> `ServiceRequest`, so a multi-test lab order produces one file per test. Verify
> this against your data during the first run.

## Pagination (do not "simplify" this)

Canvas search results are **not stably ordered across requests**. Following the
bundle's `next` links one page at a time returns some records twice and never
returns others at all: on 2026-09-09 a 120-result query paged at the default
size of 10 collected 120 entries but only **87 distinct** orders — 33 were
invisible, including committed ones that therefore never exported.

The `next` links themselves are correct (right filters, right offsets). The
result ordering underneath them drifts between requests, and 12 sequential round
trips gave it plenty of room.

`_search_all` therefore:

1. pages by explicit `_offset` at `_count=100` (Canvas caps a page at 100
   regardless of a larger value), so there are as few round trips as possible;
2. de-duplicates by resource id;
3. re-walks, up to `_MAX_SEARCH_PASSES`, whenever the distinct count is short of
   the bundle's `total`;
4. stops after `_MAX_PAGES` regardless, so a server reporting no `total` cannot
   spin the poller forever.

Reverting to a `next`-link walk silently loses orders. There is no error and the
log still reports the full `total`, which is what made this hard to spot.

## Configuration

Reads from the repo-root `.env` (already present):

| Variable                 | Required | Default                                            |
|--------------------------|----------|----------------------------------------------------|
| `CANVAS_BASE_URL`        | yes      | e.g. `https://jlab-dev.canvasmedical.com`          |
| `CANVAS_CLIENT_ID`       | yes      | OAuth client id                                    |
| `CANVAS_CLIENT_SECRET`   | yes      | OAuth client secret                                |
| `CANVAS_SCOPE`           | no       | e.g. `system/*.read`                               |
| `CANVAS_FHIR_BASE_URL`   | no       | derived as `fumage-<host>` from `CANVAS_BASE_URL`  |
| `ORDER_OUTPUT_DIR`       | no       | `C:\Projects\canvas-orders\orders`                 |
| `ORDER_STATE_FILE`       | no       | `C:\Projects\canvas-orders\order-poller\.state\processed_ids.json` |
| `ORDER_LOOKBACK_DAYS`    | no       | `2` (how many days of `authored` to scan)          |
| `ORDER_POLL_INTERVAL`    | no       | `300` (seconds between cycles in loop mode)        |

The FHIR base URL is derived by inserting the Canvas `fumage-` prefix:
`https://jlab-dev.canvasmedical.com` → `https://fumage-jlab-dev.canvasmedical.com`.

### API client scope

The OAuth client must be able to read `ServiceRequest`, `Patient`, `Practitioner`,
and `Condition` (a `system/*.read` backend client covers all four). Create/manage
the client at `{CANVAS_BASE_URL}/auth/applications/`.

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
`ORDER_STATE_FILE`. Delete the state file to force a full re-export of the
current `authored` window.

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
    "id": "<ServiceRequest id>",
    "status": "active",
    "authored_on": "<authoredOn>",
    "tests": [ { "system": "http://loinc.org", "code": "...", "display": "..." } ]
  },
  "patient": { "id": "...", "mrn": "...", "name": "...", "date_of_birth": "YYYY-MM-DD" },
  "ordering_provider": { "id": "...", "name": "...", "npi": "..." },
  "diagnoses": [ { "code": "<ICD-10>", "display": "..." } ]
}
```

`patient` / `ordering_provider` are `null` and `diagnoses` / `tests` empty when the
source order lacks that data.
