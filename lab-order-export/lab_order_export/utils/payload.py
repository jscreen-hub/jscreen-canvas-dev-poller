"""Build the structured export payload from a committed LabOrder.

The payload shape is defined in the plugin spec (§4). Every field is optional at
the source layer, so each accessor is defensive: a missing patient, ordering
provider, requisition number, or diagnosis must not raise — it simply yields a
null / empty value in the payload.
"""

from datetime import datetime, timezone
from typing import Any

from canvas_sdk.v1.data.lab import LabOrder


def _isoformat(value: Any) -> str | None:
    """Return an ISO-8601 string for a datetime/date, or None."""
    if value is None:
        return None
    if isinstance(value, (datetime,)):
        return value.isoformat()
    # date objects (and anything else with isoformat) are handled generically.
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _enum_str(value: Any) -> str | None:
    """Coerce an enum / choice value to a plain string, or None."""
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _patient_block(lab_order: LabOrder) -> dict[str, Any] | None:
    patient = lab_order.patient
    if patient is None:
        return None
    return {
        "id": str(patient.id),
        "mrn": patient.mrn,
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "date_of_birth": _isoformat(patient.birth_date),
    }


def _ordering_provider_block(lab_order: LabOrder) -> dict[str, Any] | None:
    provider = lab_order.ordering_provider
    if provider is None:
        return None
    return {
        "id": str(provider.id),
        "name": provider.full_name,
        "npi": provider.npi_number,
    }


def _tests_block(lab_order: LabOrder) -> list[dict[str, Any]]:
    tests = []
    for test in lab_order.tests.all():
        tests.append(
            {
                "name": test.ontology_test_name,
                "code": test.ontology_test_code,
                "status": _enum_str(test.status),
            }
        )
    return tests


def _diagnoses_block(lab_order: LabOrder) -> list[dict[str, Any]]:
    """Flatten LabOrder.reasons -> reason_conditions -> condition codings.

    Only ICD-10 codings are emitted. When a condition has no ICD-10 coding it is
    skipped rather than producing a null entry.
    """
    diagnoses: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for reason in lab_order.reasons.all():
        for reason_condition in reason.reason_conditions.all():
            condition = reason_condition.condition
            if condition is None:
                continue
            for coding in condition.codings.all():
                system = (coding.system or "").upper()
                if "ICD" not in system:
                    continue
                key = (coding.code, coding.display)
                if key in seen:
                    continue
                seen.add(key)
                diagnoses.append({"code": coding.code, "display": coding.display})
    return diagnoses


def build_payload(lab_order: LabOrder, command_id: str) -> dict[str, Any]:
    """Assemble the export payload for a committed lab order.

    Args:
        lab_order: the committed LabOrder resolved from the command event.
        command_id: the id of the Lab Order command that triggered the export.

    Returns:
        A JSON-serializable dict matching the spec's payload shape.
    """
    note = lab_order.note

    return {
        "event": "lab_order_committed",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "order": {
            "id": str(lab_order.id),
            "note_id": str(note.id) if note is not None else None,
            "command_id": command_id,
            "date_ordered": _isoformat(lab_order.date_ordered),
            "lab_partner": lab_order.ontology_lab_partner,
            "requisition_number": lab_order.requisition_number,
            "transmission_type": _enum_str(lab_order.transmission_type),
        },
        "patient": _patient_block(lab_order),
        "ordering_provider": _ordering_provider_block(lab_order),
        "tests": _tests_block(lab_order),
        "diagnoses": _diagnoses_block(lab_order),
    }
