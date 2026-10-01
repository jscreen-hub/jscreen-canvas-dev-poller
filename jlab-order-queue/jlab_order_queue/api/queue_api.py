"""SimpleAPI backing the JLab Order Queue application.

Two routes, both gated by `StaffSessionAuthMixin` (`/sdk/handlers-simple-api-http/#staff-session`)
so only a real, logged-in Canvas staff session can reach this plugin's
data -- per the confirmed authorization policy, any staff member sees the
whole queue (no per-provider/per-patient filtering beyond an optional
`patient_id` query param used for the patient-scoped view itself).

- `GET /queue` — a static HTML/CSS/JS shell. It renders nothing server-side;
  all table data comes from `/queue/data` via `fetch()`, which is what
  keeps filtering/sorting/live-refresh entirely client-side and this route
  trivially cacheable.
- `GET /queue/data` — JSON rows, filtered by query params. This is also
  the one place patient data crosses into an HTTP response at all, and it
  never leaves Canvas: nothing in this plugin posts this data anywhere
  external (Step 5 "do not expose PHI outside Canvas").
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from canvas_sdk.effects import Effect
from canvas_sdk.effects.simple_api import HTMLResponse, JSONResponse, Response
from canvas_sdk.handlers.simple_api import SimpleAPI, StaffSessionAuthMixin, api
from logger import log

from jlab_order_queue.logic.queue import (
    CANCELLED,
    DEFERRED,
    READY,
    RELEASED,
    SIGNED,
)
from jlab_order_queue.models import (
    CustomCommand,
    CustomPatient,
    CustomStaff,
    JLabOrderQueueEntry,
)

# `django.core.exceptions` is not an allowed import in the Canvas plugin
# sandbox (confirmed live: `canvas validate` rejects it with "ImportError:
# 'django.core.exceptions' is not an allowed import"). Each model's own
# `.DoesNotExist` is already reachable through the models already imported
# above, so catching this tuple needs no otherwise-blocked import.
_RELATED_DOES_NOT_EXIST = (
    CustomCommand.DoesNotExist,
    CustomPatient.DoesNotExist,
    CustomStaff.DoesNotExist,
)

ALL_STATES = (READY, DEFERRED, RELEASED, SIGNED, CANCELLED)


def _safe_related(getter: Any) -> Any:
    """A lazy FK traversal that may point at a row which no longer exists.

    Confirmed live on jlab-dev: a Lab Order command replaced or deleted
    while staged (e.g. the user changes the lab partner/test selection
    before finishing) can leave this plugin's row's `command_id` pointing at
    a `Command` that is gone, which raised `CustomCommand.DoesNotExist` here
    and took the *whole* `/queue/data` response down with it -- one stale
    row broke the view for every row. Returns `None` instead, so one stale
    reference degrades that one field rather than the whole request.
    """
    try:
        return getter()
    except _RELATED_DOES_NOT_EXIST:
        return None


def _serialize_entry(entry: JLabOrderQueueEntry) -> dict[str, Any]:
    """One queue row -> the shape the UI table renders.

    Pulled into its own function so it is independently testable without a
    live SimpleAPI request/response round trip (see
    tests/api/test_queue_api.py).
    """
    patient = _safe_related(lambda: entry.patient)
    provider = _safe_related(lambda: entry.ordering_provider)
    command = _safe_related(lambda: entry.command)
    return {
        "command_id": str(command.id) if command else None,
        "patient_id": str(patient.id) if patient else None,
        "patient_name": (
            f"{patient.first_name} {patient.last_name}".strip() if patient else None
        ),
        "mrn": getattr(patient, "mrn", None) if patient else None,
        "order_type": "Lab Order",
        "received_at": entry.received_at.isoformat() if entry.received_at else None,
        "priority": entry.priority,
        "queue_state": entry.queue_state,
        "eligible_date": entry.eligible_date.isoformat() if entry.eligible_date else None,
        "released_at": entry.released_at.isoformat() if entry.released_at else None,
        "signed_at": entry.signed_at.isoformat() if entry.signed_at else None,
        "provider_name": (
            f"{provider.first_name} {provider.last_name}".strip() if provider else None
        ),
        "defer_reason": entry.defer_reason,
    }


def _apply_filters(queryset: Any, params: dict[str, str]) -> Any:
    """Query-param filtering for `/queue/data`.

    Supported params: `state` (repeatable; defaults to every state but
    CANCELLED), `provider_id`, `patient_id`, `date` (matches eligible_date).
    """
    states = [s.upper() for s in params.getlist("state")] if hasattr(params, "getlist") else (
        [params["state"].upper()] if "state" in params else []
    )
    if states:
        queryset = queryset.filter(queue_state__in=[s for s in states if s in ALL_STATES])
    else:
        # Default view: everything actionable, hide cancelled noise unless
        # the user explicitly asks for it via ?state=CANCELLED.
        queryset = queryset.exclude(queue_state=CANCELLED)

    provider_id = params.get("provider_id")
    if provider_id:
        queryset = queryset.filter(ordering_provider__id=provider_id)

    patient_id = params.get("patient_id")
    if patient_id:
        queryset = queryset.filter(patient__id=patient_id)

    eligible_date = params.get("date")
    if eligible_date:
        queryset = queryset.filter(eligible_date=eligible_date)

    return queryset


def _sort(queryset: Any) -> Any:
    """Deferred orders sorted by eligible date then received timestamp;
    everything else newest-received first, per Step 5's sort requirement."""
    return queryset.order_by("eligible_date", "received_at")


