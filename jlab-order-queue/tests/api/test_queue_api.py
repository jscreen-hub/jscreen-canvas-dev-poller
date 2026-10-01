"""Tests for the JLab Order Queue SimpleAPI.

Covers the authorization requirement (Step 7 scenario 15/16, per the
confirmed policy: any logged-in Canvas staff session, no per-provider
filtering) and the row serialization/filtering/sorting the UI depends on.
"""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import MagicMock, PropertyMock, patch

from canvas_sdk.handlers.simple_api import StaffSessionAuthMixin

from jlab_order_queue.api.queue_api import QueueAPI, _apply_filters, _serialize_entry, _sort
from jlab_order_queue.models import CustomCommand

MODULE = "jlab_order_queue.api.queue_api"


def _make_api(query_params=None) -> QueueAPI:
    """Construct a `QueueAPI` instance without going through SimpleAPI's own
    framework-managed `__init__` (which expects a live request context) --
    `data()`/`page()` only touch `self.request`, so this is enough to test
    them directly."""
    instance = QueueAPI.__new__(QueueAPI)
    instance.request = MagicMock()
    instance.request.query_params = query_params if query_params is not None else {}
    return instance


# -- authorization (scenario 15/16) -------------------------------------------


def test_queue_api_is_gated_on_a_staff_session():
    """Per the confirmed policy, authorization is role-only: any logged-in
    staff session may reach this API at all (StaffSessionAuthMixin rejects
    anonymous and patient-portal sessions); there is no further per-provider
    filtering of *which* staff member can see *which* rows."""
    assert issubclass(QueueAPI, StaffSessionAuthMixin)


# -- serialization -------------------------------------------------------------


def test_serialize_entry_produces_the_expected_shape():
    entry = MagicMock()
    entry.command_id = 42
    entry.command.id = "cmd-uuid"
    entry.patient.id = "pat-uuid"
    entry.patient.first_name = "Ada"
    entry.patient.last_name = "Lovelace"
    entry.patient.mrn = "MRN-9"
    entry.received_at.isoformat.return_value = "2026-10-01T09:00:00+00:00"
    entry.priority = "ROUTINE"
    entry.queue_state = "DEFERRED"
    entry.eligible_date.isoformat.return_value = "2026-10-02"
    entry.released_at = None
    entry.signed_at = None
    entry.ordering_provider.first_name = "Gene"
    entry.ordering_provider.last_name = "Counselor"
    entry.defer_reason = "Another routine JLab lab order..."

    row = _serialize_entry(entry)

    assert row["command_id"] == "cmd-uuid"
    assert row["patient_id"] == "pat-uuid"
    assert row["patient_name"] == "Ada Lovelace"
    assert row["mrn"] == "MRN-9"
    assert row["queue_state"] == "DEFERRED"
    assert row["eligible_date"] == "2026-10-02"
    assert row["released_at"] is None
    assert row["provider_name"] == "Gene Counselor"


def test_serialize_entry_handles_no_patient_or_provider():
    entry = MagicMock()
    entry.command_id = None
    entry.patient = None
    entry.ordering_provider = None
    entry.received_at = None
    entry.eligible_date = None
    entry.released_at = None
    entry.signed_at = None
    entry.priority = "ROUTINE"
    entry.queue_state = "DEFERRED"
    entry.defer_reason = ""

    row = _serialize_entry(entry)

    assert row["patient_name"] is None
    assert row["mrn"] is None
    assert row["provider_name"] is None


def test_serialize_entry_survives_a_deleted_underlying_command():
    """Regression test for a real bug found live on jlab-dev: a row whose
    Command was replaced/deleted while staged raised
    `CustomCommand.DoesNotExist` on `entry.command` and took the *entire*
    `/queue/data` response down with it -- one stale row broke every row.
    This field must degrade to `None` instead (see `_safe_related`)."""
    entry = MagicMock()
    entry.command_id = 123
    type(entry).command = PropertyMock(side_effect=CustomCommand.DoesNotExist)
    entry.patient = MagicMock(id="pat-1", first_name="Ada", last_name="Lovelace", mrn="MRN-1")
    entry.ordering_provider = None
    entry.received_at = None
    entry.eligible_date = None
    entry.released_at = None
    entry.signed_at = None
    entry.priority = "ROUTINE"
    entry.queue_state = "DEFERRED"
    entry.defer_reason = "stale"

    row = _serialize_entry(entry)

    assert row["command_id"] is None
    assert row["patient_name"] == "Ada Lovelace"


