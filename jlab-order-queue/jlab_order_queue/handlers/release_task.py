"""Scheduled promotion of deferred JLab routine lab orders.

Catches the one case the real-time event handler (`lab_order_queue.py`)
cannot: a DEFERRED order's `eligible_date` simply arriving because the
calendar rolled over, with no Canvas event firing to prompt a recompute.
The real-time handler already recomputes a patient's whole queue on every
origination, commit, update, delete, and enter-in-error; this task's only
job is to periodically re-run that same recompute for every patient who has
something DEFERRED, so "nothing else happened, but a day passed" still
gets handled (see `logic/queue.py`'s date-rollover test for the underlying
mechanism).

Never signs, cancels, or deletes anything -- `recompute_and_persist` only
ever changes this plugin's own queue_state/eligible_date/defer_reason/
blocking_order/released_at columns. Releasing an order means clearing the
Command Validation block on its next render (`POST_VALIDATION`); the
physician's own click on `sign_action` is still what actually signs it.

KNOWN LIMITATION (flag for UAT): this relies on the real-time handler
having already kept each row's `queue_state` in sync with the command's
actual state (SIGNED on commit, CANCELLED on delete/enter-in-error). It
does not independently re-read the live `Command.state` before releasing,
because the exact string values that field takes beyond `"committed"`
(confirmed in the SDK docs) aren't pinned down in the bundled
documentation, and guessing at an enum value is exactly the kind of
invention Step "Important" in the spec prohibits. Verify end-to-end against
a live instance per TEST_PLAN.md before relying on this in production.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from canvas_sdk.effects import Effect
from canvas_sdk.effects.application_notification_badge import (
    ApplicationNotificationBadge,
)
from canvas_sdk.handlers.cron_task import CronTask
from logger import log

from jlab_order_queue.badge import actionable_count
from jlab_order_queue.config import GLOBAL_APP_IDENTIFIER, instance_timezone
from jlab_order_queue.logic.queue import DEFERRED, business_date
from jlab_order_queue.models import JLabOrderQueueEntry
from jlab_order_queue.sync import recompute_and_persist


class ReleaseDeferredOrders(CronTask):
    # Every 5 minutes: frequent enough that an order becomes available soon
    # after local midnight; cheap enough (one query per patient with
    # something deferred -- almost always zero or a handful at a time) to
    # run this often. Matches the cadence of the sibling order-poller /
    # billing-poller scheduled tasks already running in this workspace.
    SCHEDULE = "*/5 * * * *"

    def execute(self) -> list[Effect]:
        now = datetime.now(dt_timezone.utc)
        tz = instance_timezone(self.environment)
        today = business_date(now, tz)

        patient_dbids = list(
            JLabOrderQueueEntry.objects.filter(queue_state=DEFERRED)
            .values_list("patient_id", flat=True)
            .distinct()
        )
        log.info(
            f"[jlab_order_queue] release sweep starting: today={today.isoformat()} "
            f"patients_with_deferred_orders={len(patient_dbids)}"
        )

        released = 0
        for patient_dbid in patient_dbids:
            try:
                results = recompute_and_persist(patient_dbid, today, now)
            except Exception:
                # Fail safely per patient: one patient's bad data must not
                # stop every other patient's queue from being evaluated.
                log.exception(
                    "[jlab_order_queue] release sweep failed for "
                    f"patient_dbid={patient_dbid}; leaving that patient's "
                    "queue untouched this cycle"
                )
                continue
            for result in results:
                if result.became_available:
                    released += 1
                    log.info(
                        f"[jlab_order_queue] release: command={result.command_id} "
                        f"patient_dbid={patient_dbid} state={result.queue_state} "
                        f"eligible_date={result.eligible_date.isoformat()}"
                    )

        if released:
            # Must agree with `apps/queue_app.py`'s `compute_notification_badge()`
            # on what the badge counts -- see badge.py.
            ApplicationNotificationBadge(GLOBAL_APP_IDENTIFIER).broadcast(
                count=actionable_count()
            )

        log.info(f"[jlab_order_queue] release sweep complete: released={released}")
        return []
