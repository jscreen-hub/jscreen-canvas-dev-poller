"""Durable record of which (order, CPT) pairs have already been exported.

A patient can have several open orders, and one order can produce more than one
signed-off report -- a corrected re-issue, or partial results arriving
separately. Without a ledger, every one of those reports would emit the same
CPT for the same order again, and a downstream biller would have no way to tell
the repeat from a legitimate second service.

The ledger is keyed on the ORDER, not the patient and not the report: billing
the same CPT twice against one order is a duplicate, while the same CPT against
a different order is a real second service and must still go out.

Re-exporting the SAME report (after clearing the processed-id store, say) is
idempotent -- a CPT is only suppressed when the report that first billed it is a
different one.
"""

from __future__ import annotations

import json
import os
from typing import Any


class BilledLedger:
    def __init__(self, path: str) -> None:
        self._path = path
        # order key -> {cpt code: report id that first billed it}
        self._billed: dict[str, dict[str, str]] = {}

    def load(self) -> "BilledLedger":
        """Load the ledger from disk. A missing file starts an empty one."""
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            self._billed = {}
        else:
            self._billed = {
                str(order): {str(cpt): str(report) for cpt, report in codes.items()}
                for order, codes in data.items()
            }
        return self

    def billed_by(self, order_key: str, cpt: str) -> str | None:
        """The report that already billed this CPT for this order, if any."""
        return self._billed.get(order_key, {}).get(cpt)

    def record(self, order_key: str, cpt: str, report_id: str) -> None:
        codes = self._billed.setdefault(order_key, {})
        codes.setdefault(cpt, report_id)

    def save(self) -> None:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as handle:
            json.dump(self._billed, handle, indent=2, sort_keys=True)


def order_key(
    order: dict[str, Any] | None, lookup_report: dict[str, Any] | None
) -> str | None:
    """A stable identity for the order a CPT is being billed against.

    Prefers the Canvas LabOrder id from the plugin (authoritative) and falls
    back to the matched ServiceRequest id. Returns None when neither is known --
    in that case the record cannot be safely de-duplicated, and the caller says
    so rather than guessing.
    """
    lab_order_ids = [
        o.get("lab_order_id")
        for o in (lookup_report or {}).get("orders", [])
        if o.get("lab_order_id")
    ]
    if len(lab_order_ids) == 1:
        return f"LabOrder/{lab_order_ids[0]}"
    if order and order.get("id"):
        return f"ServiceRequest/{order['id']}"
    return None


def split_already_billed(
    procedures: list[dict[str, Any]],
    order_key_value: str | None,
    ledger: BilledLedger | None,
    report_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition procedures into (bill now, already billed elsewhere).

    A CPT first billed by a DIFFERENT report for the same order is held back,
    annotated with the report that claimed it. Nothing is discarded -- the
    suppressed list keeps the audit trail.
    """
    if ledger is None or not order_key_value:
        return procedures, []
    fresh: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for procedure in procedures:
        code = str(procedure.get("code") or "")
        prior = ledger.billed_by(order_key_value, code) if code else None
        if prior and prior != report_id:
            suppressed.append(
                {**procedure, "already_billed_on": prior, "order_key": order_key_value}
            )
        else:
            fresh.append(procedure)
    return fresh, suppressed
