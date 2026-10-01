"""The JLab Order Queue notification badge: shared count logic.

Every place that sets the badge (the initial `compute_notification_badge()`
on each `Application`, the real-time handler, and the CronTask) must agree
on what it counts -- this is the one place that decision lives, so the
three call sites can't drift apart from each other.

The badge counts orders **actionable right now** -- `READY` and `RELEASED`
(both mean the Sign button is enabled; see `logic/queue.py`) -- not
`DEFERRED`. A deferred count tells a physician how many orders are stuck
waiting, which isn't something they can act on; a ready/released count
tells them how many need their attention today, which is.
"""

from __future__ import annotations

from jlab_order_queue.logic.queue import READY, RELEASED
from jlab_order_queue.models import JLabOrderQueueEntry

ACTIONABLE_STATES = (READY, RELEASED)


def actionable_count(patient_id: str | None = None) -> int:
    queryset = JLabOrderQueueEntry.objects.filter(queue_state__in=ACTIONABLE_STATES)
    if patient_id:
        queryset = queryset.filter(patient__id=patient_id)
    return int(queryset.count())
