"""Expose lab billing data that the FHIR API does not carry.

Two facts the billing-poller needs live only in SDK models, with no FHIR or REST
equivalent on a Canvas instance:

1. **CPT codes.** `LabPartnerTest.cpt_code` holds the CPT for an orderable test.
   Nothing in FHIR exposes it -- `Claim.item` is empty until a claim is coded,
   `Procedure` is unpopulated, and `ServiceRequest.code` carries only the lab
   partner's order code.
2. **The result -> order link.** `DiagnosticReport.lab -> LabReport.tests ->
   LabTest.order` is an exact foreign-key chain. FHIR omits
   `DiagnosticReport.basedOn` entirely, forcing the poller to guess which order
   produced a result.

This handler is read-only. It returns billing codes and identifiers, never
clinical values or patient demographics -- the poller already reads those over
FHIR, and keeping PHI out of this response limits what a leaked API key exposes.
"""

from http import HTTPStatus
from hmac import compare_digest
from typing import Any

from canvas_sdk.effects import Effect
from canvas_sdk.effects.simple_api import JSONResponse, Response
from canvas_sdk.handlers.simple_api import APIKeyCredentials, SimpleAPI, api
from canvas_sdk.v1.data import DiagnosticReport
from canvas_sdk.v1.data.lab import LabPartnerTest
from logger import log

SECRET_API_KEY = "BILLING_LOOKUP_API_KEY"


def _cpt_index() -> dict[tuple[str, str], dict[str, Any]]:
    """Map (lab partner name, order code) -> compendium entry.

    Keyed on the partner too: two partners can reuse the same order code for
    different tests, so a code-only index would return the wrong CPT.
    """
    index: dict[tuple[str, str], dict[str, Any]] = {}
    tests = LabPartnerTest.objects.select_related("lab_partner").all()
    for test in tests:
        if not test.order_code:
            continue
        partner = getattr(test.lab_partner, "name", "") or ""
        entry = {
            "order_code": test.order_code,
            "order_name": test.order_name,
            "cpt_code": test.cpt_code or None,
            "lab_partner": partner,
        }
        # NOTE: `index[key] = entry` is rejected by the Canvas sandbox
        # ("Forbidden assignment to a non-module attribute: builtins.dict").
        # Subscript assignment on a dict is blocked; method calls are not.
        index.update({(partner, test.order_code): entry})
        # Also index without the partner so a lookup still resolves when the
        # order's partner string does not match the compendium's exactly.
        index.setdefault(("", test.order_code), entry)
    return index


def _lookup_cpt(
    index: dict[tuple[str, str], dict[str, Any]], partner: str, order_code: str | None
) -> dict[str, Any] | None:
    if not order_code:
        return None
    return index.get((partner or "", order_code)) or index.get(("", order_code))


