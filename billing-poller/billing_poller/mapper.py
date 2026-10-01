"""Map FHIR resources to a flat billing record for one signed-off lab result.

FHIR field sources (Canvas):
- MRN:      Patient.identifier where type.coding.code == "MR".
- NPI:      Practitioner.identifier where system == http://hl7.org/fhir/sid/us-npi.
- ICD-10:   Condition.code.coding where system == http://hl7.org/fhir/sid/icd-10-cm,
            reached from the matched order's `reasonReference`.
- Insurance: Coverage (+ its payor Organization), ranked by Coverage.order,
            which is the coordination-of-benefits sequence (1 = primary).
- CPT:      Claim.item.productOrService. See the note in `extract_procedures`.
- C-CDA:    Not a FHIR resource -- `GET {auth host}/api/data-export/ccda/{patient_key}`.
            Fetched by the caller (`CanvasClient.get_ccda`), not by this module.
            See the notes on `build_ccda_url` and `ccda_content` below.
"""

from __future__ import annotations

from typing import Any

from billing_poller.config import (
    CCDA_DOCUMENT_TYPE,
    CCDA_PATH,
    CLAIM_QUEUE_EXTENSION,
    CPT_SYSTEM,
    ICD10_SYSTEM,
    MRN_TYPE_CODE,
    NPI_SYSTEM,
)


# -- small field extractors --------------------------------------------------


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


def _preferred(entries: list[dict[str, Any]], uses: tuple[str, ...]) -> dict[str, Any] | None:
    """First entry whose `use` is one of `uses`, else the first entry."""
    for entry in entries:
        if entry.get("use") in uses:
            return entry
    return entries[0] if entries else None


def extract_address(patient: dict[str, Any] | None) -> dict[str, Any] | None:
    """Billing address. Claims are rejected without one, so this is required data."""
    if not patient:
        return None
    address = _preferred(patient.get("address", []), ("billing", "home"))
    if not address:
        return None
    return {
        "line": address.get("line", []),
        "city": address.get("city"),
        "state": address.get("state"),
        "postal_code": address.get("postalCode"),
        "country": address.get("country"),
    }


def extract_phone(patient: dict[str, Any] | None) -> str | None:
    if not patient:
        return None
    for telecom in patient.get("telecom", []):
        if telecom.get("system") == "phone":
            value = telecom.get("value")
            return str(value) if value else None
    return None