# -- filtering ------------------------------------------------------------------


class _PlainParams(dict):
    """A query-param mapping with no `.getlist` -- the common case."""


class _QueryDictParams(dict):
    """Stands in for Django's QueryDict, which supports repeated keys."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._lists = {k: [v] for k, v in kwargs.items()}

    def getlist(self, key):
        return self._lists.get(key, [])


def test_default_filter_excludes_cancelled():
    queryset = MagicMock()
    _apply_filters(queryset, _PlainParams())
    queryset.exclude.assert_called_once_with(queue_state="CANCELLED")
    queryset.filter.assert_not_called()


def test_explicit_state_filter_is_applied():
    queryset = MagicMock()
    _apply_filters(queryset, _QueryDictParams(state="deferred"))
    queryset.filter.assert_any_call(queue_state__in=["DEFERRED"])


def test_patient_id_and_provider_id_and_date_filters_are_applied():
    queryset = MagicMock()
    # No `state` param -> the default-exclude-CANCELLED branch runs first;
    # make every chained call land back on the same mock either way.
    queryset.filter.return_value = queryset
    queryset.exclude.return_value = queryset
    params = _PlainParams(provider_id="staff-1", patient_id="pat-1", date="2026-10-02")
    _apply_filters(queryset, params)
    queryset.filter.assert_any_call(ordering_provider__id="staff-1")
    queryset.filter.assert_any_call(patient__id="pat-1")
    queryset.filter.assert_any_call(eligible_date="2026-10-02")


# -- sorting --------------------------------------------------------------------


def test_sort_orders_by_eligible_date_then_received_at():
    queryset = MagicMock()
    _sort(queryset)
    queryset.order_by.assert_called_once_with("eligible_date", "received_at")


# -- QueueAPI.data() ------------------------------------------------------------
#
# Regression coverage for a real bug found live on jlab-dev: `select_related()`
# on `ordering_provider` (nullable at the DB level, but CustomModel fields
# can't declare `null=True`) silently dropped every row with a NULL
# `ordering_provider` via an INNER JOIN Django generated because it believed
# the FK was required. The notification badge (`apps/queue_app.py`, a plain
# `.filter().count()`, no join) kept showing the real count while this
# endpoint returned zero rows for every filter -- exactly what made it look
# like "the table is empty" when it demonstrably was not.


def test_data_does_not_use_select_related():
    """Locks in the fix: no query here may JOIN through a nullable FK the way
    `select_related()` on `ordering_provider`/`patient`/`command` did."""
    api_instance = _make_api()
    with patch(f"{MODULE}.JLabOrderQueueEntry") as mock_entry_cls:
        mock_entry_cls.objects.count.return_value = 0
        mock_entry_cls.objects.all.return_value.exclude.return_value.order_by.return_value = []

        api_instance.data()

        mock_entry_cls.objects.all.return_value.select_related.assert_not_called()


def test_data_includes_rows_with_no_ordering_provider():
    """The actual symptom: a row with a NULL ordering_provider must still be
    returned, not silently dropped."""
    entry = MagicMock(
        command_id=1,
        patient=MagicMock(id="pat-1", first_name="Ada", last_name="Lovelace", mrn="MRN-1"),
        ordering_provider=None,
        received_at=None,
        eligible_date=None,
        released_at=None,
        signed_at=None,
        priority="ROUTINE",
        queue_state="DEFERRED",
        defer_reason="",
    )
    entry.command.id = "cmd-1"
    api_instance = _make_api()

    with patch(f"{MODULE}.JLabOrderQueueEntry") as mock_entry_cls:
        mock_entry_cls.objects.count.return_value = 1
        mock_entry_cls.objects.all.return_value.exclude.return_value.order_by.return_value = [
            entry
        ]

        responses = api_instance.data()

        assert responses[0].status_code == HTTPStatus.OK
        assert b'"provider_name": null' in responses[0].content
        assert b'"patient_name": "Ada Lovelace"' in responses[0].content


def test_data_returns_a_distinguishable_error_on_failure():
    """A build failure must come back as a real error, not an empty `rows`
    list indistinguishable from a genuinely empty queue."""
    api_instance = _make_api()

    with patch(f"{MODULE}.JLabOrderQueueEntry") as mock_entry_cls:
        mock_entry_cls.objects.count.side_effect = RuntimeError("boom")

        responses = api_instance.data()

        assert responses[0].status_code == HTTPStatus.INTERNAL_SERVER_ERROR
        assert b"error" in responses[0].content