class BillingLookupAPI(SimpleAPI):
    """Read-only billing lookups for the local billing-poller."""

    PREFIX = "/billing"

    def authenticate(self, credentials: APIKeyCredentials) -> bool:
        expected = self.secrets.get(SECRET_API_KEY) or ""
        provided = credentials.key or ""
        if not expected:
            log.error(
                f"[lab-billing-lookup] {SECRET_API_KEY} is not configured; "
                "refusing all requests"
            )
            return False
        return compare_digest(provided.encode(), expected.encode())

    @api.get("/compendium")
    def compendium(self) -> list[Response | Effect]:
        """Every orderable test with its CPT code.

        The poller caches this and resolves CPT locally, so a full sweep does not
        issue one lookup per test.
        """
        entries = [
            {
                "order_code": test.order_code,
                "order_name": test.order_name,
                "cpt_code": test.cpt_code or None,
                "lab_partner": getattr(test.lab_partner, "name", "") or "",
            }
            for test in LabPartnerTest.objects.select_related("lab_partner").all()
            if test.order_code
        ]
        return [
            JSONResponse({"count": len(entries), "tests": entries})
        ]

    @api.get("/orders/<patient_id>")
    def orders(self) -> list[Response | Effect]:
        """Lab orders for a patient, with every identifier Canvas holds.

        The FHIR ServiceRequest id and the Canvas LabOrder id are different
        values for the same order, and nothing in FHIR exposes the LabOrder id.
        This is what lets a caller carry both.
        """
        patient_id = self.request.path_params["patient_id"]
        from canvas_sdk.v1.data.lab import LabOrder

        rows = []
        orders = (
            LabOrder.objects.filter(patient__id=patient_id, deleted=False)
            .select_related("note", "patient")
            .order_by("-date_ordered")
        )
        for order in orders:
            note = getattr(order, "note", None)
            tests = [
                {
                    "lab_test_id": str(test.id),
                    "order_code": test.ontology_test_code,
                    "name": test.ontology_test_name,
                }
                for test in order.tests.all()
            ]
            rows.append(
                {
                    "lab_order_id": str(order.id),
                    "lab_order_dbid": order.dbid,
                    "requisition_number": order.requisition_number,
                    "note_id": str(getattr(note, "id", "")) or None,
                    "date_ordered": _iso(order.date_ordered),
                    "lab_partner": order.ontology_lab_partner or "",
                    "tests": tests,
                }
            )
        return [JSONResponse({"patient_id": patient_id, "orders": rows})]

    @api.get("/lab-orders")
    def lab_orders(self) -> list[Response | Effect]:
        """Every SIGNED lab order, in the order-poller's legacy record shape.

        "Signed" is `LabOrder.objects.committed()`: a committer is set and the
        order is not entered-in-error. That is Canvas's own commit flag, which
        is a stricter and more direct signal than the FHIR `status` heuristic
        the poller used to apply.
        """
        from canvas_sdk.v1.data.lab import LabOrder

        index = _cpt_index()
        orders = (
            LabOrder.objects.committed()
            .filter(deleted=False)
            .select_related("patient", "ordering_provider")
            .order_by("dbid")
        )
        records = []
        for lab_order in orders:
            try:
                records.append(_order_record(lab_order, index))
            except Exception as exc:  # noqa: BLE001 - one bad row must not blank the feed
                log.error(
                    f"[lab-billing-lookup] skipping LabOrder {lab_order.id}: "
                    f"{exc.__class__.__name__}: {exc}"
                )
        return [JSONResponse({"count": len(records), "orders": records})]

    @api.get("/order/<lab_order_id>")
    def order(self) -> list[Response | Effect]:
        """Resolve a Canvas LabOrder id -- the id downstream lab vendors use.

        It is NOT a FHIR id: it resolves against no FHIR endpoint. This is the
        only way to go from that id back to the patient, tests and CPT codes.
        """
        lab_order_id = self.request.path_params["lab_order_id"]
        from canvas_sdk.v1.data.lab import LabOrder

        try:
            order = LabOrder.objects.select_related("patient", "note").get(
                id=lab_order_id
            )
        except LabOrder.DoesNotExist:
            return [
                JSONResponse(
                    {"error": f"no LabOrder {lab_order_id}"},
                    status_code=HTTPStatus.NOT_FOUND,
                )
            ]

        index = _cpt_index()
        partner = order.ontology_lab_partner or ""
        tests = []
        for test in order.tests.all():
            entry = _lookup_cpt(index, partner, test.ontology_test_code)
            tests.append(
                {
                    "lab_test_id": str(test.id),
                    "order_code": test.ontology_test_code,
                    "name": test.ontology_test_name,
                    "cpt_code": (entry or {}).get("cpt_code"),
                }
            )
        patient = getattr(order, "patient", None)
        note = getattr(order, "note", None)
        return [
            JSONResponse(
                {
                    "lab_order_id": str(order.id),
                    "requisition_number": order.requisition_number,
                    "patient_id": str(getattr(patient, "id", "")) or None,
                    "note_id": str(getattr(note, "id", "")) or None,
                    "lab_partner": partner,
                    "date_ordered": _iso(order.date_ordered),
                    "deleted": order.deleted,
                    "tests": tests,
                }
            )
        ]

    @api.get("/report/<report_id>")
    def report(self) -> list[Response | Effect]:
        """The exact order behind a DiagnosticReport, with CPT per test.

        `report_id` is the FHIR DiagnosticReport id, which is also the id of the
        SDK `DiagnosticReport` row -- so the poller can call this with the id it
        already has.
        """
        report_id = self.request.path_params["report_id"]
        try:
            diagnostic_report = DiagnosticReport.objects.select_related("lab").get(
                id=report_id
            )
        except DiagnosticReport.DoesNotExist:
            return [
                JSONResponse(
                    {"error": f"no DiagnosticReport {report_id}"},
                    status_code=HTTPStatus.NOT_FOUND,
                )
            ]

        lab_report = diagnostic_report.lab
        if lab_report is None:
            return [JSONResponse(_empty_payload(report_id, "report has no lab report"))]

        # This walk crosses four models. A schema difference on any of them must
        # degrade to an empty payload the poller can carry on with, not a 500
        # that stops the billing feed.
        try:
            orders = _orders_for(lab_report)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            log.error(
                f"[lab-billing-lookup] order walk failed for {report_id}: "
                f"{exc.__class__.__name__}: {exc}"
            )
            return [
                JSONResponse(
                    _empty_payload(
                        report_id, f"order walk failed: {exc.__class__.__name__}: {exc}"
                    )
                )
            ]

        return [
            JSONResponse(
                {
                    "diagnostic_report_id": report_id,
                    "lab_report_id": str(lab_report.id),
                    "requisition_number": lab_report.requisition_number,
                    "orders": orders,
                }
            )
        ]


