jlab_order_queue
=================

## What this plugin does

Limits a physician to **one ROUTINE JLab lab order per patient per local
calendar day**. If a patient has more than one routine lab order placed on
the same day, the first one is immediately available to sign; the rest are
queued in FIFO order and become available on successive days. STAT and
URGENT orders always bypass the queue entirely (today, every order is
treated as ROUTINE — see "Priority" below).

**The physician always performs the actual signature.** This plugin never
signs, cancels, or deletes a clinical order. The only thing it ever does to
the order itself is block or allow its own `sign_action` button in the
Canvas UI, via a documented Command Validation effect — see "How deferral
actually works" below.

See `DISCOVERY.md` (project root) for the full investigation this design is
based on, with citations to every SDK doc page relied on.

## Architecture

```
LAB_ORDER_COMMAND events (origination, validation, commit, update, delete, enter-in-error)
        │
        ▼
handlers/lab_order_queue.py (LabOrderQueueHandler)
        │
        ├─► logic/queue.py            pure decision logic (no Canvas imports; see below)
        │
        └─► models/ (JLabOrderQueueEntry, a CustomModel)   persistence

handlers/release_task.py (ReleaseDeferredOrders, a CronTask, every 5 min)
        │
        └─► sync.py (recompute_and_persist) ──► logic/queue.py / models

apps/queue_app.py (GlobalQueueApp, PatientQueueApp)
        │
        └─► LaunchModalEffect ──► api/queue_api.py (QueueAPI, SimpleAPI)
                                        │
                                        └─► models/ (read-only, for the UI table)
```

- **`logic/queue.py`** holds every business rule in this spec (one order per
  patient per day, FIFO, STAT/URGENT bypass, idempotent recomputation,
  cancellation pulling later orders forward) as pure functions over plain
  dataclasses — no Django, no Canvas SDK. This is deliberate: it is the
  highest-risk part of the plugin to get wrong, and it is the one part
  that's fully unit-testable without a database or a mocked event bus (see
  `tests/logic/test_queue.py`, 20 tests, zero mocks).
- **`sync.py`** is the only bridge between that pure logic and
  `JLabOrderQueueEntry` rows. Both the real-time handler and the CronTask go
  through it, so they can't drift out of sync with each other on how a row
  maps to the logic layer's `QueuedOrder`/`RecomputeResult`.
- **`models/`** is a `CustomModel` (a real Django-backed table with typed,
  indexed columns), not an `AttributeHub` or Command Metadata — this is
  structured, relational data needing compound filtering and sorting
  (DISCOVERY.md §6 explains why, citing Canvas's own custom-data design
  guide).
- **`handlers/lab_order_queue.py`** is the real-time side: it reacts to a
  Lab Order command being staged, validated (rendered), committed, edited,
  deleted, or entered-in-error.
- **`handlers/release_task.py`** is the scheduled side: every 5 minutes it
  re-evaluates every patient who has a `DEFERRED` order, which is what
  catches a deferred order's day simply arriving with no other Canvas event
  to trigger a recheck (local-midnight rollover).
- **`apps/` + `api/`** are the UI: a `global`-scope app-drawer application
  and a `patient_specific` one, both opening the same SimpleAPI-served page.

## How deferral actually works

A Lab Order command can be staged and signed (`sign_action`) independently
of the rest of its note — it is not the whole note's lock/sign state that
gates it. Canvas fires `LAB_ORDER_COMMAND__POST_VALIDATION` on every render
of a staged command, and a handler can return a `CommandValidationErrorEffect`
from it. Per the SDK's own documentation, this:

> "...disables the command's action buttons and the messages appear as a
> tooltip — so the command can't be committed there."

So a `DEFERRED` order's Sign button is shown **disabled, with a tooltip**
explaining why and when it becomes available. The order is never hidden —
it's fully visible in the note, the physician can see it's queued and why,
and the moment this plugin (or the scheduled release task) promotes it to
`READY`/`RELEASED`, the very next render of that command gets no validation
error and the button re-enables itself. There is no separate "unblock"
effect — the absence of a block *is* the unblock.

This is why the plugin can honestly claim it never signs anything: it only
ever toggles whether a button Canvas already renders is clickable.

**Documented limitation:** this validation-error block only gates signing
**through the Canvas UI**. A `.commit()` called directly through the SDK's
`commands` module is not blocked by it. This plugin never calls `.commit()`
on a lab order itself, so this doesn't weaken its own enforcement — but if
some other integration ever signs lab orders programmatically, it would
bypass this queue.

