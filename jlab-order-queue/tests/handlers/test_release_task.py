"""Tests for ReleaseDeferredOrders (the CronTask)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from jlab_order_queue.handlers.release_task import ReleaseDeferredOrders

MODULE = "jlab_order_queue.handlers.release_task"


def _make_task(environment=None) -> ReleaseDeferredOrders:
    task = ReleaseDeferredOrders(event=MagicMock())
    task.environment = environment or {"INSTALLATION_TIME_ZONE": "UTC"}
    return task


def test_schedule_is_a_five_field_cron_string():
    parts = ReleaseDeferredOrders.SCHEDULE.split()
    assert len(parts) == 5


def test_sweeps_every_patient_with_a_deferred_order():
    from datetime import date

    task = _make_task()
    result_promoted = MagicMock(became_available=True, command_id="cmd-1", queue_state="RELEASED")
    result_untouched = MagicMock(became_available=False)

    with patch(f"{MODULE}.JLabOrderQueueEntry") as mock_entry_cls, patch(
        f"{MODULE}.recompute_and_persist"
    ) as mock_recompute, patch(
        f"{MODULE}.ApplicationNotificationBadge"
    ) as mock_badge, patch(f"{MODULE}.actionable_count", return_value=2):
        mock_entry_cls.objects.filter.return_value.values_list.return_value.distinct.return_value = [
            7,
            9,
        ]
        mock_recompute.side_effect = [[result_promoted], [result_untouched]]

        effects = task.execute()

        assert effects == []
        assert mock_recompute.call_count == 2
        patients_called = [c.args[0] for c in mock_recompute.call_args_list]
        assert patients_called == [7, 9]
        # every call shares the same `today`/`now` across patients in one sweep
        assert isinstance(mock_recompute.call_args_list[0].args[1], date)
        mock_badge.return_value.broadcast.assert_called_once_with(count=2)


def test_no_badge_broadcast_when_nothing_was_released():
    task = _make_task()
    result_untouched = MagicMock(became_available=False)

    with patch(f"{MODULE}.JLabOrderQueueEntry") as mock_entry_cls, patch(
        f"{MODULE}.recompute_and_persist"
    ) as mock_recompute, patch(f"{MODULE}.ApplicationNotificationBadge") as mock_badge:
        mock_entry_cls.objects.filter.return_value.values_list.return_value.distinct.return_value = [
            7
        ]
        mock_recompute.return_value = [result_untouched]

        task.execute()

        mock_badge.return_value.broadcast.assert_not_called()


def test_one_patients_failure_does_not_stop_the_sweep():
    task = _make_task()
    result_promoted = MagicMock(became_available=True, command_id="cmd-2", queue_state="RELEASED")

    with patch(f"{MODULE}.JLabOrderQueueEntry") as mock_entry_cls, patch(
        f"{MODULE}.recompute_and_persist"
    ) as mock_recompute, patch(
        f"{MODULE}.ApplicationNotificationBadge"
    ) as mock_badge, patch(f"{MODULE}.actionable_count", return_value=1):
        mock_entry_cls.objects.filter.return_value.values_list.return_value.distinct.return_value = [
            7,
            9,
        ]
        mock_recompute.side_effect = [RuntimeError("boom"), [result_promoted]]

        effects = task.execute()

        assert effects == []
        assert mock_recompute.call_count == 2
        mock_badge.return_value.broadcast.assert_called_once_with(count=1)


def test_never_returns_effects_that_sign_anything():
    """The CronTask must never itself sign/commit a command -- it only ever
    returns []; releasing means clearing a validation block on next render."""
    task = _make_task()
    with patch(f"{MODULE}.JLabOrderQueueEntry") as mock_entry_cls, patch(
        f"{MODULE}.recompute_and_persist", return_value=[]
    ):
        mock_entry_cls.objects.filter.return_value.values_list.return_value.distinct.return_value = []
        assert task.execute() == []
