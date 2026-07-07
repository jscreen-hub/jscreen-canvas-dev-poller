"""Map FHIR resources to an enriched, flat order record.

The output shape mirrors the lab-order-export plugin payload so downstream
consumers see a consistent structure regardless of push vs. pull delivery.

FHIR field sources (Canvas):
- MRN: Patient.identifier where type.coding.code == "MR".
- NPI: Practitioner.identifier where system == http://hl7.org/fhir/sid/us-npi.
- ICD-10: Condition.code.coding where system == http://hl7.org/fhir/sid/icd-10-cm.
- Test code: ServiceRequest.code.coding (LOINC).
"""

from __future__ import annotations

from typing import Any

NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"
ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10-cm"
MRN_TYPE_CODE = "MR"


def extract_mrn(patient: dict[str, Any] | None) -> str | None:
    if not patient:
        return None
    for identifier in patient.get("identifier", []):
        for coding in identifier.get("type", {}).get("coding", []):
            if coding.get("code") == MRN_TYPE_CODE:
                value = identifier.get("value")
                return str(value) if value is not None else None
    return None


def extract_full_name(resource: dict[str, Any] | None) -> str | None:
    if not resource:
        return None
    names = resource.get("name", [])
    if not names:
        return None
    name = names[0]
    given = " ".join(name.get("given", []))
    family = name.get("family", "")
    full = " ".join(part for part in (given, family) if part).strip()
    return full or None


def extract_npi(practitioner: dict[str, Any] | None) -> str | None:
    if not practitioner:
        return None
    for identifier in practitioner.get("identifier", []):
        if identifier.get("system") == NPI_SYSTEM:
            value = identifier.get("value")
            return str(value) if value is not None else None
    return None


def extract_icd10(conditions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    diagnoses: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for condition in conditions:
        for coding in condition.get("code", {}).get("coding", []):
            if coding.get("system") != ICD10_SYSTEM:
                continue
            key = (coding.get("code"), coding.get("display"))
            if key in seen:
                continue
            seen.add(key)
            diagnoses.append({"code": coding.get("code"), "display": coding.get("display")})
    return diagnoses


def extract_test_codes(service_request: dict[str, Any]) -> list[dict[str, Any]]:
    codings = service_request.get("code", {}).get("coding", [])
    return [
        {
            "system": coding.get("system"),
            "code": coding.get("code"),
            "display": coding.get("display"),
        }
        for coding in codings
    ]


def _patient_block(patient: dict[str, Any] | None) -> dict[str, Any] | None:
    if not patient:
        return None
    return {
        "id": patient.get("id"),
        "mrn": extract_mrn(patient),
        "name": extract_full_name(patient),
        "date_of_birth": patient.get("birthDate"),
    }


def _provider_block(practitioner: dict[str, Any] | None) -> dict[str, Any] | None:
    if not practitioner:
        return None
    return {
        "id": practitioner.get("id"),
        "name": extract_full_name(practitioner),
        "npi": extract_npi(practitioner),
    }


def build_order_record(
    service_request: dict[str, Any],
    patient: dict[str, Any] | None,
    practitioner: dict[str, Any] | None,
    conditions: list[dict[str, Any]],
    captured_at: str,
) -> dict[str, Any]:
    """Assemble the enriched order record for one ServiceRequest."""
    return {
        "event": "lab_order_polled",
        "captured_at": captured_at,
        "order": {
            "id": service_request.get("id"),
            "status": service_request.get("status"),
            "authored_on": service_request.get("authoredOn"),
            "tests": extract_test_codes(service_request),
        },
        "patient": _patient_block(patient),
        "ordering_provider": _provider_block(practitioner),
        "diagnoses": extract_icd10(conditions),
    }
