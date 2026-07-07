"""Tests for FHIR -> order record mapping."""

from order_poller.mapper import (
    build_order_record,
    extract_full_name,
    extract_icd10,
    extract_mrn,
    extract_npi,
    extract_test_codes,
)

PATIENT = {
    "id": "patient-1",
    "birthDate": "1985-03-02",
    "name": [{"given": ["Jane", "Q"], "family": "Doe"}],
    "identifier": [
        {
            "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MR"}]},
            "system": "http://canvasmedical.com",
            "value": "MRN001",
        }
    ],
}

PRACTITIONER = {
    "id": "prac-1",
    "name": [{"given": ["Gregory"], "family": "House"}],
    "identifier": [
        {"system": "http://hl7.org/fhir/sid/us-npi", "value": "1234567890"}
    ],
}

CONDITION = {
    "id": "cond-1",
    "code": {
        "coding": [
            {"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "E11.9", "display": "Type 2 diabetes mellitus"},
            {"system": "http://snomed.info/sct", "code": "44054006", "display": "Diabetes"},
        ]
    },
}

SERVICE_REQUEST = {
    "id": "sr-1",
    "status": "active",
    "authoredOn": "2026-07-07T14:02:55+00:00",
    "code": {"coding": [{"system": "http://loinc.org", "code": "58410-2", "display": "CBC panel"}]},
    "subject": {"reference": "Patient/patient-1"},
    "requester": {"reference": "Practitioner/prac-1"},
    "reasonReference": [{"reference": "Condition/cond-1"}],
}


class TestExtractors:
    def test_extract_mrn(self) -> None:
        assert extract_mrn(PATIENT) == "MRN001"

    def test_extract_mrn_none_patient(self) -> None:
        assert extract_mrn(None) is None

    def test_extract_mrn_no_mr_identifier(self) -> None:
        assert extract_mrn({"identifier": [{"type": {"coding": [{"code": "SS"}]}}]}) is None

    def test_extract_full_name(self) -> None:
        assert extract_full_name(PATIENT) == "Jane Q Doe"

    def test_extract_full_name_missing(self) -> None:
        assert extract_full_name({"name": []}) is None
        assert extract_full_name(None) is None

    def test_extract_npi(self) -> None:
        assert extract_npi(PRACTITIONER) == "1234567890"

    def test_extract_npi_none(self) -> None:
        assert extract_npi(None) is None
        assert extract_npi({"identifier": [{"system": "other", "value": "x"}]}) is None

    def test_extract_icd10_filters_and_dedupes(self) -> None:
        # SNOMED coding filtered out; duplicate ICD-10 collapsed.
        assert extract_icd10([CONDITION, CONDITION]) == [
            {"code": "E11.9", "display": "Type 2 diabetes mellitus"}
        ]

    def test_extract_icd10_empty(self) -> None:
        assert extract_icd10([]) == []

    def test_extract_test_codes(self) -> None:
        assert extract_test_codes(SERVICE_REQUEST) == [
            {"system": "http://loinc.org", "code": "58410-2", "display": "CBC panel"}
        ]


class TestBuildOrderRecord:
    def test_full_record(self) -> None:
        record = build_order_record(
            SERVICE_REQUEST, PATIENT, PRACTITIONER, [CONDITION], "2026-07-07T15:00:00+00:00"
        )
        assert record == {
            "event": "lab_order_polled",
            "captured_at": "2026-07-07T15:00:00+00:00",
            "order": {
                "id": "sr-1",
                "status": "active",
                "authored_on": "2026-07-07T14:02:55+00:00",
                "tests": [
                    {"system": "http://loinc.org", "code": "58410-2", "display": "CBC panel"}
                ],
            },
            "patient": {
                "id": "patient-1",
                "mrn": "MRN001",
                "name": "Jane Q Doe",
                "date_of_birth": "1985-03-02",
            },
            "ordering_provider": {"id": "prac-1", "name": "Gregory House", "npi": "1234567890"},
            "diagnoses": [{"code": "E11.9", "display": "Type 2 diabetes mellitus"}],
        }

    def test_missing_enrichment(self) -> None:
        record = build_order_record(SERVICE_REQUEST, None, None, [], "t")
        assert record["patient"] is None
        assert record["ordering_provider"] is None
        assert record["diagnoses"] == []
