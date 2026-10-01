"""The JLab Order Queue application: global app-drawer entry + patient view.

Two `Application` registrations share one served page
(`api/queue_api.py`'s `/queue` route): `GlobalQueueApp` (scope `global`,
shown outside patient charts, Step 5) and `PatientQueueApp` (scope
`patient_specific`, shown only within a patient's own chart app drawer,
Step 6). `PatientQueueApp` passes the current patient's id as a query
parameter so the same page opens pre-filtered -- see DISCOVERY.md's
"Row navigation" note for why clicking a row in the global view switches
into this same per-patient view (client-side, in `queue_api.py`'s page
script) rather than a native Canvas chart deep-link, which has no
documented equivalent for this surface.

Both report the live actionable (READY + RELEASED) count as their
notification badge (`/sdk/effect-application-notification-badge/`): the
whole-instance count for the global app, and the count scoped to the
patient being viewed for the patient-specific one, exactly as the SDK's own
badge example does for its own per-patient Task count. See `badge.py` for
why this counts actionable orders rather than deferred ones.
"""

from __future__ import annotations

from canvas_sdk.effects import Effect
from canvas_sdk.effects.launch_modal import LaunchModalEffect
from canvas_sdk.handlers.application import Application

from jlab_order_queue.badge import actionable_count

_QUEUE_PAGE_PATH = "/plugin-io/api/jlab_order_queue/queue"


class GlobalQueueApp(Application):
    def on_open(self) -> Effect | list[Effect]:
        return LaunchModalEffect(
            url=_QUEUE_PAGE_PATH,
            title="JLab Order Queue",
            target=LaunchModalEffect.TargetType.DEFAULT_MODAL,
        ).apply()

    def compute_notification_badge(self) -> int | None:
        return actionable_count()


class PatientQueueApp(Application):
    def on_open(self) -> Effect | list[Effect]:
        patient_id = self.event.context.get("patient", {}).get("id")
        url = f"{_QUEUE_PAGE_PATH}?patient_id={patient_id}" if patient_id else _QUEUE_PAGE_PATH
        return LaunchModalEffect(
            url=url,
            title="JLab Order Queue",
            target=LaunchModalEffect.TargetType.RIGHT_CHART_PANE,
        ).apply()

    def compute_notification_badge(self) -> int | None:
        patient_id = self.event.context.get("patient", {}).get("id")
        if not patient_id:
            return None
        return actionable_count(patient_id)
