# TEST_PLAN.md — jlab_order_queue UAT

Manual acceptance steps against the `jlab-dev` instance, after following
DEPLOYMENT.md. Automated coverage for the logic underlying every scenario
below lives in `tests/` (55 tests: `tests/logic/test_queue.py` is the
exhaustive, mock-free core; `tests/handlers/`, `tests/apps/`, `tests/api/`
cover the Canvas-facing wiring) — this plan verifies the same scenarios
actually work end-to-end in the live Canvas UI, which no automated test in
this repo can do (nothing here can click a real Sign button).

## Setup

- Two or more test patients with no existing JLab lab orders today. Call
  them **Patient A** and **Patient B**.
- A test provider account able to place and sign Lab Order commands.
- The JLab Order Queue app installed and enabled (DEPLOYMENT.md steps 1–5).
- `uv run canvas logs --host jlab-dev --plugin jlab_order_queue --since 5m`
  running in a terminal throughout, so you can watch the structured logs
  (`order detected`, `queue decision`, `release`, `cancellation detected`,
  `signing detected`) as you go.
- Open the global **JLab Order Queue** app in a separate browser tab so you
  can refresh it alongside each step.

Each scenario lists: steps, expected result in the note UI, expected
result in the JLab Order Queue app, and the log line to look for.

---

### 1. First routine order today → READY

1. Open a note for Patient A. Add a **Lab Order** command (any lab
   partner/test), leave it staged.
2. Observe the command's Sign button.

**Expected:** Sign is enabled immediately — no tooltip, no delay. Log shows
`order detected` then `queue decision` is absent (no block applied). In the
queue app, Patient A's row shows `queue_state=READY`, `eligible_date` =
today.

---

### 2. Second routine order, same patient, same day → DEFERRED until tomorrow

1. Without signing the first order, add a **second** Lab Order command for
   Patient A (a different test), in the same or a different note.

**Expected:** The second command's Sign button is **disabled**, with a
tooltip reading approximately *"Another routine JLab lab order for this
patient was already released today; this order becomes eligible on
<tomorrow's date>."* Log shows `queue decision: ... deferred`. Queue app
shows a second row for Patient A: `queue_state=DEFERRED`,
`eligible_date` = tomorrow, `defer_reason` populated.

---

### 3. Three routine orders, same patient → three sequential eligible dates

1. Add a third Lab Order command for Patient A.

**Expected:** Third command also blocked, tooltip mentions a date two days
out. Queue app shows three rows for Patient A with eligible dates today,
tomorrow, and the day after, in the order the commands were staged.

---

### 4. Two different patients → both READY today

1. Repeat scenario 1 for Patient B (their own first order of the day).

**Expected:** Patient B's order is READY immediately, same as Patient A's
first order was — Patient A's existing DEFERRED orders have no effect on
Patient B.

---

### 5 & 6. STAT / URGENT bypass

Today, every JLab lab order is treated as ROUTINE (see README.md
"Priority" — the installed Canvas SDK's Lab Order command has no priority
field to read a STAT/URGENT value from). There is currently no UI control
that produces a non-ROUTINE order, so this scenario cannot be exercised
end-to-end in the live UI yet.

**What's actually verified today:** `tests/logic/test_queue.py` has both
`test_stat_order_bypasses_the_queue_even_with_a_routine_order_today` and
`test_urgent_order_bypasses_the_queue` exercising the bypass logic directly
and `test_bypass_order_does_not_consume_the_routine_slot` confirming a
same-day routine order is unaffected by a bypassed one. If/when a priority
source is added (see README.md), add a step here placing a STAT/URGENT
order alongside an existing DEFERRED routine order for the same patient
and confirm it is immediately signable.

---

### 7. Duplicate event does not create a duplicate queue record

This can't be triggered deliberately through the UI (Canvas, not this
plugin, controls when `POST_ORIGINATE` fires). Verified automatically:
`tests/handlers/test_lab_order_queue.py::test_on_originate_is_idempotent_for_a_duplicate_event`.
As a sanity check during UAT: confirm the queue app never shows two rows
for what you know to be a single command you staged once.

---

### 8. Deferred order is cancelled

1. With Patient A's second (DEFERRED) order from scenario 2 still staged,
   delete that command from the note.

**Expected:** Log shows `cancellation detected`. Queue app shows that row
now `queue_state=CANCELLED`. Patient A's **third** order (previously
deferred two days out) is pulled forward to tomorrow — refresh the queue
app and confirm its `eligible_date` moved up by one day, `blocking_order`
reference updated.

---

### 9. Ready order is signed

1. Sign Patient A's first (READY) order.

**Expected:** Sign succeeds normally (this plugin never blocks a READY
order). Log shows `signing detected`. Queue app shows `queue_state=SIGNED`,
`signed_at` populated. The still-deferred order for Patient A is
unaffected (signing does not free today's slot — it was already spent by
having been released).

---

### 10. Deferred order reaches its eligible date

1. Leave a DEFERRED order for a test patient overnight (eligible_date =
   tomorrow).
2. The next day, without doing anything else for that patient, check the
   command's Sign button and the queue app.

