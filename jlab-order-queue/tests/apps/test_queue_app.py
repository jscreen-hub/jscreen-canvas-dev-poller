"""Tests for GlobalQueueApp / PatientQueueApp."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from jlab_order_queue.apps.queue_app import GlobalQueueApp, PatientQueueApp

MODULE = "jlab_order_queue.apps.queue_app"


def _make_app(cls, context=None):
    event = MagicMock()
    event.context = context or {}
    return cls(event=event)


def test_global_app_opens_the_queue_page():
    app = _make_app(GlobalQueueApp)
    effect = app.on_open()
    assert "/plugin-io/api/jlab_order_queue/queue" in str(effect)


def test_global_app_badge_is_the_whole_instance_actionable_count():
    app = _make_app(GlobalQueueApp)
    with patch(f"{MODULE}.actionable_count") as mock_count:
        mock_count.return_value = 5
        assert app.compute_notification_badge() == 5
        mock_count.assert_called_once_with()


def test_patient_app_includes_the_patient_id_in_the_url():
    app = _make_app(PatientQueueApp, context={"patient": {"id": "pat-123"}})
    effect = app.on_open()
    assert "patient_id=pat-123" in str(effect)


def test_patient_app_badge_is_scoped_to_that_patient():
    app = _make_app(PatientQueueApp, context={"patient": {"id": "pat-123"}})
    with patch(f"{MODULE}.actionable_count") as mock_count:
        mock_count.return_value = 2
        assert app.compute_notification_badge() == 2
        mock_count.assert_called_once_with("pat-123")


def test_patient_app_badge_is_none_without_a_patient_in_context():
    app = _make_app(PatientQueueApp, context={})
    assert app.compute_notification_badge() is None
