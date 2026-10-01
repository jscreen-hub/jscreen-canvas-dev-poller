"""Tests for LabOrderQueueHandler.

Mock-based, matching this workspace's established convention (see
lab-order-export/tests): the Canvas SDK model classes and the
`recompute_and_persist`/`ApplicationNotificationBadge` collaborators are
patched, so these exercise the handler's own wiring/decisions, not a real
database. `logic/queue.py`'s own correctness is covered exhaustively in
tests/logic/test_queue.py without any mocking at all.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

from canvas_sdk.events import EventType

from jlab_order_queue.handlers.lab_order_queue import LabOrderQueueHandler

MODULE = "jlab_order_queue.handlers.lab_order_queue"


class _DoesNotExist(Exception):
    """Stand-in for a model's `.DoesNotExist` during tests."""


def _make_handler(event_type, fields=None, environment=None) -> LabOrderQueueHandler:
    event = MagicMock()
    event.target.id = "command-uuid"
    event.type = event_type
    event.context = {"fields": fields or {}, "note": {"uuid": "note-uuid"}, "patient": {"id": "patient-uuid"}}
    handler = LabOrderQueueHandler(event=event)
    handler.environment = environment or {"INSTALLATION_TIME_ZONE": "UTC"}
    return handler


def _mock_command(dbid=42):
    """A resolvable Command for `_get_entry`'s first step (see its docstring
    for why it resolves the Command before looking up the queue row)."""
    return MagicMock(dbid=dbid)


def test_responds_to_the_expected_lab_order_command_events():
    assert LabOrderQueueHandler.RESPONDS_TO == [
        EventType.Name(EventType.LAB_ORDER_COMMAND__POST_ORIGINATE),
        EventType.Name(EventType.LAB_ORDER_COMMAND__POST_VALIDATION),
        EventType.Name(EventType.LAB_ORDER_COMMAND__POST_COMMIT),
        EventType.Name(EventType.LAB_ORDER_COMMAND__POST_UPDATE),
        EventType.Name(EventType.LAB_ORDER_COMMAND__POST_DELETE),
        EventType.Name(EventType.LAB_ORDER_COMMAND__POST_ENTER_IN_ERROR),
    ]


# -- POST_ORIGINATE -----------------------------------------------------------


def test_on_originate_creates_a_queue_row_and_recomputes():
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_ORIGINATE)

    command = MagicMock()
    command.dbid = 42
    command.patient_id = 7
    command.created = MagicMock()

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls, patch(f"{MODULE}.recompute_and_persist") as mock_recompute, patch(
        f"{MODULE}.ApplicationNotificationBadge"
    ) as mock_badge, patch(f"{MODULE}.actionable_count") as mock_count:
        mock_command_cls.objects.get.return_value = command
        mock_entry_cls.objects.get_or_create.return_value = (MagicMock(), True)
        mock_count.return_value = 3

        effects = handler.compute()

        assert effects == []
        assert mock_command_cls.mock_calls[0] == call.objects.get(id="command-uuid")
        assert mock_entry_cls.objects.get_or_create.call_args.kwargs["command_id"] == 42
        assert (
            mock_entry_cls.objects.get_or_create.call_args.kwargs["defaults"]["patient_id"]
            == 7
        )
        mock_recompute.assert_called_once()
        assert mock_recompute.call_args.args[0] == 7  # patient_dbid
        mock_badge.assert_called_once()
        mock_badge.return_value.broadcast.assert_called_once_with(count=3)


def test_on_originate_is_idempotent_for_a_duplicate_event():
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_ORIGINATE)
    command = MagicMock(dbid=42, patient_id=7)

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls, patch(f"{MODULE}.recompute_and_persist"), patch(
        f"{MODULE}.ApplicationNotificationBadge"
    ), patch(f"{MODULE}.actionable_count", return_value=0):
        mock_command_cls.objects.get.return_value = command
        mock_entry_cls.objects.get_or_create.return_value = (MagicMock(), False)

        effects = handler.compute()

        assert effects == []
        # get_or_create was still called exactly once -- no second row created
        assert mock_entry_cls.objects.get_or_create.call_count == 1


def test_on_originate_fails_safely_without_a_patient():
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_ORIGINATE)
    command = MagicMock(dbid=42, patient_id=None)

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls:
        mock_command_cls.objects.get.return_value = command

        effects = handler.compute()

        assert effects == []
        mock_entry_cls.objects.get_or_create.assert_not_called()


