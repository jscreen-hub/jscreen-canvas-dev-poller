"""Real-time queue maintenance for JLab routine Lab Order commands.

Responds to the Lab Order command's own lifecycle, not the surrounding
note's lock/sign state -- a Lab Order command can be staged and signed
(`sign_action`) independently of the rest of its note. See DISCOVERY.md
§2-3 for the full citation trail.

- `POST_ORIGINATE` — a new order is staged. Create its queue row and decide
  READY vs DEFERRED for it and recheck nothing else (nothing else changed).
- `POST_VALIDATION` — fires on every render/edit of the staged command.
  Block `sign_action` with a `CommandValidationErrorEffect` while DEFERRED;
  return no effect while READY/RELEASED, which is what lets the Sign
  button re-enable the moment this plugin (or the CronTask) promotes the
  row -- no separate "unblock" effect exists or is needed. See
  DISCOVERY.md §5 for why this is the documented, supported mechanism
  (`/sdk/effect-command-validation/`) and not a workaround.
- `POST_COMMIT` — the physician signed. Mark SIGNED and recompute the rest
  of the patient's queue (self-healing; normally a no-op since a signed
  order keeps its day).
- `POST_UPDATE` — the staged command was edited. Only the denormalized
  ordering-provider copy is refreshed; queue_state/eligible_date/
  received_at are never touched by an edit.
- `POST_DELETE` / `POST_ENTER_IN_ERROR` — the order was removed while
  staged, or voided after being signed. Both are treated as "this order no
  longer counts" (CANCELLED) and both free the day for recomputation --
  see the docstring on `_cancel_and_recompute` for why ENTER_IN_ERROR is
  handled the same as DELETE rather than left locked.

Every branch is wrapped so an unexpected error is logged and swallowed
rather than propagated: Step 8 requires this plugin to fail safely, never
silently sign, cancel, delete, or otherwise alter the clinical order.
PRE_DELETE is deliberately not subscribed to -- the only effect it accepts
(`CommandValidationErrorEffect`) *blocks* the deletion, which this plugin
must never do (staff must always be able to remove a staged order).
"""

from __future__ import annotations

from datetime import date, datetime, timezone as dt_timezone
from typing import Any

from canvas_sdk.commands.validation import CommandValidationErrorEffect
from canvas_sdk.effects import Effect
from canvas_sdk.effects.application_notification_badge import (
    ApplicationNotificationBadge,
)
from canvas_sdk.events import EventType
from canvas_sdk.handlers import BaseHandler
from logger import log

from jlab_order_queue.badge import actionable_count
from jlab_order_queue.config import GLOBAL_APP_IDENTIFIER, instance_timezone
from jlab_order_queue.logic.queue import (
    CANCELLED,
    DEFERRED,
    NEW,
    READY,
    RELEASED,
    ROUTINE,
    SIGNED,
    TERMINAL_STATES,
    business_date,
)
from jlab_order_queue.models import CustomCommand, CustomStaff, JLabOrderQueueEntry
from jlab_order_queue.sync import recompute_and_persist


