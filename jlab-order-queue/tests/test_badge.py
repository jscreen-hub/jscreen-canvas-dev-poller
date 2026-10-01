"""Tests for the shared notification-badge count.

Per the confirmed design decision, the badge counts orders actionable right
now (READY + RELEASED -- both mean the Sign button is enabled), not
DEFERRED. See `badge.py`'s module docstring for why, and
`apps/queue_app.py` / `handlers/lab_order_queue.py` /
`handlers/release_task.py` for the three call sites that must all agree
with this.
"""

from __future__ import annotations

from unittest.mock import patch

from jlab_order_queue.badge import actionable_count

MODULE = "jlab_order_queue.badge"


def test_actionable_count_filters_on_ready_and_released_not_deferred():
    with patch(f"{MODULE}.JLabOrderQueueEntry") as mock_entry_cls:
        mock_entry_cls.objects.filter.return_value.count.return_value = 3
        assert actionable_count() == 3
        mock_entry_cls.objects.filter.assert_called_once_with(
            queue_state__in=("READY", "RELEASED")
        )


def test_actionable_count_scopes_to_a_patient_when_given():
    with patch(f"{MODULE}.JLabOrderQueueEntry") as mock_entry_cls:
        mock_entry_cls.objects.filter.return_value.filter.return_value.count.return_value = 1
        assert actionable_count("pat-1") == 1
        mock_entry_cls.objects.filter.return_value.filter.assert_called_once_with(
            patient__id="pat-1"
        )