def test_on_originate_logs_and_returns_empty_when_command_missing():
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_ORIGINATE)

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls:
        mock_command_cls.DoesNotExist = _DoesNotExist
        mock_command_cls.objects.get.side_effect = _DoesNotExist()

        assert handler.compute() == []


# -- POST_VALIDATION ----------------------------------------------------------


def test_on_validation_blocks_sign_when_deferred():
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_VALIDATION)
    entry = MagicMock(queue_state="DEFERRED", defer_reason="Eligible 2026-10-02.")

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls:
        mock_command_cls.objects.get.return_value = _mock_command()
        mock_entry_cls.objects.get.return_value = entry

        effects = handler.compute()

        assert len(effects) == 1  # the CommandValidationErrorEffect


def test_on_validation_allows_sign_when_ready():
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_VALIDATION)
    entry = MagicMock(queue_state="READY")

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls:
        mock_command_cls.objects.get.return_value = _mock_command()
        mock_entry_cls.objects.get.return_value = entry
        assert handler.compute() == []


def test_on_validation_allows_sign_when_released():
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_VALIDATION)
    entry = MagicMock(queue_state="RELEASED")

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls:
        mock_command_cls.objects.get.return_value = _mock_command()
        mock_entry_cls.objects.get.return_value = entry
        assert handler.compute() == []


def test_on_validation_fails_safely_when_no_queue_row_exists():
    """Never invent a block for an order this plugin never managed to queue."""
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_VALIDATION)

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls:
        mock_command_cls.objects.get.return_value = _mock_command()
        mock_entry_cls.DoesNotExist = _DoesNotExist
        mock_entry_cls.objects.get.side_effect = _DoesNotExist()
        assert handler.compute() == []


def test_on_validation_fails_safely_when_command_itself_is_gone():
    """`_get_entry` resolves the Command before the queue row -- if even the
    Command can't be found, fail safely rather than erroring."""
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_VALIDATION)

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls:
        mock_command_cls.DoesNotExist = _DoesNotExist
        mock_command_cls.objects.get.side_effect = _DoesNotExist()
        assert handler.compute() == []


def test_get_entry_looks_up_by_raw_command_dbid_not_a_relational_join():
    """Regression test: a direct `JLabOrderQueueEntry.objects.get(command__id=...)`
    join proved unreliable live on jlab-dev (see `_get_entry`'s docstring) --
    it must resolve the Command first and then query by the raw `command_id`
    (dbid) column, the same way every other lookup in this plugin already
    does."""
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_VALIDATION)

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls:
        mock_command_cls.objects.get.return_value = _mock_command(dbid=123)
        mock_entry_cls.objects.get.return_value = MagicMock(queue_state="READY")

        handler.compute()

        mock_command_cls.objects.get.assert_called_once_with(id="command-uuid")
        mock_entry_cls.objects.get.assert_called_once_with(command_id=123)


# -- POST_COMMIT ---------------------------------------------------------------


def test_on_commit_marks_signed_and_recomputes():
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_COMMIT)
    entry = MagicMock(patient_id=7)

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls, patch(
        f"{MODULE}.recompute_and_persist"
    ) as mock_recompute, patch(f"{MODULE}.ApplicationNotificationBadge"), patch(
        f"{MODULE}.actionable_count", return_value=0
    ):
        mock_command_cls.objects.get.return_value = _mock_command()
        mock_entry_cls.objects.get.return_value = entry

        effects = handler.compute()

        assert effects == []
        assert entry.queue_state == "SIGNED"
        assert entry.signed_at is not None
        entry.save.assert_called_once()
        mock_recompute.assert_called_once()
        assert mock_recompute.call_args.args[0] == 7


def test_on_commit_never_touches_the_command_itself():
    """The handler must never call anything that signs/commits the command --
    signing already happened; this only updates the plugin's own row."""
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_COMMIT)
    entry = MagicMock(patient_id=7)

    with patch(f"{MODULE}.JLabOrderQueueEntry") as mock_entry_cls, patch(
        f"{MODULE}.recompute_and_persist"
    ), patch(f"{MODULE}.ApplicationNotificationBadge"), patch(
        f"{MODULE}.actionable_count", return_value=0
    ), patch(f"{MODULE}.CustomCommand") as mock_command_cls:
        mock_command_cls.objects.get.return_value = _mock_command()
        mock_entry_cls.objects.get.return_value = entry

        handler.compute()

        mock_command_cls.objects.get.return_value.commit.assert_not_called()