class LabOrderQueueHandler(BaseHandler):
    RESPONDS_TO = [
        EventType.Name(EventType.LAB_ORDER_COMMAND__POST_ORIGINATE),
        EventType.Name(EventType.LAB_ORDER_COMMAND__POST_VALIDATION),
        EventType.Name(EventType.LAB_ORDER_COMMAND__POST_COMMIT),
        EventType.Name(EventType.LAB_ORDER_COMMAND__POST_UPDATE),
        EventType.Name(EventType.LAB_ORDER_COMMAND__POST_DELETE),
        EventType.Name(EventType.LAB_ORDER_COMMAND__POST_ENTER_IN_ERROR),
    ]

    def compute(self) -> list[Effect]:
        command_id = str(self.event.target.id)
        try:
            return self._dispatch(command_id)
        except Exception:
            log.exception(
                "[jlab_order_queue] unhandled error processing "
                f"{EventType.Name(self.event.type)} for command {command_id}"
            )
            return []

    def _dispatch(self, command_id: str) -> list[Effect]:
        event_type = self.event.type
        if event_type == EventType.LAB_ORDER_COMMAND__POST_ORIGINATE:
            return self._on_originate(command_id)
        if event_type == EventType.LAB_ORDER_COMMAND__POST_VALIDATION:
            return self._on_validation(command_id)
        if event_type == EventType.LAB_ORDER_COMMAND__POST_COMMIT:
            return self._on_commit(command_id)
        if event_type == EventType.LAB_ORDER_COMMAND__POST_UPDATE:
            return self._on_update(command_id)
        if event_type in (
            EventType.LAB_ORDER_COMMAND__POST_DELETE,
            EventType.LAB_ORDER_COMMAND__POST_ENTER_IN_ERROR,
        ):
            return self._cancel_and_recompute(command_id)
        return []  # pragma: no cover - RESPONDS_TO guarantees one of the above

    # -- event handlers -------------------------------------------------

    def _on_originate(self, command_id: str) -> list[Effect]:
        command = self._get_command(command_id)
        if command is None:
            return []
        if command.patient_id is None:
            log.error(
                f"[jlab_order_queue] command {command_id} has no patient; "
                "cannot queue it safely, leaving it unmanaged"
            )
            return []

        now = datetime.now(dt_timezone.utc)
        tz = instance_timezone(self.environment)
        today = business_date(now, tz)

        # get_or_create keys on the command's own dbid (the table's primary
        # key), so a duplicate POST_ORIGINATE for the same command can never
        # create a second row (Step 8 "duplicate events/idempotency").
        entry, created = JLabOrderQueueEntry.objects.get_or_create(
            command_id=command.dbid,
            defaults={
                "patient_id": command.patient_id,
                "received_at": command.created,
                "priority": ROUTINE,
                "queue_state": NEW,
                "eligible_date": today,
                "ordering_provider_id": self._resolve_ordering_provider_dbid(
                    self.event.context.get("fields", {})
                ),
            },
        )
        if not created:
            log.info(
                f"[jlab_order_queue] duplicate originate for command {command_id}; "
                "queue row already exists, recomputing only"
            )

        log.info(
            f"[jlab_order_queue] order detected: command={command_id} "
            f"patient_dbid={command.patient_id} received_at={command.created.isoformat()}"
        )

        self._recompute_and_notify(command.patient_id, today, now)
        return []  # the physician must still click Sign themselves

    def _on_validation(self, command_id: str) -> list[Effect]:
        entry = self._get_entry(command_id)
        if entry is None:
            # Fail safely: if we cannot find our own queue row, do not
            # invent a block. An order this plugin never managed to queue
            # must not be stuck un-signable because of it.
            return []
        if entry.queue_state in (READY, RELEASED):
            return []
        if entry.queue_state == DEFERRED:
            log.info(
                f"[jlab_order_queue] queue decision: command={command_id} "
                f"deferred, eligible={entry.eligible_date.isoformat()}"
            )
            return [
                CommandValidationErrorEffect()
                .add_error(entry.defer_reason or "This order is queued; see the JLab Order Queue app.")
                .apply()
            ]
        # CANCELLED/SIGNED/NEW shouldn't realistically reach POST_VALIDATION
        # (the first two no longer accept edits; NEW is resolved within the
        # same transaction as origination) -- never block in these cases.
        return []

    def _on_commit(self, command_id: str) -> list[Effect]:
        entry = self._get_entry(command_id)
        if entry is None:
            log.error(
                f"[jlab_order_queue] command {command_id} committed but has no "
                "queue row; nothing to reconcile"
            )
            return []
        now = datetime.now(dt_timezone.utc)
        log.info(f"[jlab_order_queue] signing detected: command={command_id}")
        entry.queue_state = SIGNED
        entry.signed_at = now
        entry.save()

        tz = instance_timezone(self.environment)
        today = business_date(now, tz)
        self._recompute_and_notify(entry.patient_id, today, now)
        return []

    def _on_update(self, command_id: str) -> list[Effect]:
        entry = self._get_entry(command_id)
        if entry is None:
            return []
        if entry.queue_state in TERMINAL_STATES:
            return []  # an edit to an already-resolved order changes nothing here
        new_provider_dbid = self._resolve_ordering_provider_dbid(
            self.event.context.get("fields", {})
        )
        if new_provider_dbid != entry.ordering_provider_id:
            log.info(
                f"[jlab_order_queue] ordering provider changed: command={command_id}"
            )
            entry.ordering_provider_id = new_provider_dbid
            entry.save()
        return []

    def _cancel_and_recompute(self, command_id: str) -> list[Effect]:
        """Shared path for POST_DELETE and POST_ENTER_IN_ERROR.

        Both mean "this order no longer counts" -- a deleted staged order
        never happened, and an entered-in-error committed order is voided
        as if it never happened either. Either way the day it held should
        be handed back to the next deferred order for this patient, which
        is why both free the slot and recompute rather than leaving later
        orders waiting behind a day that is no longer spoken for.
        """
        entry = self._get_entry(command_id)
        if entry is None:
            return []
        if entry.queue_state == CANCELLED:
            # Idempotency: a duplicate delete/enter-in-error event for an
            # already-cancelled row is a no-op. SIGNED is deliberately NOT
            # treated as already-resolved here -- SIGNED -> CANCELLED via
            # ENTER_IN_ERROR is exactly the transition this path exists for.
            return []

        log.info(f"[jlab_order_queue] cancellation detected: command={command_id}")
        patient_dbid = entry.patient_id
        entry.queue_state = CANCELLED
        entry.save()

        now = datetime.now(dt_timezone.utc)
        tz = instance_timezone(self.environment)
        today = business_date(now, tz)
        self._recompute_and_notify(patient_dbid, today, now)
        return []

    # -- helpers ----------------------------------------------------------

    def _get_command(self, command_id: str) -> CustomCommand | None:
        try:
            command: CustomCommand = CustomCommand.objects.get(id=command_id)
            return command
        except CustomCommand.DoesNotExist:
            log.error(f"[jlab_order_queue] no Command found for id {command_id}")
            return None

    def _get_entry(self, command_id: str) -> JLabOrderQueueEntry | None:
        """Look up this command's queue row.

        Deliberately resolves the Command first and then looks the row up by
        its raw `command_id` (dbid) column, instead of a single query
        filtering across the relation (`JLabOrderQueueEntry.objects.get(
        command__id=command_id)`). That direct-join form was the original
        implementation and proved unreliable on jlab-dev: confirmed live, it
        raised DoesNotExist for rows that demonstrably still existed (the
        same rows were found correctly moments later by `recompute_and_persist`'s
        plain `filter(patient_id=...)` query, which is what kept FIFO
        eligible-date sequencing correct even while this lookup was failing
        for those same rows). Every other lookup in this plugin already
        queries by a raw dbid column (`command_id=`, `patient_id=`) rather
        than through a cross-model relation, and none of those have shown
        this failure -- so this mirrors that proven-reliable pattern instead
        of root-causing the relational join itself.
        """
        command = self._get_command(command_id)
        if command is None:
            return None
        try:
            entry: JLabOrderQueueEntry = JLabOrderQueueEntry.objects.get(
                command_id=command.dbid
            )
            return entry
        except JLabOrderQueueEntry.DoesNotExist:
            log.error(f"[jlab_order_queue] no queue entry found for command {command_id}")
            return None

    def _resolve_ordering_provider_dbid(self, fields: dict[str, Any]) -> int | None:
        """Best-effort: resolve the lab order's ordering provider to a Staff dbid.

        The exact shape of `fields["ordering_provider"]` in the event context
        is not pinned down in the bundled SDK docs (the per-command-type
        context reference lists the key but not its value shape). This tries
        the keys a Canvas coded-field dict commonly uses and gives up safely
        -- the ordering provider is display-only data for this plugin (the
        chosen authorization policy does not filter by it), so never raising
        here matters far more than always resolving it.

        NOTE: confirm the exact shape against a live instance during UAT,
        same caveat `lab-order-export` already carries for its own
        best-effort resolutions.
        """
        provider = fields.get("ordering_provider") or {}
        if not isinstance(provider, dict):
            return None
        staff_id = provider.get("value") or provider.get("id") or provider.get("key")
        if not staff_id:
            return None
        try:
            dbid: int = CustomStaff.objects.get(id=staff_id).dbid
            return dbid
        except CustomStaff.DoesNotExist:
            log.info(
                f"[jlab_order_queue] ordering provider {staff_id} not found; "
                "leaving provider unset"
            )
            return None

    def _recompute_and_notify(
        self, patient_dbid: int, today: date, now: datetime
    ) -> None:
        recompute_and_persist(patient_dbid, today, now)
        # Must agree with `apps/queue_app.py`'s `compute_notification_badge()`
        # on what the badge counts -- see badge.py.
        ApplicationNotificationBadge(GLOBAL_APP_IDENTIFIER).broadcast(
            count=actionable_count()
        )
