"""Shared fixtures for lab-order-export tests."""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest


def _make_test(name: str, code: str, status: str) -> MagicMock:
    test = MagicMock()
    test.ontology_test_name = name
    test.ontology_test_code = code
    test.status = status
    return test


def _make_coding(system: str, code: str, display: str) -> MagicMock:
    coding = MagicMock()
    coding.system = system
    coding.code = code
    coding.display = display
    return coding


@pytest.fixture
def mock_patient() -> MagicMock:
    patient = MagicMock()
    patient.id = "patient-uuid"
    patient.mrn = "MRN001"
    patient.first_name = "Jane"
    patient.last_name = "Doe"
    patient.birth_date = date(1985, 3, 2)
    return patient


@pytest.fixture
def mock_provider() -> MagicMock:
    provider = MagicMock()
    provider.id = "staff-uuid"
    provider.full_name = "Dr. Gregory House"
    provider.npi_number = "1234567890"
    return provider


@pytest.fixture
def mock_lab_order(mock_patient: MagicMock, mock_provider: MagicMock) -> MagicMock:
    """A fully-populated committed lab order with two tests and one ICD-10 diagnosis."""
    lab_order = MagicMock()
    lab_order.id = "order-uuid"
    lab_order.note.id = "note-uuid"
    lab_order.date_ordered = datetime(2026, 7, 7, 14, 2, 55, tzinfo=timezone.utc)
    lab_order.ontology_lab_partner = "LabCorp"
    lab_order.requisition_number = "REQ-123"
    lab_order.transmission_type = "electronic"
    lab_order.patient = mock_patient
    lab_order.ordering_provider = mock_provider

    lab_order.tests.all.return_value = [
        _make_test("Complete Blood Count", "CBC", "ordered"),
        _make_test("BRCA1 Genetic Panel", "BRCA1", "ordered"),
    ]

    # reason -> reason_conditions -> condition -> codings (ICD-10 + a non-ICD coding)
    reason = MagicMock()
    reason_condition = MagicMock()
    reason_condition.condition.codings.all.return_value = [
        _make_coding("ICD-10", "E11.9", "Type 2 diabetes mellitus"),
        _make_coding("SNOMED", "44054006", "Diabetes"),
    ]
    reason.reason_conditions.all.return_value = [reason_condition]
    lab_order.reasons.all.return_value = [reason]

    return lab_order