def extract_icd10(conditions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ICD-10-CM diagnoses, de-duplicated, order preserved."""
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
            diagnoses.append(
                {"code": coding.get("code"), "display": coding.get("display")}
            )
    return diagnoses


def extract_test_codes(order: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not order:
        return []
    return [
        {
            "system": coding.get("system"),
            "code": coding.get("code"),
            "display": coding.get("display"),
        }
        for coding in order.get("code", {}).get("coding", [])
    ]


def build_ccda_url(auth_base_url: str | None, patient_id: str | None) -> str | None:
    """Link to the patient's continuity-of-care C-CDA export (config.CCDA_PATH).

    This is the whole chart, not scoped to this result's order -- a biller who
    needs wider history can pull it, but it is never fetched or embedded here.
    """
    if not auth_base_url or not patient_id:
        return None
    path = CCDA_PATH.format(patient_key=patient_id)
    return f"{auth_base_url}{path}?document={CCDA_DOCUMENT_TYPE}"


def claim_queue(claim: dict[str, Any]) -> str | None:
    for extension in claim.get("extension", []):
        if extension.get("url") == CLAIM_QUEUE_EXTENSION:
            code = extension.get("valueCoding", {}).get("code")
            return str(code) if code else None
    return None


def extract_procedures(claim: dict[str, Any] | None) -> list[dict[str, Any]]:
    """CPT/HCPCS service lines from a Claim.

    Canvas exposes billed procedure codes only through `Claim.item`. A claim
    that has not been coded yet comes back with no `item` array at all, in which
    case this is legitimately empty -- the caller records that as a data gap
    rather than dropping the record.
    """
    if not claim:
        return []
    procedures: list[dict[str, Any]] = []
    for item in claim.get("item", []):
        for coding in item.get("productOrService", {}).get("coding", []):
            procedures.append(
                {
                    "code": coding.get("code"),
                    "display": coding.get("display"),
                    "system": coding.get("system"),
                    "is_cpt": coding.get("system") == CPT_SYSTEM,
                    "quantity": item.get("quantity", {}).get("value"),
                    "unit_price": item.get("unitPrice", {}).get("value"),
                    "modifiers": [
                        m.get("code")
                        for mod in item.get("modifier", [])
                        for m in mod.get("coding", [])
                        if m.get("code")
                    ],
                    "diagnosis_pointers": item.get("diagnosisSequence", []),
                }
            )
    return procedures


def extract_report_procedures(report: dict[str, Any]) -> list[dict[str, Any]]:
    """CPT codings carried on the lab report itself.

    Whoever posts the result via `$create-lab-report` controls
    `labReport.code.coding`, and unlike the labTest Observations (which Canvas
    does not read back over FHIR) that field IS readable. A poster who knows the
    CPT from the lab can put it there, and it needs no plugin and no compendium.
    """
    procedures: list[dict[str, Any]] = []
    seen: set[str] = set()
    for coding in report.get("code", {}).get("coding", []):
        if coding.get("system") != CPT_SYSTEM:
            continue
        code = coding.get("code")
        if not code or str(code) in seen:
            continue
        seen.add(str(code))
        procedures.append(
            {
                "code": str(code),
                "display": coding.get("display"),
                "system": CPT_SYSTEM,
                "is_cpt": True,
                "quantity": 1,
                "unit_price": None,
                "modifiers": [],
                "diagnosis_pointers": [],
                "source": "lab_report",
            }
        )
    return procedures


def extract_claim_diagnoses(claim: dict[str, Any] | None) -> list[dict[str, Any]]:
    """ICD-10 carried on the claim itself, which may differ from the order's."""
    if not claim:
        return []
    diagnoses: list[dict[str, Any]] = []
    for entry in claim.get("diagnosis", []):
        for coding in entry.get("diagnosisCodeableConcept", {}).get("coding", []):
            diagnoses.append(
                {
                    "code": coding.get("code"),
                    "display": coding.get("display"),
                    "sequence": entry.get("sequence"),
                }
            )
    return diagnoses


# -- record blocks -----------------------------------------------------------


def _patient_block(
    patient: dict[str, Any] | None,
    ccda_url: str | None,
    ccda_xml: str | None,
) -> dict[str, Any] | None:
    if not patient:
        return None
    return {
        "id": patient.get("id"),
        "mrn": extract_mrn(patient),
        "name": extract_full_name(patient),
        "date_of_birth": patient.get("birthDate"),
        "gender": patient.get("gender"),
        "address": extract_address(patient),
        "phone": extract_phone(patient),
        # Link for provenance/debugging, plus the fetched document itself --
        # see the note on `auth_base_url`/`ccda_content` in build_billing_record.
        "ccda_url": ccda_url,
        "ccda_xml": ccda_xml,
    }


def _provider_block(practitioner: dict[str, Any] | None) -> dict[str, Any] | None:
    if not practitioner:
        return None
    return {
        "id": practitioner.get("id"),
        "name": extract_full_name(practitioner),
        "npi": extract_npi(practitioner),
    }


def _coverage_class(coverage: dict[str, Any], class_type: str) -> str | None:
    """Pull a `plan` or `group` value out of Coverage.class."""
    classes = coverage.get("class")
    if isinstance(classes, dict):
        entries = [classes]
    elif isinstance(classes, list):
        entries = classes
    else:
        entries = []
    for entry in entries:
        for coding in entry.get("type", {}).get("coding", []):
            if coding.get("code") == class_type:
                value = entry.get("value") or entry.get("name")
                return str(value) if value else None
    return None


def _member_id(coverage: dict[str, Any]) -> str | None:
    identifiers = coverage.get("identifier")
    if isinstance(identifiers, dict):
        identifiers = [identifiers]
    for identifier in identifiers or []:
        value = identifier.get("value")
        if value:
            return str(value)
    return None


def _coding_field(concept: dict[str, Any], field: str) -> str | None:
    for coding in concept.get("coding", []):
        value = coding.get(field)
        if value:
            return str(value)
    return None


def _insurance_block(
    coverages: list[dict[str, Any]], payors: dict[str, dict[str, Any] | None]
) -> list[dict[str, Any]]:
    """One entry per active coverage, ordered primary first.

    `Coverage.order` is the coordination-of-benefits rank. Coverages without one
    sort last rather than being dropped.
    """
    blocks: list[dict[str, Any]] = []
    for coverage in coverages:
        payor_ref = (coverage.get("payor") or [{}])[0]
        payor = payors.get(payor_ref.get("reference") or "") or {}
        relationship = coverage.get("relationship", {})
        blocks.append(
            {
                "rank": coverage.get("order"),
                "coverage_id": coverage.get("id"),
                "status": coverage.get("status"),
                "payor": {
                    "id": payor.get("id"),
                    "name": payor.get("name") or payor_ref.get("display"),
                },
                "member_id": _member_id(coverage),
                "subscriber_id": coverage.get("subscriberId"),
                "relationship": {
                    "code": _coding_field(relationship, "code"),
                    "display": _coding_field(relationship, "display"),
                    # Canvas puts the 2-character CMS relationship code in .text
                    "cms_code": relationship.get("text"),
                },
                "plan": _coverage_class(coverage, "plan"),
                "group": _coverage_class(coverage, "group"),
                "period": coverage.get("period"),
            }
        )
    return sorted(blocks, key=lambda b: (b["rank"] is None, b["rank"] or 0))


def _encounter_type(encounter: dict[str, Any] | None) -> str | None:
    if not encounter:
        return None
    for entry in encounter.get("type", []):
        display = _coding_field(entry, "display")
        if display:
            return display
    return None


def reviewer_reference(encounter: dict[str, Any] | None) -> str | None:
    """The practitioner who committed the Lab Results Review."""
    if not encounter:
        return None
    for participant in encounter.get("participant", []):
        reference = participant.get("individual", {}).get("reference")
        if reference:
            return str(reference)
    return None


def build_billing_record(
    report: dict[str, Any],
    encounter: dict[str, Any] | None,
    patient: dict[str, Any] | None,
    order: dict[str, Any] | None,
    order_match: dict[str, Any],
    ordering_provider: dict[str, Any] | None,
    reviewing_provider: dict[str, Any] | None,
    conditions: list[dict[str, Any]],
    coverages: list[dict[str, Any]],
    payors: dict[str, dict[str, Any] | None],
    claim: dict[str, Any] | None,
    captured_at: str,
    extra_procedures: list[dict[str, Any]] | None = None,
    lookup: dict[str, Any] | None = None,
    unresolved_codes: list[str] | None = None,
    suppressed_procedures: list[dict[str, Any]] | None = None,
    authoritative_procedures: list[dict[str, Any]] | None = None,
    lab_order_key: str | None = None,
    auth_base_url: str | None = None,
    ccda_content: str | None = None,
) -> dict[str, Any]:
    """Assemble the billing record for one signed-off lab result.

    `extra_procedures` carries CPT lines resolved from the lab compendium via the
    lab_billing_lookup plugin. A coded Claim is still preferred when one exists
    (it reflects what was actually billed); the compendium fills the far more
    common case where no claim carries lines.

    `auth_base_url` is the Canvas auth host (Settings.auth_base_url, not the
    `fumage-` FHIR host); it's used only to build `patient.ccda_url`. Omitted or
    None, that field comes back None rather than failing the whole record.

    `ccda_content` is the C-CDA XML already fetched by the caller (via
    `CanvasClient.get_ccda`) -- this function does no I/O itself. It lands
    verbatim in `patient.ccda_xml`; the patient's whole chart history, not
    scoped to this order. A patient with no fetched content still produces a
    record, with `ccda_xml: null` and `no_ccda` in `data_gaps`.
    """
    if authoritative_procedures is not None:
        # The report itself listed the tests it covers, so those codes -- and
        # only those -- decide what is billable here. A CPT from a claim or from
        # the order's other tests could bill work this report does not cover.
        procedures = list(authoritative_procedures)
    else:
        procedures = extract_procedures(claim)
        if not any(p["is_cpt"] for p in procedures):
            # A CPT posted on the report outranks the compendium: it is what the
            # performing lab actually billed for this specimen.
            procedures = procedures + extract_report_procedures(report)
        if not any(p["is_cpt"] for p in procedures):
            procedures = procedures + list(extra_procedures or [])
    diagnoses = extract_icd10(conditions)
    claim_diagnoses = extract_claim_diagnoses(claim)
    insurance = _insurance_block(coverages, payors)
    ccda_url = build_ccda_url(auth_base_url, (patient or {}).get("id"))
    patient_block = _patient_block(patient, ccda_url, ccda_content)

    record: dict[str, Any] = {
        "event": "lab_billing_ready",
        "captured_at": captured_at,
        "result": {
            "diagnostic_report_id": report.get("id"),
            "name": report.get("code", {}).get("text"),
            "status": report.get("status"),
            "effective_date": report.get("effectiveDateTime"),
            "issued": report.get("issued"),
            "report_pdf_url": next(
                (
                    form.get("url")
                    for form in report.get("presentedForm", [])
                    if form.get("url")
                ),
                None,
            ),
        },
        "sign_off": {
            "encounter_id": (encounter or {}).get("id"),
            "encounter_status": (encounter or {}).get("status"),
            "encounter_type": _encounter_type(encounter),
            "reviewed_at": (encounter or {}).get("period", {}).get("start"),
            "reviewed_by": _provider_block(reviewing_provider),
        },
        "order": (
            {
                "id": order.get("id"),
                "status": order.get("status"),
                "authored_on": order.get("authoredOn"),
                "tests": extract_test_codes(order),
                "ordering_provider": _provider_block(ordering_provider),
            }
            if order
            else None
        ),
        "order_match": order_match,
        "patient": patient_block,
        "insurance": insurance,
        "diagnoses": diagnoses,
        "procedures": procedures,
        "claim": (
            {
                "id": claim.get("id"),
                "queue": claim_queue(claim),
                "date_of_service": claim.get("created"),
                "diagnoses": claim_diagnoses,
            }
            if claim
            else None
        ),
        "lab_order": (
            {
                "lab_order_ids": [o.get("lab_order_id") for o in lookup.get("orders", [])],
                "requisition_number": lookup.get("requisition_number"),
                "lab_partner": next(
                    (o.get("lab_partner") for o in lookup.get("orders", [])), None
                ),
            }
            if lookup
            else None
        ),
        # CPTs this order was already billed for by an earlier report. Held out
        # of `procedures` so that block is always safe to bill as-is, but kept
        # here so nothing disappears silently.
        "suppressed_procedures": list(suppressed_procedures or []),
        # Identity the duplicate-CPT ledger is keyed on; null when no order was
        # resolved, which is why such a record cannot be de-duplicated.
        "lab_order_key": lab_order_key,
        "data_gaps": [],
    }

    # Anything a biller would otherwise have to chase. Recorded explicitly so a
    # thin record is visibly thin rather than quietly wrong.
    gaps: list[str] = []
    if record["order"] is None:
        gaps.append("no_order_match")
    elif order_match.get("confidence") == "low":
        gaps.append("low_confidence_order_match")
    if not insurance:
        gaps.append("no_active_coverage")
    if not diagnoses and not claim_diagnoses:
        gaps.append("no_icd10")
    if not any(p["is_cpt"] for p in procedures) and not suppressed_procedures:
        gaps.append("no_cpt_codes")
    if unresolved_codes:
        # Some posted test codes resolved and some did not: the record is
        # partially billed, which is easy to miss without saying it.
        gaps.append("cpt_unresolved_for_" + ",".join(sorted(unresolved_codes)))
    if order_match.get("spans_orders"):
        gaps.append("report_spans_multiple_orders")
    if suppressed_procedures:
        gaps.append("duplicate_cpt_suppressed")
    if patient_block is None or not patient_block.get("address"):
        gaps.append("no_patient_address")
    if patient_block is not None and not ccda_content:
        gaps.append("no_ccda")
    record["data_gaps"] = gaps
    return record
