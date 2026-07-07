"""Tests for OrderExportHandler."""

from unittest.mock import MagicMock, call, patch

from canvas_sdk.events import EventType

from lab_order_export.handlers.order_export import OrderExportHandler

SECRETS = {
    "ORDER_EXPORT_API_URL": "https://example.com/orders",
    "ORDER_EXPORT_API_TOKEN": "secret-token",
    "ORDER_EXPORT_FILE_PATH": "/tmp/orders.ndjson",
}


class _DoesNotExist(Exception):
    """Stand-in for Command.DoesNotExist during tests."""


def _make_handler() -> OrderExportHandler:
    event = MagicMock()
    event.target.id = "command-uuid"
    handler = OrderExportHandler(event=event)
    handler.secrets = SECRETS
    return handler


def test_responds_to_lab_order_post_commit() -> None:
    assert OrderExportHandler.RESPONDS_TO == EventType.Name(
        EventType.LAB_ORDER_COMMAND__POST_COMMIT
    )


def test_full_flow_delivers_to_both_destinations() -> None:
    handler = _make_handler()

    command = MagicMock()
    command.note.id = "note-uuid"
    command.patient.id = "patient-uuid"
    lab_order = MagicMock()
    payload = {"order": {"id": "order-uuid"}}

    with patch("lab_order_export.handlers.order_export.Command") as mock_command, patch(
        "lab_order_export.handlers.order_export.LabOrder"
    ) as mock_lab_order, patch(
        "lab_order_export.handlers.order_export.build_payload", return_value=payload
    ) as mock_build, patch(
        "lab_order_export.handlers.order_export.post_to_api", return_value=True
    ) as mock_post, patch(
        "lab_order_export.handlers.order_export.append_to_file", return_value=True
    ) as mock_append:
        mock_command.objects.get.return_value = command
        mock_lab_order.objects.filter.return_value.order_by.return_value.first.return_value = (
            lab_order
        )

        effects = handler.compute()

        assert effects == []
        assert mock_command.mock_calls == [call.objects.get(id="command-uuid")]
        assert mock_lab_order.mock_calls == [
            call.objects.filter(
                note__id="note-uuid", patient__id="patient-uuid", deleted=False
            ),
            call.objects.filter().order_by("-created"),
            call.objects.filter().order_by().first(),
        ]
        assert mock_build.mock_calls == [call(lab_order, "command-uuid")]
        assert mock_post.mock_calls == [
            call("https://example.com/orders", "secret-token", payload)
        ]
        assert mock_append.mock_calls == [call("/tmp/orders.ndjson", payload)]


def test_command_not_found_returns_empty() -> None:
    handler = _make_handler()

    with patch("lab_order_export.handlers.order_export.Command") as mock_command, patch(
        "lab_order_export.handlers.order_export.LabOrder"
    ) as mock_lab_order:
        mock_command.DoesNotExist = _DoesNotExist
        mock_command.objects.get.side_effect = _DoesNotExist()

        effects = handler.compute()

        assert effects == []
        assert mock_command.mock_calls == [call.objects.get(id="command-uuid")]
        assert mock_lab_order.mock_calls == []


def test_command_missing_note_returns_empty() -> None:
    handler = _make_handler()

    command = MagicMock()
    command.note = None
    command.patient.id = "patient-uuid"

    with patch("lab_order_export.handlers.order_export.Command") as mock_command, patch(
        "lab_order_export.handlers.order_export.LabOrder"
    ) as mock_lab_order:
        mock_command.objects.get.return_value = command

        effects = handler.compute()

        assert effects == []
        assert mock_lab_order.mock_calls == []


def test_lab_order_not_found_returns_empty() -> None:
    handler = _make_handler()

    command = MagicMock()
    command.note.id = "note-uuid"
    command.patient.id = "patient-uuid"

    with patch("lab_order_export.handlers.order_export.Command") as mock_command, patch(
        "lab_order_export.handlers.order_export.LabOrder"
    ) as mock_lab_order, patch(
        "lab_order_export.handlers.order_export.build_payload"
    ) as mock_build:
        mock_command.objects.get.return_value = command
        mock_lab_order.objects.filter.return_value.order_by.return_value.first.return_value = (
            None
        )

        effects = handler.compute()

        assert effects == []
        assert mock_build.mock_calls == []


def test_both_deliveries_fail_still_returns_empty() -> None:
    handler = _make_handler()

    command = MagicMock()
    command.note.id = "note-uuid"
    command.patient.id = "patient-uuid"
    lab_order = MagicMock()
    payload = {"order": {"id": "order-uuid"}}

    with patch("lab_order_export.handlers.order_export.Command") as mock_command, patch(
        "lab_order_export.handlers.order_export.LabOrder"
    ) as mock_lab_order, patch(
        "lab_order_export.handlers.order_export.build_payload", return_value=payload
    ), patch(
        "lab_order_export.handlers.order_export.post_to_api", return_value=False
    ) as mock_post, patch(
        "lab_order_export.handlers.order_export.append_to_file", return_value=False
    ) as mock_append:
        mock_command.objects.get.return_value = command
        mock_lab_order.objects.filter.return_value.order_by.return_value.first.return_value = (
            lab_order
        )

        effects = handler.compute()

        assert effects == []
        assert mock_post.mock_calls == [
            call("https://example.com/orders", "secret-token", payload)
        ]
        assert mock_append.mock_calls == [call("/tmp/orders.ndjson", payload)]