# -- POST_UPDATE ----------------------------------------------------------------


def test_on_update_refreshes_the_ordering_provider():
    handler = _make_handler(
        EventType.LAB_ORDER_COMMAND__POST_UPDATE,
        fields={"ordering_provider": {"value": "staff-2"}},
    )
    entry = MagicMock(queue_state="DEFERRED", ordering_provider_id=99)
    staff = MagicMock(dbid=55)

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls, patch(f"{MODULE}.CustomStaff") as mock_staff_cls:
        mock_command_cls.objects.get.return_value = _mock_command()
        mock_entry_cls.objects.get.return_value = entry
        mock_staff_cls.objects.get.return_value = staff

        assert handler.compute() == []
        assert entry.ordering_provider_id == 55
        entry.save.assert_called_once()


def test_on_update_does_not_touch_queue_state_or_eligible_date():
    handler = _make_handler(
        EventType.LAB_ORDER_COMMAND__POST_UPDATE, fields={"ordering_provider": {}}
    )
    entry = MagicMock(queue_state="DEFERRED", ordering_provider_id=None)

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls:
        mock_command_cls.objects.get.return_value = _mock_command()
        mock_entry_cls.objects.get.return_value = entry
        handler.compute()
        # no eligible_date/queue_state assignment happened
        assert entry.queue_state == "DEFERRED"


def test_on_update_is_a_no_op_for_an_already_resolved_order():
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_UPDATE)
    entry = MagicMock(queue_state="SIGNED")

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls:
        mock_command_cls.objects.get.return_value = _mock_command()
        mock_entry_cls.objects.get.return_value = entry
        handler.compute()
        entry.save.assert_not_called()


# -- POST_DELETE / POST_ENTER_IN_ERROR ------------------------------------------


def test_post_delete_marks_cancelled_and_recomputes():
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_DELETE)
    entry = MagicMock(queue_state="DEFERRED", patient_id=7)

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls, patch(
        f"{MODULE}.recompute_and_persist"
    ) as mock_recompute, patch(f"{MODULE}.ApplicationNotificationBadge"), patch(
        f"{MODULE}.actionable_count", return_value=0
    ):
        mock_command_cls.objects.get.return_value = _mock_command()
        mock_entry_cls.objects.get.return_value = entry

        handler.compute()

        assert entry.queue_state == "CANCELLED"
        entry.save.assert_called_once()
        mock_recompute.assert_called_once()


def test_post_enter_in_error_behaves_the_same_as_delete():
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_ENTER_IN_ERROR)
    entry = MagicMock(queue_state="SIGNED", patient_id=7)

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls, patch(
        f"{MODULE}.recompute_and_persist"
    ) as mock_recompute, patch(f"{MODULE}.ApplicationNotificationBadge"), patch(
        f"{MODULE}.actionable_count", return_value=0
    ):
        mock_command_cls.objects.get.return_value = _mock_command()
        mock_entry_cls.objects.get.return_value = entry

        handler.compute()

        assert entry.queue_state == "CANCELLED"
        mock_recompute.assert_called_once()


def test_cancellation_is_idempotent_for_an_already_cancelled_order():
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_DELETE)
    entry = MagicMock(queue_state="CANCELLED")

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls, patch(
        f"{MODULE}.JLabOrderQueueEntry"
    ) as mock_entry_cls, patch(f"{MODULE}.recompute_and_persist") as mock_recompute:
        mock_command_cls.objects.get.return_value = _mock_command()
        mock_entry_cls.objects.get.return_value = entry
        handler.compute()
        entry.save.assert_not_called()
        mock_recompute.assert_not_called()


# -- fail-safe behavior ---------------------------------------------------------


def test_unexpected_exceptions_are_swallowed_not_propagated():
    handler = _make_handler(EventType.LAB_ORDER_COMMAND__POST_ORIGINATE)

    with patch(f"{MODULE}.CustomCommand") as mock_command_cls:
        mock_command_cls.objects.get.side_effect = RuntimeError("boom")
        # must not raise
        assert handler.compute() == []
