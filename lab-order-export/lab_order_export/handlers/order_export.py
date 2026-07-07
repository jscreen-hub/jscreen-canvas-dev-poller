"""Export committed lab (and genetic) orders to an external system.

Fires on LAB_ORDER_COMMAND__POST_COMMIT — i.e. the moment a provider signs a Lab
Order command in a note. Every committed order is exported with all of its tests;
there is no filtering. Genetic panels are placed as lab orders and so flow through
this same handler with no special handling.
"""

from canvas_sdk.effects import Effect
from canvas_sdk.events import EventType
from canvas_sdk.handlers import BaseHandler
from canvas_sdk.v1.data.command import Command
from canvas_sdk.v1.data.lab import LabOrder
from logger import log

from lab_order_export.utils.delivery import append_to_file, post_to_api
from lab_order_export.utils.payload import build_payload


class OrderExportHandler(BaseHandler):
    """On lab order commit, deliver a structured payload to the API and audit file."""

    RESPONDS_TO = EventType.Name(EventType.LAB_ORDER_COMMAND__POST_COMMIT)

    def _resolve_lab_order(self, command_id: str) -> LabOrder | None:
        """Resolve the committed LabOrder for the triggering command.

        The event target is the command id. There is no direct command->LabOrder
        foreign key, so the order is resolved by its note + patient, taking the
        most recently created non-deleted order for that note. A single note can
        in principle hold multiple lab orders; committing one command produces one
        new LabOrder, so the newest row for the note is the one just committed.

        NOTE: confirm this correlation against a live instance during UAT.
        """
        try:
            command = Command.objects.get(id=command_id)
        except Command.DoesNotExist:
            log.error(f"[lab-order-export] no Command found for id {command_id}")
            return None

        note = command.note
        patient = command.patient
        if note is None or patient is None:
            log.error(
                f"[lab-order-export] command {command_id} missing note or patient"
            )
            return None

        lab_order = (
            LabOrder.objects.filter(
                note__id=note.id,
                patient__id=patient.id,
                deleted=False,
            )
            .order_by("-created")
            .first()
        )

        if lab_order is None:
            log.error(
                f"[lab-order-export] no LabOrder found for note {note.id} "
                f"(command {command_id})"
            )
        return lab_order

    def compute(self) -> list[Effect]:
        """Build and deliver the export payload. Produces no Canvas effect."""
        command_id = str(self.event.target.id)
        log.info(f"[lab-order-export] lab order committed (command {command_id})")

        lab_order = self._resolve_lab_order(command_id)
        if lab_order is None:
            return []

        payload = build_payload(lab_order, command_id)

        # Deliver to both destinations. Each is best-effort and self-contained:
        # a failed API POST still leaves the order recorded in the audit file.
        api_ok = post_to_api(
            self.secrets.get("ORDER_EXPORT_API_URL"),
            self.secrets.get("ORDER_EXPORT_API_TOKEN"),
            payload,
        )
        file_ok = append_to_file(
            self.secrets.get("ORDER_EXPORT_FILE_PATH"),
            payload,
        )

        if not api_ok and not file_ok:
            log.error(
                f"[lab-order-export] order {payload['order']['id']} was not "
                "delivered to any destination"
            )

        return []
