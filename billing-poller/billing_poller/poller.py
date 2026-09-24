"""Polling orchestration: sweep -> filter to signed-off -> enrich -> write files.

One cycle:
  1. Search every lab DiagnosticReport (paged).
  2. Keep the ones a provider has signed off: status `final` and an attached
     Lab Results Review encounter that has not been voided.
  3. Skip ids already in the dedupe store.
  4. For each new one, resolve the order behind it and gather the billing data:
     patient demographics, active coverages + payors, ICD-10 from the order,
     CPT from the claim on the review encounter.
  5. Write <output_dir>/<DiagnosticReport id>.json and record the id.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from billing_poller.canvas_client import CanvasClient
from billing_poller.config import (
    COMMITTED_ORDER_STATUSES,
    FINAL_REPORT_STATUS,
    LAB_CATEGORY,
    LAB_REVIEW_ENCOUNTER_TYPES,
    NOTE_ID_EXTENSION,
    VOID_ENCOUNTER_STATUSES,
)
from billing_poller.ledger import BilledLedger, order_key, split_already_billed
from billing_poller.lookup import (
    BillingLookup,
    order_codes_from_lookup,
    procedures_from_compendium,
    procedures_from_lookup,
    unresolved_order_codes,
)
from billing_poller.mapper import build_billing_record, reviewer_reference
from billing_poller.matcher import (
    eligible_orders,
    match_by_order_codes,
    match_order,
    orders_matching_codes,
    report_order_codes,
)
from billing_poller.state import ProcessedStore


def _reference(resource: dict[str, Any], key: str) -> str | None:
    ref = resource.get(key) or {}
    return ref.get("reference") if isinstance(ref, dict) else None


def _note_id(resource: dict[str, Any] | None) -> str | None:
    """The Canvas note id extension, shared by an Encounter and its Claim."""
    if not resource:
        return None
    for extension in resource.get("extension", []):
        if extension.get("url") == NOTE_ID_EXTENSION:
            value = extension.get("valueId")
            return str(value) if value else None
    return None


def is_signed_off(
    report: dict[str, Any], encounter: dict[str, Any] | None
) -> tuple[bool, str]:
    """Has a provider committed a Lab Results Review for this report?

    Returns (signed_off, reason). A Canvas lab report gains its `encounter`
    reference only once a review command is committed in a note, so the presence
    of a live review encounter IS the sign-off.
    """
    if report.get("status") != FINAL_REPORT_STATUS:
        return False, f"report status={report.get('status')}"
    if not encounter:
        return False, "not yet reviewed"
    if encounter.get("status") in VOID_ENCOUNTER_STATUSES:
        return False, f"review note {encounter.get('status')}"
    types = [
        coding.get("display")
        for entry in encounter.get("type", [])
        for coding in entry.get("coding", [])
    ]
    if not any(t in LAB_REVIEW_ENCOUNTER_TYPES for t in types):
        return False, f"encounter type={types or 'unknown'}"
    return True, "signed off"


def find_claim(
    client: CanvasClient, patient_reference: str, encounter: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The claim for the review encounter, if the review produced one.

    Canvas links a Claim to its note through the shared `note-id` extension, and
    to encounters through `item.encounter`. A Lab Results Review often produces
    no claim at all; that is a normal outcome and the caller records it as a
    missing-CPT gap.
    """
    if not encounter:
        return None
    encounter_id = encounter.get("id")
    note_id = _note_id(encounter)
    if not encounter_id and not note_id:
        return None

    for claim in client.search("Claim", patient=patient_reference):
        if note_id and _note_id(claim) == note_id:
            return claim
        for item in claim.get("item", []):
            for entry in item.get("encounter", []):
                reference = entry.get("reference") or ""
                if encounter_id and reference.endswith(encounter_id):
                    return claim
    return None


