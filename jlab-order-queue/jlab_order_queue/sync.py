"""Bridges `JLabOrderQueueEntry` rows and the pure `logic.queue` module.

Both the real-time event handler and the CronTask need to: load a patient's
current queue rows, feed them to `logic.queue.recompute_patient_queue`, and
persist whatever changed. This module is the one place that does that
translation, so the two call sites can't drift out of sync with each other
on how a row maps to a `QueuedOrder` and back.

Rows are keyed internally by `Command.dbid` (an integer, via the
`OneToOneField(..., to_field="dbid")` on `JLabOrderQueueEntry.command`) --
not `Command.id` (the external UUID) -- because that is the actual primary
key column Django gives the table. Handlers resolve the UUID from a Canvas
event into a `Command`/dbid exactly once, at the point of first contact with
the event; everything downstream, including this module, deals only in
dbids. `QueuedOrder.command_id` (typed as `str` in the pure logic layer,
which has no opinion on what kind of id it is) is always
`str(<that dbid>)` here.
"""

from __future__ import annotations

from datetime import date, datetime

from jlab_order_queue.logic.queue import (
    QueuedOrder,
    RecomputeResult,
    recompute_patient_queue,
)
from jlab_order_queue.models import JLabOrderQueueEntry


def _to_queued_order(entry: JLabOrderQueueEntry) -> QueuedOrder:
    return QueuedOrder(
        command_id=str(entry.command_id),
        patient_id=str(entry.patient_id),
        received_at=entry.received_at,
        queue_state=entry.queue_state,
        eligible_date=entry.eligible_date,
        priority=entry.priority,
    )


def recompute_and_persist(
    patient_dbid: int, today: date, now: datetime
) -> list[RecomputeResult]:
    """Recompute one patient's whole queue and write every row back.

    Safe to call from any handler at any time: it is a read-modify-write
    over the current rows, and `recompute_patient_queue` is idempotent by
    construction, so calling this twice with nothing changed in between
    (duplicate events, overlapping cron ticks) produces identical output
    and does not re-fire `released_at`/badge updates. Every row's
    `last_processed_at` advances on every call regardless -- that field
    exists specifically to show when this patient's queue was last
    evaluated, including passes that changed nothing.
    """
    entries = list(JLabOrderQueueEntry.objects.filter(patient_id=patient_dbid))
    by_dbid = {entry.command_id: entry for entry in entries}
    queued = [_to_queued_order(entry) for entry in entries]

    results = recompute_patient_queue(queued, today)

    for result in results:
        row = by_dbid.get(int(result.command_id))
        if row is None:
            continue  # defensive: results are always 1:1 with the input rows
        row.queue_state = result.queue_state
        row.eligible_date = result.eligible_date
        row.defer_reason = result.defer_reason
        row.blocking_order_id = (
            int(result.blocking_order_id) if result.blocking_order_id else None
        )
        if result.became_available:
            row.released_at = now
        row.save()

    return results