## Queue states

`READY` → the very first routine order for this patient today; immediately
signable. `DEFERRED` → waiting for a later day. `RELEASED` → was deferred,
and has now reached its eligible day (promoted by the real-time handler or
the CronTask). `SIGNED` → the physician signed it. `CANCELLED` → the order
was deleted while staged, or entered-in-error after being signed. `READY`
and `RELEASED` are functionally identical (Sign is enabled) — they're kept
distinct only so the UI/audit trail can show *how* an order became
available.

## Priority (STAT/URGENT)

**The installed Canvas SDK's Lab Order command has no `priority` field at
all** — confirmed absent from the command's own parameters, from its event
context, and from the FHIR `ServiceRequest` resource. (`ImagingOrderCommand`
and `ReferCommand` do have a `Priority` enum; Lab Order does not.) Per the
confirmed scope decision, every JLab lab order is treated as `ROUTINE`.

The bypass logic (`logic.queue.is_bypass_priority`, `STAT`/`URGENT`
constants) is implemented and tested anyway — it costs nothing to keep, and
if a priority source is ever added (e.g. a Command Metadata field filled in
at order time), it takes effect with no change to the decision logic, only
to whatever feeds `QueuedOrder.priority`.

## Timezone / business date

Every business-date calculation uses `self.environment["INSTALLATION_TIME_ZONE"]`
(a documented `BaseHandler`/`CronTask` accessor — the instance's configured
IANA zone name), fed into `zoneinfo.ZoneInfo` and `logic.queue.business_date()`.
UTC is used only as a defensive fallback if that key is ever missing or
unparseable — see `config.instance_timezone()`.

## Authorization

Per the confirmed policy, **any logged-in Canvas staff session** can see
the full queue — there is no per-provider or per-care-team filtering. The
API is gated with `StaffSessionAuthMixin`, which rejects anonymous and
patient-portal sessions but makes no further assertion about which staff
member is asking. See TEST_PLAN.md scenarios 15–16 for what this does and
does not guarantee.

## Row navigation

Each row has two separate actions:

- **Clicking the row** switches the page (client-side, within the same app)
  into the per-patient view that `PatientQueueApp` itself opens into.
- **The "Chart" button** navigates the whole browser tab to that patient's
  real chart in Canvas: `window.top.location = "/patient/<id>"`. This is a
  documented URL (`docs.canvasmedical.com/api/patient/`, the Patient create
  endpoint's response description: *"the patient record can be viewed in
  Canvas at `https://<instance>.canvasmedical.com/patient/<id>`"*) —
  confirmed live against jlab-dev. An earlier pass at this (see
  DISCOVERY.md) concluded no such navigation existed; that was wrong — it
  had only searched the SDK handler docs, not the FHIR API docs, for it.

## Known limitations (documented, not hidden)

- The CronTask's "confirm the order is still valid and unsigned" step
  relies on the real-time handler having already kept each row's
  `queue_state` in sync with the command's actual state. It does not
  independently re-read the live `Command.state` field, because the exact
  string values that field takes beyond `"committed"` (confirmed in the
  SDK docs) aren't pinned down in the bundled documentation, and this
  plugin does not invent undocumented values. Verify end-to-end per
  TEST_PLAN.md before relying on this in production.
- `_resolve_ordering_provider_dbid` (in `handlers/lab_order_queue.py`)
  parses the event context's `ordering_provider` dict defensively (tries
  several common key names) because its exact shape isn't pinned down in
  the bundled docs either. It is display-only data — the authorization
  policy doesn't depend on it — and failing to resolve it never blocks
  anything. Confirm the real shape against a live instance during UAT.
- Provider and date filters are implemented in the `/queue/data` API
  (`provider_id`, `date` query params) but are not yet wired into UI filter
  controls — only the queue-state filter buttons are. The status filter
  covers the spec's core requirement; the other two are available to a
  future UI pass or direct API consumer without any backend change.

## Development

```bash
uv sync
uv run pytest --cov=jlab_order_queue --cov-report=term-missing --cov-branch
uv run mypy jlab_order_queue
```

### CANVAS_MANIFEST

The CANVAS_MANIFEST.json is used when installing your plugin. Please ensure
it gets updated if you add, remove, or rename file or class names.