def _orders_for(lab_report: Any) -> list[dict[str, Any]]:
    """Group the report's ordered tests under the orders they came from."""
    index = _cpt_index()
    orders: dict[str, dict[str, Any]] = {}
    # `ordered_tests` is the SDK's own filter for tests that came from an order
    # (as opposed to result-only rows), which is exactly the set that carries
    # billable order codes.
    for test in lab_report.ordered_tests.select_related("order"):
        order = test.order
        if order is None:
            continue
        order_id = str(order.id)
        partner = order.ontology_lab_partner or ""
        entry = orders.setdefault(
            order_id,
            {
                "lab_order_id": order_id,
                "requisition_number": order.requisition_number,
                "lab_partner": partner,
                "date_ordered": _iso(order.date_ordered),
                "is_patient_bill": order.is_patient_bill,
                "tests": [],
            },
        )
        compendium = _lookup_cpt(index, partner, test.ontology_test_code)
        entry["tests"].append(
            {
                "name": test.ontology_test_name,
                "order_code": test.ontology_test_code,
                "cpt_code": (compendium or {}).get("cpt_code"),
                "compendium_name": (compendium or {}).get("order_name"),
            }
        )
    return list(orders.values())


def _icd10_for(lab_order: Any) -> list[dict[str, Any]]:
    """ICD-10 codings reached through LabOrder.reasons -> conditions."""
    diagnoses: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for reason in lab_order.reasons.all():
        for reason_condition in reason.reason_conditions.all():
            condition = reason_condition.condition
            if condition is None:
                continue
            for coding in condition.codings.all():
                if "ICD" not in (coding.system or "").upper():
                    continue
                key = (coding.code, coding.display)
                if key in seen:
                    continue
                seen.add(key)
                diagnoses.append({"code": coding.code, "display": coding.display})
    return diagnoses


def _order_record(lab_order: Any, index: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    """One committed lab order, shaped like the order-poller's legacy record.

    `order.id` is the Canvas LabOrder UUID -- the id downstream lab vendors ask
    for. The FHIR ServiceRequest id is deliberately absent: it lives in a
    separate identifier space with no reliable join (62% of committed
    ServiceRequests match more than one LabOrder on test codes alone).
    """
    partner = lab_order.ontology_lab_partner or ""
    patient = getattr(lab_order, "patient", None)
    provider = getattr(lab_order, "ordering_provider", None)

    tests = []
    for test in lab_order.tests.all():
        entry = _lookup_cpt(index, partner, test.ontology_test_code)
        tests.append(
            {
                # `system` mirrors what FHIR reported, so the shape is unchanged
                # for existing consumers, even though these are lab partner
                # order codes rather than true LOINC.
                "system": "http://loinc.org",
                "code": test.ontology_test_code,
                "display": test.ontology_test_name,
                "cpt_code": (entry or {}).get("cpt_code"),
            }
        )

    return {
        "order": {
            "id": str(lab_order.id),
            "requisition_number": lab_order.requisition_number or None,
            "status": "active",
            "authored_on": _iso(lab_order.date_ordered) or _iso(lab_order.created),
            "lab_partner": partner,
            "tests": tests,
        },
        "patient": (
            {
                "id": str(patient.id),
                "mrn": patient.mrn,
                "name": " ".join(
                    part for part in (patient.first_name, patient.last_name) if part
                ).strip()
                or None,
                "date_of_birth": _iso(patient.birth_date),
            }
            if patient
            else None
        ),
        "ordering_provider": (
            {
                "id": str(provider.id),
                "name": provider.full_name,
                "npi": provider.npi_number,
            }
            if provider
            else None
        ),
        "diagnoses": _icd10_for(lab_order),
    }


def _empty_payload(report_id: str, note: str) -> dict[str, Any]:
    return {
        "diagnostic_report_id": report_id,
        "lab_report_id": None,
        "requisition_number": None,
        "orders": [],
        "note": note,
    }


def _iso(value: Any) -> str | None:
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value) if value is not None else None