**Expected:** Within 5 minutes of local midnight, the scheduled release
task promotes it. Log (from the CronTask) shows `release sweep starting`
then `release: command=... state=RELEASED`. The command's Sign button is
now enabled with no tooltip. Queue app shows `queue_state=RELEASED`,
`released_at` populated.

---

### 11. New order arrives while an older deferred order exists

1. With Patient A having one DEFERRED order already (eligible tomorrow),
   add yet another new Lab Order command for Patient A.

**Expected:** The new order is deferred **behind** the existing one — its
eligible date is the day after, not tomorrow — regardless of the order you
happened to add them to the note in. (FIFO is by received/staged time, not
by which one you clicked into most recently.)

---

### 12. Date rollover / timezone behavior

1. Confirm the instance's configured timezone: Settings → general instance
   configuration (or ask your Canvas admin) — this is the IANA zone name
   `self.environment["INSTALLATION_TIME_ZONE"]` returns.
2. Place an order late in the local evening (e.g. 11 PM local time) for a
   test patient who already has today's routine slot taken.

**Expected:** The new order's `eligible_date` is **tomorrow in the
instance's local timezone**, not UTC — if local time and UTC date
disagree at that hour (e.g. instance is US-based, so UTC is already past
midnight while local time is still "today"), the eligible date must match
the *local* day, confirming UTC is not silently being used instead.
Automated coverage: `tests/logic/test_queue.py::test_business_date_uses_instance_timezone_not_utc`
and `::test_business_date_rolls_over_at_local_midnight`.

---

### 13. Weekend behavior

1. On a Thursday or Friday, place three routine orders for one test
   patient in a single day.

**Expected:** Eligible dates land on consecutive **calendar** days
including Saturday/Sunday if applicable — this plugin does not skip
weekends (see README.md and
`tests/logic/test_queue.py::test_weekend_dates_are_assigned_like_any_other_calendar_day`).
If JLab actually wants weekends skipped, that's a scope change to
`logic/queue.py`'s `_first_available_date`, not a bug — flag it here if
this assumption is wrong for your workflow.

---

### 14. Provider change

1. Edit a staged, still-DEFERRED Lab Order command for a test patient and
   change its ordering provider.

**Expected:** Log shows `ordering provider changed`. Queue app's
"Provider" column for that row updates to the new provider.
`queue_state`/`eligible_date` are unchanged — the order's position in the
queue does not move because the provider changed.

---

### 15 & 16. Queue UI authorization

Per the confirmed policy (see DISCOVERY.md "Decisions"), **any logged-in
Canvas staff member sees the whole queue** — there is no per-provider
restriction, so scenario 16 as originally phrased ("user cannot access
another provider's data") does not apply by design; verifying it would
mean asserting the opposite of the chosen policy.

**What to actually verify:**

1. Open the global JLab Order Queue app as a logged-in staff user.
   **Expected:** the page loads and shows data.
2. Attempt to hit the data endpoint directly without a Canvas session (e.g.
   `curl https://jlab-dev.canvasmedical.com/plugin-io/api/jlab_order_queue/queue/data`
   with no cookies/auth header).
   **Expected:** rejected (not a 200 with data) — `StaffSessionAuthMixin`
   requires a real, logged-in staff session; an anonymous or patient-portal
   session must not see this data. Automated coverage:
   `tests/api/test_queue_api.py::test_queue_api_is_gated_on_a_staff_session`.
3. Confirm two different staff accounts both see the **same** full queue
   (rows are not filtered differently per viewer) — this confirms the
   chosen policy is actually what's deployed, not an accidental narrower
   filter.

If JLab later decides per-provider filtering is actually wanted, that is a
policy change to `api/queue_api.py` (add an `ordering_provider`-based
filter keyed off the session's own staff id), not a bug fix.

---

## Additional scenarios worth checking live (beyond the required 16)

- **Already-signed order, re-rendered:** open a note containing a SIGNED
  Lab Order command. Confirm there's no validation error shown (signed
  commands don't re-trigger `POST_VALIDATION` in a way that would block
  anything — but worth eyeballing once).
- **Entered-in-error:** void a signed Lab Order command (mark
  entered-in-error). Confirm the queue app shows `queue_state=CANCELLED`
  for it and that this does **not** retroactively un-sign anything.
- **Notification badge:** with at least one DEFERRED order anywhere on the
  instance, confirm the JLab Order Queue app drawer icon shows a numeric
  badge matching the DEFERRED count in the queue app's own table, and that
  it updates within a few seconds of a new order being deferred (no page
  reload needed — `ApplicationNotificationBadge`'s live-update behavior).
- **Patient-specific view:** open a patient's own chart, find "JLab Order
  Queue" in that patient's app drawer, and confirm it shows only that
  patient's rows.
- **Row click:** from the global queue view, click a row with a patient
  (not the Chart button). Confirm it switches this same view to that
  patient's own filtered queue.
- **Chart button:** click the "Chart" button on a row. Confirm the browser
  tab navigates to that patient's real chart in Canvas (`/patient/<id>`),
  not just this app's own filtered view (see README.md "Row navigation").
- **Refresh button:** click it with no filter change. Confirm the table
  re-fetches (watch `canvas logs` for a fresh `GET /queue/data` line).
