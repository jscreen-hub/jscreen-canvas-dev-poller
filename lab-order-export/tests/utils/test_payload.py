"""Tests for the export payload builder."""

from datetime import datetime
from unittest.mock import MagicMock

from lab_order_export.utils.payload import build_payload


class TestBuildPayloadFullOrder:
    """A fully-populated order maps every field group correctly."""

    def test_order_block(self, mock_lab_order: MagicMock) -> None:
        payload = build_payload(mock_lab_order, "command-uuid")

        assert payload["event"] == "lab_order_committed"
        assert payload["order"] == {
            "id": "order-uuid",
            "note_id": "note-uuid",
            "command_id": "command-uuid",
            "date_ordered": "2026-07-07T14:02:55+00:00",
            "lab_partner": "LabCorp",
            "requisition_number": "REQ-123",
            "transmission_type": "electronic",
        }

    def test_captured_at_is_iso_timestamp(self, mock_lab_order: MagicMock) -> None:
        payload = build_payload(mock_lab_order, "command-uuid")
        # Parseable ISO-8601 string.
        assert isinstance(datetime.fromisoformat(payload["captured_at"]), datetime)

    def test_patient_block(self, mock_lab_order: MagicMock) -> None:
        payload = build_payload(mock_lab_order, "command-uuid")
        assert payload["patient"] == {
            "id": "patient-uuid",
            "mrn": "MRN001",
            "first_name": "Jane",
            "last_name": "Doe",
            "date_of_birth": "1985-03-02",
        }

    def test_ordering_provider_block(self, mock_lab_order: MagicMock) -> None:
        payload = build_payload(mock_lab_order, "command-uuid")
        assert payload["ordering_provider"] == {
            "id": "staff-uuid",
            "name": "Dr. Gregory House",
            "npi": "1234567890",
        }

    def test_tests_block_includes_all_tests(self, mock_lab_order: MagicMock) -> None:
        payload = build_payload(mock_lab_order, "command-uuid")
        assert payload["tests"] == [
            {"name": "Complete Blood Count", "code": "CBC", "status": "ordered"},
            {"name": "BRCA1 Genetic Panel", "code": "BRCA1", "status": "ordered"},
        ]

    def test_diagnoses_block_only_icd10(self, mock_lab_order: MagicMock) -> None:
        payload = build_payload(mock_lab_order, "command-uuid")
        # SNOMED coding is filtered out; only the ICD-10 coding survives.
        assert payload["diagnoses"] == [
            {"code": "E11.9", "display": "Type 2 diabetes mellitus"}
        ]


class TestBuildPayloadMissingOptionalFields:
    """Missing optional data yields nulls/empties rather than raising."""

    def test_missing_note(self, mock_lab_order: MagicMock) -> None:
        mock_lab_order.note = None
        payload = build_payload(mock_lab_order, "command-uuid")
        assert payload["order"]["note_id"] is None

    def test_missing_patient(self, mock_lab_order: MagicMock) -> None:
        mock_lab_order.patient = None
        payload = build_payload(mock_lab_order, "command-uuid")
        assert payload["patient"] is None

    def test_missing_ordering_provider(self, mock_lab_order: MagicMock) -> None:
        mock_lab_order.ordering_provider = None
        payload = build_payload(mock_lab_order, "command-uuid")
        assert payload["ordering_provider"] is None

    def test_no_tests(self, mock_lab_order: MagicMock) -> None:
        mock_lab_order.tests.all.return_value = []
        payload = build_payload(mock_lab_order, "command-uuid")
        assert payload["tests"] == []

    def test_no_reasons(self, mock_lab_order: MagicMock) -> None:
        mock_lab_order.reasons.all.return_value = []
        payload = build_payload(mock_lab_order, "command-uuid")
        assert payload["diagnoses"] == []

    def test_reason_condition_missing_condition(self, mock_lab_order: MagicMock) -> None:
        reason = MagicMock()
        reason_condition = MagicMock()
        reason_condition.condition = None
        reason.reason_conditions.all.return_value = [reason_condition]
        mock_lab_order.reasons.all.return_value = [reason]

        payload = build_payload(mock_lab_order, "command-uuid")
        assert payload["diagnoses"] == []

    def test_duplicate_icd_codings_deduped(self, mock_lab_order: MagicMock) -> None:
        coding = MagicMock()
        coding.system = "ICD-10"
        coding.code = "E11.9"
        coding.display = "Type 2 diabetes mellitus"
        reason = MagicMock()
        reason_condition = MagicMock()
        reason_condition.condition.codings.all.return_value = [coding, coding]
        reason.reason_conditions.all.return_value = [reason_condition]
        mock_lab_order.reasons.all.return_value = [reason]

        payload = build_payload(mock_lab_order, "command-uuid")
        assert payload["diagnoses"] == [
            {"code": "E11.9", "display": "Type 2 diabetes mellitus"}
        ]


class TestBuildPayloadValueCoercion:
    """Enum-like and date-like values are coerced to plain JSON-safe types."""

    def test_transmission_type_enum_value(self, mock_lab_order: MagicMock) -> None:
        enum_value = MagicMock()
        enum_value.value = "fax"
        mock_lab_order.transmission_type = enum_value
        payload = build_payload(mock_lab_order, "command-uuid")
        assert payload["order"]["transmission_type"] == "fax"

    def test_none_transmission_type(self, mock_lab_order: MagicMock) -> None:
        mock_lab_order.transmission_type = None
        payload = build_payload(mock_lab_order, "command-uuid")
        assert payload["order"]["transmission_type"] is None

    def test_none_date_ordered(self, mock_lab_order: MagicMock) -> None:
        mock_lab_order.date_ordered = None
        payload = build_payload(mock_lab_order, "command-uuid")
        assert payload["order"]["date_ordered"] is None

    def test_non_datetime_date_ordered_coerced_to_str(
        self, mock_lab_order: MagicMock
    ) -> None:
        # A value with no isoformat() falls through to str().
        mock_lab_order.date_ordered = "2026-07-07"
        payload = build_payload(mock_lab_order, "command-uuid")
        assert payload["order"]["date_ordered"] == "2026-07-07"
