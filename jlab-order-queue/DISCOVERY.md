# DISCOVERY — `jlab_order_queue`

Findings from inspecting the installed Canvas SDK docs (`cpa` plugin skills:
`canvas-sdk`, `fhir-api-client-development`, `custom-data-patterns`) and the
existing JLab plugin/integration code in this workspace
(`lab-order-export`, `lab-billing-lookup`, `order-poller`, `billing-poller`).
No Canvas SDK class, event, field, or effect below is invented — each is
cited to the doc page it came from. Where the installed SDK does **not**
expose something the spec assumes, that is called out explicitly rather than
worked around.

This document stops at discovery, per Step 1. Three open questions at the
bottom block Steps 2–9 and need your decision before implementation starts.

---

## 1. Actual JLab order type

"JLab" is this practice's Canvas instance (`jlab-dev`), not a special order
type — confirmed by grepping every existing JLab plugin/integration in this
workspace (`lab-order-export`, `lab-billing-lookup`, `order-poller`,
`billing-poller`) for `stat`/`urgent`/`routine`/`priority`: **zero hits**.
There is no existing priority convention anywhere in this codebase.

"JLab ROUTINE order" in your spec maps to: **a Lab Order command
(`LabOrderCommand`) placed in a note, excluding genetic panels placed as
referrals** — the same order type `lab-order-export` already handles
(`lab_order_export/handlers/order_export.py`, triggers on
`LAB_ORDER_COMMAND__POST_COMMIT`). Genetic panels are placed as Lab Order
commands too per that plugin's own comment, so they're in scope here as well
unless you say otherwise.

## 2. Canvas model involved

- **Command (SDK)**: `canvas_sdk.commands.LabOrderCommand` — the command a
  provider fills out in a note. Doc: `/sdk/commands/#laborder`.
- **Command (event target)**: `canvas_sdk.v1.data.command.Command` — generic
  command row; `self.event.target.id` on any `LAB_ORDER_COMMAND__*` event is
  this row's id.
- **LabOrder (SDK read model)**: `canvas_sdk.v1.data.lab.LabOrder` — the
  committed order record. Doc: `/sdk/data-labs/#laborder`. Fields: `id`,
  `patient`, `note`, `ordering_provider`, `comment`, `date_ordered`,
  `fasting_status`, `tests` (`LabTest[]`), `reports`, etc.
- **ServiceRequest (FHIR)**: what `order-poller`/`billing-poller` already
  read externally. Fields: `status`, `intent`, `category`, `code`, `subject`,
  `authoredOn`, `requester`, `reasonReference`. **No `priority` field** (see
  §5/Open Question 1).

A Lab Order command has its own staged → committed lifecycle **independent
of the surrounding note's lock/sign state** — confirmed by the command's
documented actions (`/sdk/commands/#laborder`):

| Action | Available when | Does |
|---|---|---|
| `sign_action` | staged | Signs the order: staged → committed |
| `sign_send_action` | staged | Signs + sends electronically |
| `send_action` | staged | Sends electronically (already committed) |

So "sign a lab order" is a **per-command** action, not necessarily tied to
locking the whole note. This is what makes a per-order queue workable at all.

## 3. Events we subscribe to

All under `LAB_ORDER_COMMAND__*` (`/sdk/events/`, confirmed against the
command-lifecycle event list and cross-checked against the live
`lab-order-export` handler already using one of these):

| Event | Fires | Use here |
|---|---|---|
| `LAB_ORDER_COMMAND__POST_ORIGINATE` | Order staged in a note (not yet signed) | Create/update the queue row; run the deferral decision |
| `LAB_ORDER_COMMAND__POST_VALIDATION` | Every render/edit of the staged command | Return (or clear) a block on `sign_action` based on current queue state |
| `LAB_ORDER_COMMAND__POST_COMMIT` | Physician signs | Mark queue row `SIGNED`; release the next deferred order for that patient |
| `LAB_ORDER_COMMAND__POST_UPDATE` | Staged command edited (e.g. provider changed) | Re-evaluate; handle "provider changed" case |
| `LAB_ORDER_COMMAND__PRE_DELETE` / `POST_DELETE` | Command deleted while staged | Mark `CANCELLED`; release the next in line |
| `LAB_ORDER_COMMAND__POST_ENTER_IN_ERROR` | Committed order voided | Mark `CANCELLED`; does not re-release (already consumed its slot by being signed) |

Context for all of these includes `note.uuid` and `patient.id`
(`/sdk/events/#command-lifecycle-events`). None of the `fields` payloads
include a priority value (see Open Question 1).

## 4. How physician signing currently occurs

The physician clicks the Lab Order command's `sign_action` (or
`sign_send_action`) button while the command is staged in a note they're
editing (`/sdk/commands/#laborder`). This is an **in-UI-only** action —
nothing in this workspace auto-signs lab orders today, and the spec forbids
that regardless.

## 5. Can Canvas suppress/defer presentation for signing? — **Yes, supported**

Two independent, documented mechanisms, found at
`/sdk/effect-command-validation/` and `/sdk/commands/#customizing-action-availability`:

**Primary mechanism — `CommandValidationErrorEffect` on `LAB_ORDER_COMMAND__POST_VALIDATION`.**
Quoting the doc verbatim: *"blocking a command until an external condition is
met — before the command can be committed."* This is literally our use case,
not an adaptation of one. Returning this effect:

> "the Canvas UI shows them to the user — the command's action buttons are
> disabled and the messages appear as a tooltip — so the command can't be
> committed there."

So a deferred Lab Order's `sign_action` button is **disabled, with a tooltip**
(e.g. "Another routine JLab order for this patient was released today; this
order becomes available 2026-10-02") — the order stays fully visible in the
note, satisfies "physician must always perform the actual signature" (we
never call `.commit()` ourselves), and satisfies "do not make it available
for routine physician signing today." When our CronTask (Step 4) advances the
queue row to `READY`, the next render of that command simply gets no
validation error, and the button re-enables — no separate "unblock" effect
needed.

**Documented limitation (same page):** *"`__POST_VALIDATION` only gates
committing in the Canvas UI. A `.commit()` made through the SDK commands
module is not blocked by these errors."* We never call `.commit()` ourselves,
so this doesn't weaken our own enforcement — but it does mean if some *other*
plugin or integration ever calls `LabOrderCommand(...).commit()`
programmatically on a deferred order, our block would not stop it. Worth
knowing; not a blocker.

**Secondary/defense-in-depth — `AVAILABLE_ACTIONS` on `LAB_ORDER_COMMAND__AVAILABLE_ACTIONS`.**
Can additionally remove `sign_action`/`sign_send_action` from the rendered
action list entirely via a `COMMAND_AVAILABLE_ACTIONS_RESULTS` effect
(`/sdk/commands/#customizing-action-availability`). Recommended as a second
layer for cleaner UX (hide the button rather than show it disabled) — the
validation block is the enforced mechanism either way.

**No workaround was needed or used.** We do not touch Canvas database tables
directly anywhere in this design.

## 6. Recommended architecture

**Persistence — `CustomModel`, not `AttributeHub` or Command Metadata.**
Per `/sdk/custom-data-design-considerations/`'s own decision table, our data
is "structured entities with a stable, known schema" + "relationships
between entities" + "data requiring compound filtering/sorting" — all three
point at `CustomModel`. One table, `JLabOrderQueueEntry`, proxying
`LabOrder`/`Patient`/`Staff` via `ModelExtension` (`/sdk/custom-data-custom-models/`,
`/sdk/custom-data-extending-sdk-models/`) with a `OneToOneField` to the lab
order (one queue row per order — natural idempotency key) and indexes on
`(patient, queue_state)` and `eligible_date`. Requires a `custom_data`
namespace block in `CANVAS_MANIFEST.json` (`/sdk/custom-data-quick-start/`).

**Signing control** — `LAB_ORDER_COMMAND__POST_VALIDATION` +
`CommandValidationErrorEffect`, as above.

**Scheduled release** — `CronTask` (`/sdk/handlers-crontask/`), standard
5-field cron string, `execute()` returns `list[Effect]`.

**UI** — `Application` with `scope: "global"` for the app-drawer queue view
(`/sdk/handlers-applications/`), `on_open()` returning `LaunchModalEffect`
pointing at a page served by a `SimpleAPIRoute`/`SimpleAPI` handler in the
same plugin (`/sdk/handlers-simple-api-http/`). Data endpoints authenticate
via `SessionCredentials`/`StaffSessionAuthMixin`
(`/sdk/handlers-simple-api-http/#staff-session`) so the viewing provider's
identity is known server-side for authorization filtering — not a static API
key, since this is an in-Canvas, staff-only UI.

**Badge** — `compute_notification_badge()` on the `Application` for the
initial DEFERRED count on load, plus `ApplicationNotificationBadge(...).broadcast(...)`
from the queue-processing handlers for live updates
(`/sdk/effect-application-notification-badge/`). Confirmed supported for
`global`-scope applications.

**Patient-level visibility (Step 6)** — a second `Application` registration,
same handler family, `scope: "patient_specific"`, pre-filtered to
`self.event.context["patient"]["id"]`. This is a real, documented scope
(`/sdk/handlers-applications/#application-scopes`) and avoids inventing a
navigation hack (see Open Question 3 below for why "click a row to jump to
that patient's chart" doesn't have an equally clean answer).

**Business-date timezone** — confirmed: every `BaseHandler`/`CronTask`
exposes `self.environment["INSTALLATION_TIME_ZONE"]`, the instance's
configured time zone as an IANA name (`/sdk/handlers/`). Fed straight into
`zoneinfo.ZoneInfo(...)` and `logic.queue.business_date()` — no separate
instance-analysis step needed, and no UTC-only fallback except as a defensive
default if the key is ever missing/invalid.

---

## Decisions (resolved after this discovery was first circulated)

1. **Priority source** — treat every lab order as ROUTINE. The STAT/URGENT
   bypass logic is implemented and tested (it costs nothing to keep), but
   nothing currently feeds it a non-ROUTINE value — see `logic/queue.py`'s
   module docstring.
2. **Authorization** — any logged-in staff/provider may see the full queue;
   no per-provider or per-care-team filtering. `StaffSessionAuthMixin` still
   gates the API to real, logged-in Canvas staff (not anonymous/portal
   sessions) — see TEST_PLAN.md scenario 15/16 for what this does and does
   not mean.
3. ~~**Row navigation** — clicking a row opens this plugin's patient-specific
   Application for that patient (§6), not a native Canvas chart deep-link.~~
   **Superseded** — see note below. A real chart URL does exist; the
   "Chart" button now uses it.

### Correction: a documented patient-chart URL does exist

The original open question #3 below concluded no supported cross-page
navigation existed for this surface. That was wrong — it was searched for
only in the SDK handler docs (`/sdk/...`); the FHIR API docs (a different
doc set, `/api/...`) state it plainly, twice: the **Patient create**
endpoint's response description (`/api/patient/`) says *"the patient record
can be viewed in Canvas at `https://<instance>.canvasmedical.com/patient/<id>`"*,
and this is confirmed elsewhere in the same doc set too. Independently
confirmed live by the user against jlab-dev.

So: a "Chart" button per row now does
`window.top.location = "/patient/" + patient_id` (same pattern as the
Provider Companion's own documented `window.top.location` navigation, just
targeting the main app's URL instead of the companion one) — a real jump to
Canvas's native chart, not a workaround. Row-click-to-filter-this-view (the
original fallback) is kept as a separate, additional action, not replaced.

## Record of the original open questions (for reference)

### 1. Lab orders have no `priority` field. How should STAT/URGENT be captured?

Confirmed by three independent sources: the `LAB_ORDER_COMMAND__*` event
context `fields` (lab_partner, tests, ordering_provider, diagnosis,
fasting_status, comment — no priority), the `LabOrderCommand` SDK class's own
parameter list (same set, no priority), and the FHIR `ServiceRequest`
resource (status, intent, category, code, subject, occurrencePeriod,
authoredOn, requester, reasonReference — no priority). By contrast,
`ImagingOrderCommand` and `ReferCommand` **do** have a `.Priority` enum
(STAT/URGENT/ROUTINE) — Canvas just didn't carry that forward to Lab Order.

Your spec's entire STAT/URGENT bypass rests on a field that doesn't exist
today. Three real options, none of them a workaround:

- **(a) Add a custom field via a Command Metadata Create Form effect** — a
  documented mechanism for adding a field to a command's own UI, stored as
  command metadata. Whoever places the order picks STAT/URGENT/ROUTINE at
  order time. Cleanest, but changes the order-entry workflow and needs staff
  training/adoption.
- **(b) Infer priority from the free-text `comment` field** — no workflow
  change, but fragile (unstructured text, easy to miss, no validation) and I
  would not recommend building safety-relevant bypass logic on it.
- **(c) Treat every Lab Order as ROUTINE for this plugin's purposes** — if
  this practice has no real STAT/URGENT lab-order workflow today, the bypass
  logic may be unnecessary. (Separately note: your spec's step 6 already says
  *"default to not consuming [the routine slot]"* for STAT/URGENT, so even
  under (a) this is a small amount of logic.)

I'd default to **(a)** if STAT/URGENT lab orders are a real, current
practice at JLab, or **(c)** if they're not — but this is your call, not
mine to assume.

### 2. Authorization policy for the queue UI

Confirmed supported: `StaffSessionAuthMixin`/`SessionCredentials` tells us
*who* is viewing (a real, logged-in staff session, not an API key). What's
not yet defined is the *rule*: should every staff member who can open the
app drawer see every patient's queue (role-gated only, e.g. "must be a
provider"), or should a given provider only see orders where they are the
`ordering_provider`? Canvas's own role/permission model (Staff roles, care
teams) can back either, but Step 7 test #16 ("user cannot access another
provider's data") implies the latter and I don't want to guess which.

### 3. "Click a row to navigate to that patient's chart" — no clean equivalent found for this surface

**(Corrected above — this conclusion was wrong; kept here only as a record
of what was actually searched at the time.)**

The only documented cross-page navigation pattern in the installed SDK docs
is `window.top.location = "/companion/patient/<uuid>/"`
(`/sdk/companion/`) — and that's explicitly for the **Provider Companion**
mobile surface, a different app shell than the main desktop Canvas UI our
`global`-scope Application opens into. I found no documented URL scheme for
jumping the main Canvas UI to a specific patient's chart from a plugin iframe.
Rather than invent one (which your own instructions tell me not to do), my
recommendation is the patient-specific Application from §6 above as the
closest supported substitute — it's a real "view this patient's queue"
entry point, just reached via the patient's own app drawer rather than a
click-through link. If you know of a navigation URL Canvas does support here
that isn't in the bundled docs, tell me and I'll use it instead.

---

**Nothing in Steps 2–9 has been implemented yet.** Waiting on your answers to
the three questions above before writing the queue logic, persistence model,
CronTask, UI, tests, and remaining deliverables.