def enrich_and_build(
    client: CanvasClient,
    report: dict[str, Any],
    encounter: dict[str, Any] | None,
    captured_at: str,
    lookup: BillingLookup | None = None,
    ledger: BilledLedger | None = None,
) -> dict[str, Any]:
    """Gather everything billable for one signed-off result and build the record."""
    patient_reference = _reference(report, "subject") or ""
    patient = client.read_reference(patient_reference)


    # The order behind this result. Canvas offers no basedOn link over FHIR, so
    # narrow to the patient's committed lab orders and let the matcher choose --
    # using the lookup's authoritative order codes when they are available.
    candidates = eligible_orders(
        client.search(
            "ServiceRequest", patient=patient_reference, category=LAB_CATEGORY
        ),
        COMMITTED_ORDER_STATUSES,
        report.get("effectiveDateTime"),
    )
    # Order codes the poster put on `labReport.code.coding`. These drive both
    # the order match and the CPT lookup, so a report that carries them needs no
    # per-report plugin call at all.
    posted_codes = report_order_codes(report)

    # Only fall back to the plugin's per-report chain when the report carries no
    # codes of its own -- that call is a round trip per report, the compendium is
    # one cached call per cycle.
    lookup_report = (
        lookup.report(report["id"])
        if lookup and lookup.enabled and report.get("id") and not posted_codes
        else None
    )

    order, order_match = match_by_order_codes(
        order_codes_from_lookup(lookup_report), candidates
    )
    if order is None:
        order, order_match = match_order(report, candidates)

    # A report whose codes span several orders cannot be represented by the
    # single-order record shape; say so rather than binding to an arbitrary one.
    spanned = orders_matching_codes(set(posted_codes), candidates)
    if len(spanned) > 1:
        order_match = dict(order_match)
        order_match["spans_orders"] = [o.get("id") for o in spanned]

    # CPT: resolve ONLY the codes this signed report carries.
    #
    # No fallback when the report has codes. An order can hold tests that have
    # not resulted yet, and the plugin's order walk would return those too --
    # billing them off a report that does not cover them. If a posted code has
    # no CPT, that is a named gap, never a reason to widen the scope.
    compendium = lookup.compendium() if lookup and lookup.enabled else {}
    if posted_codes:
        report_procedures = procedures_from_compendium(posted_codes, compendium)
        authoritative = report_procedures
    else:
        # Nothing posted on the report: fall back to the order behind it.
        report_procedures = procedures_from_lookup(lookup_report)
        authoritative = None

    # ICD-10 comes off the matched order only, so the diagnoses belong to THIS
    # order rather than to everything the patient has open.
    conditions: list[dict[str, Any]] = []
    for reason in (order or {}).get("reasonReference", []):
        condition = client.read_reference(reason.get("reference"))
        if condition:
            conditions.append(condition)

    ordering_provider = client.read_reference(_reference(order or {}, "requester"))
    reviewing_provider = client.read_reference(reviewer_reference(encounter))

    coverages = [
        coverage
        for coverage in client.search(
            "Coverage", patient=patient_reference, status="active"
        )
        if coverage.get("beneficiary", {}).get("reference") in (patient_reference, None)
    ]
    payors: dict[str, dict[str, Any] | None] = {}
    for coverage in coverages:
        for payor in coverage.get("payor", []):
            reference = payor.get("reference")
            if reference and reference not in payors:
                payors[reference] = client.read_reference(reference)

    claim = find_claim(client, patient_reference, encounter)

    # Hold back CPTs this order was already billed for by an earlier report --
    # a corrected re-issue must not bill the same work twice.
    this_order = order_key(order, lookup_report)
    report_procedures, suppressed = split_already_billed(
        report_procedures, this_order, ledger, str(report.get("id") or "")
    )
    if authoritative is not None:
        authoritative = report_procedures

    return build_billing_record(
        report=report,
        encounter=encounter,
        patient=patient,
        order=order,
        order_match=order_match,
        ordering_provider=ordering_provider,
        reviewing_provider=reviewing_provider,
        conditions=conditions,
        coverages=coverages,
        payors=payors,
        claim=claim,
        captured_at=captured_at,
        extra_procedures=report_procedures,
        authoritative_procedures=authoritative,
        suppressed_procedures=suppressed,
        lab_order_key=this_order,
        lookup=lookup_report,
        unresolved_codes=(
            unresolved_order_codes(posted_codes, compendium) if compendium else []
        ),
        auth_base_url=client.auth_base_url,
    )


def write_record(output_dir: str, record: dict[str, Any]) -> str:
    """Write one billing record as <output_dir>/<report_id>.json; return the path."""
    os.makedirs(output_dir, exist_ok=True)
    report_id = record["result"]["diagnostic_report_id"]
    path = os.path.join(output_dir, f"{report_id}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
    return path


def poll_once(
    client: CanvasClient,
    store: ProcessedStore,
    output_dir: str,
    date_ge: str | None,
    captured_at: str,
    lookup: BillingLookup | None = None,
    ledger: BilledLedger | None = None,
    log: Callable[[str], None] = print,
) -> int:
    """Run one polling cycle. Returns the number of new billing records written."""
    search_params: dict[str, str] = {}
    if date_ge:
        search_params["date"] = f"ge{date_ge}"
    reports = client.search("DiagnosticReport", **search_params)
    log(f"found {len(reports)} lab report(s)")

    written = 0
    skipped = 0
    for report in reports:
        report_id = report.get("id")
        if not report_id or store.contains(report_id):
            continue

        # Read the encounter before the dedupe commit: an unreviewed report must
        # stay unrecorded so it is picked up once the provider signs it off.
        encounter = client.read_reference(_reference(report, "encounter"))
        signed_off, reason = is_signed_off(report, encounter)
        if not signed_off:
            skipped += 1
            continue

        record = enrich_and_build(
            client, report, encounter, captured_at, lookup, ledger
        )
        path = write_record(output_dir, record)
        # Claim the CPTs only once the record is safely on disk, so a failed
        # write cannot leave codes marked billed by a record that never existed.
        if ledger is not None and record.get("lab_order_key"):
            for procedure in record["procedures"]:
                if procedure.get("is_cpt") and procedure.get("code"):
                    ledger.record(
                        record["lab_order_key"], str(procedure["code"]), report_id
                    )
        store.add(report_id)
        written += 1
        gaps = record["data_gaps"]
        log(
            f"wrote {path}"
            + (f"  [gaps: {', '.join(gaps)}]" if gaps else "")
        )

    if written:
        store.save()
        if ledger is not None:
            ledger.save()
    log(f"{written} new billing record(s) written, {skipped} not yet signed off")
    return written