class QueueAPI(StaffSessionAuthMixin, SimpleAPI):
    @api.get("/queue")
    def page(self) -> list[Response | Effect]:
        log.info("[jlab_order_queue] GET /queue (page shell)")
        return [HTMLResponse(_PAGE_HTML, status_code=HTTPStatus.OK)]

    @api.get("/queue/data")
    def data(self) -> list[Response | Effect]:
        params = dict(self.request.query_params)
        log.info(f"[jlab_order_queue] GET /queue/data params={params}")
        try:
            # Deliberately NOT using `select_related()` here. CustomModel
            # fields can't declare `null=True` (unsupported -- "has no
            # effect" per the SDK docs), so Django has no way to know
            # `ordering_provider` (nullable at the DB level; frequently NULL
            # in practice -- see `_resolve_ordering_provider_dbid`) is
            # actually optional. `select_related()` on an FK Django believes
            # is required generates an INNER JOIN, which silently drops
            # every row where that FK is NULL -- confirmed live on jlab-dev:
            # the notification badge (a plain `.filter().count()`, no join)
            # showed 5 DEFERRED rows while this endpoint returned zero for
            # every filter, because every test row so far has a NULL
            # `ordering_provider`. Plain FK traversal below is a few extra
            # queries per row instead, which is fine at this table's scale.
            total = JLabOrderQueueEntry.objects.count()
            queryset = JLabOrderQueueEntry.objects.all()
            queryset = _apply_filters(queryset, self.request.query_params)
            queryset = _sort(queryset)
            rows = [_serialize_entry(entry) for entry in queryset]
            log.info(
                f"[jlab_order_queue] /queue/data: {total} total row(s) in table, "
                f"{len(rows)} after filters"
            )
        except Exception:
            # Fail safely and visibly: an empty/malformed response here must
            # never be confused with "there are genuinely zero rows" -- log
            # the real cause and surface a distinguishable error to the page.
            log.exception("[jlab_order_queue] /queue/data failed to build rows")
            return [
                JSONResponse(
                    {"error": "failed to load queue data"},
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            ]
        return [JSONResponse({"rows": rows}, status_code=HTTPStatus.OK)]


_PAGE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>JLab Order Queue</title>
<style>
  body { font-family: -apple-system, Segoe UI, sans-serif; margin: 0; padding: 12px; background: #fff; color: #1a1a1a; }
  h1 { font-size: 16px; margin: 0 0 12px; }
  .filters { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
  .filters button { border: 1px solid #ccc; background: #f5f5f5; border-radius: 4px; padding: 4px 10px; cursor: pointer; font-size: 12px; }
  .filters button.active { background: #2a5db0; color: #fff; border-color: #2a5db0; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee; }
  th { color: #666; font-weight: 600; }
  tr.row { cursor: pointer; }
  tr.row:hover { background: #f7f9fc; }
  .state { padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; }
  .state-READY, .state-RELEASED { background: #e6f4ea; color: #1e7e34; }
  .state-DEFERRED { background: #fff4e5; color: #a66a00; }
  .state-SIGNED { background: #e8eaf6; color: #3949ab; }
  .state-CANCELLED { background: #f1f1f1; color: #777; }
  .empty { color: #888; padding: 16px 0; }
  .refresh { margin-left: auto; }
  .chart-link { border: 1px solid #ccc; background: #fff; border-radius: 4px; padding: 2px 8px; cursor: pointer; font-size: 12px; color: #2a5db0; }
  .chart-link:hover { background: #f0f5fc; }
</style>
</head>
<body>
<h1>JLab Order Queue</h1>
<div class="filters" id="filters">
  <button data-state="" class="active">All</button>
  <button data-state="READY">Ready</button>
  <button data-state="DEFERRED">Deferred</button>
  <button data-state="RELEASED">Released</button>
  <button data-state="SIGNED">Signed</button>
  <button data-state="CANCELLED">Cancelled</button>
  <button id="refresh" class="refresh" type="button">&#8635; Refresh</button>
</div>
<table>
  <thead>
    <tr>
      <th>Patient</th><th>MRN</th><th>Order Type</th><th>Received</th>
      <th>Priority</th><th>Queue Status</th><th>Eligible Date</th>
      <th>Released Date</th><th>Provider</th><th>Reason Deferred</th><th>Chart</th>
    </tr>
  </thead>
  <tbody id="rows"></tbody>
</table>
<div class="empty" id="empty" style="display:none">No orders match this filter.</div>
<script>
(function () {
  var params = new URLSearchParams(window.location.search);
  // `initialPatientId` is how this page was *launched* -- set by
  // `PatientQueueApp` for its patient-specific view, absent for the global
  // app. `patientId` is the current, possibly-narrower scope after a row
  // click. Refresh restores the former, not just re-fetches the latter --
  // otherwise, once you've clicked into one patient from the global view,
  // every future Refresh stays stuck on that one patient.
  var initialPatientId = params.get("patient_id");
  var patientId = initialPatientId;
  var state = "";

  // Reflects the current `patientId` in the address bar without reloading
  // the page -- see the row-click handler below for why a reload would
  // break `initialPatientId`.
  function setUrlPatientId(id) {
    var next = new URLSearchParams(window.location.search);
    if (id) {
      next.set("patient_id", id);
    } else {
      next.delete("patient_id");
    }
    var qs = next.toString();
    var url = window.location.pathname + (qs ? "?" + qs : "");
    history.replaceState(null, "", url);
  }

  function fmt(iso) {
    if (!iso) return "";
    return iso.slice(0, 10);
  }

  function load() {
    var q = new URLSearchParams();
    if (state) q.set("state", state);
    if (patientId) q.set("patient_id", patientId);
    fetch("/plugin-io/api/jlab_order_queue/queue/data?" + q.toString(), {
      headers: { "Accept": "application/json" },
      credentials: "same-origin",
    })
      // A non-2xx response (e.g. an auth rejection or a server error) must
      // never be silently treated as "zero rows" -- that previously showed
      // the exact same empty state as a genuinely empty queue, which made
      // an actual failure indistinguishable from there being nothing to
      // show. Surface the HTTP status explicitly instead.
      .then(function (r) {
        if (!r.ok) {
          throw new Error("HTTP " + r.status + " from /queue/data");
        }
        return r.json();
      })
      .then(render)
      .catch(function (err) {
        document.getElementById("rows").innerHTML = "";
        document.getElementById("empty").style.display = "block";
        document.getElementById("empty").textContent =
          "Unable to load the queue right now (" + err.message + ").";
        console.error(err);
      });
  }

  function render(data) {
    var rows = data.rows || [];
    var body = document.getElementById("rows");
    var empty = document.getElementById("empty");
    // Reset in case a previous load left an error message here -- a
    // genuinely empty (but successful) response must show the normal
    // empty-state copy, not a stale error from an earlier failed attempt.
    empty.textContent = "No orders match this filter.";
    body.innerHTML = "";
    empty.style.display = rows.length ? "none" : "block";
    rows.forEach(function (row) {
      var tr = document.createElement("tr");
      tr.className = "row";
      tr.innerHTML =
        "<td>" + (row.patient_name || "") + "</td>" +
        "<td>" + (row.mrn || "") + "</td>" +
        "<td>" + (row.order_type || "") + "</td>" +
        "<td>" + fmt(row.received_at) + "</td>" +
        "<td>" + (row.priority || "") + "</td>" +
        "<td><span class=\\"state state-" + row.queue_state + "\\">" + row.queue_state + "</span></td>" +
        "<td>" + (row.eligible_date || "") + "</td>" +
        "<td>" + fmt(row.released_at) + "</td>" +
        "<td>" + (row.provider_name || "") + "</td>" +
        "<td>" + (row.defer_reason || "") + "</td>" +
        "<td></td>";
      // Clicking a row (other than the Chart button below) switches this
      // same view into that patient's own queue -- a different thing from
      // the Chart button, which leaves this app entirely for Canvas's own
      // patient chart (see README.md "Row navigation").
      //
      // Deliberately NOT `window.location.search = ...` here: setting
      // `location.search` navigates (a real page reload), and on reload
      // this whole script re-runs from scratch and re-reads
      // `initialPatientId` from the now-changed URL -- which silently
      // turns "the scope I clicked into" into "the scope this page was
      // supposedly launched with", so Refresh had nothing correct left to
      // restore. `history.replaceState` updates the address bar (so the
      // URL still reflects the current view, e.g. for sharing/reload)
      // without reloading, which is what actually lets `initialPatientId`
      // stay the one true original value for the page's whole lifetime.
      tr.addEventListener("click", function () {
        if (row.patient_id && row.patient_id !== patientId) {
          patientId = row.patient_id;
          setUrlPatientId(patientId);
          load();
        }
      });
      if (row.patient_id) {
        var chartBtn = document.createElement("button");
        chartBtn.type = "button";
        chartBtn.className = "chart-link";
        chartBtn.textContent = "Chart";
        chartBtn.title = "Open this patient's chart in Canvas";
        chartBtn.addEventListener("click", function (ev) {
          // Must not also trigger the row's own click handler above (which
          // would just filter this same view to the patient instead of
          // leaving it).
          ev.stopPropagation();
          // Documented at docs.canvasmedical.com/api/patient/ ("Patient
          // create"): a patient's chart lives at
          // https://<instance>.canvasmedical.com/patient/<id>. Navigates
          // the whole browser tab, not just this iframe, since this app's
          // own modal has no reason to still be open once you've left it
          // for the native chart.
          window.top.location = "/patient/" + row.patient_id;
        });
        tr.lastElementChild.appendChild(chartBtn);
      }
      body.appendChild(tr);
    });
  }

  document.getElementById("filters").addEventListener("click", function (ev) {
    var btn = ev.target.closest("button");
    if (!btn || btn.id === "refresh") return;
    Array.prototype.forEach.call(
      document.querySelectorAll("#filters button[data-state]"),
      function (b) { b.classList.remove("active"); }
    );
    btn.classList.add("active");
    state = btn.getAttribute("data-state");
    load();
  });

  document.getElementById("refresh").addEventListener("click", function () {
    // Always restores the scope this page was actually launched with (undoing
    // any row-click narrowing) and resets the state filter to "All", then
    // re-fetches -- "refresh the whole order queue", not just whatever
    // narrower view happens to be showing right now.
    patientId = initialPatientId;
    setUrlPatientId(patientId);
    state = "";
    Array.prototype.forEach.call(
      document.querySelectorAll("#filters button[data-state]"),
      function (b) { b.classList.toggle("active", b.getAttribute("data-state") === ""); }
    );
    load();
  });

  load();
})();
</script>
</body>
</html>
"""
