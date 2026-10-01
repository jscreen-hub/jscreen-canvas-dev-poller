"""Polling orchestration: fetch signed lab orders -> dedupe -> write files."""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from order_poller.source import LabOrderSource
from order_poller.state import ProcessedStore


def order_id_of(record: dict[str, Any]) -> str | None:
    """The Canvas LabOrder UUID this record is keyed on."""
    order = record.get("order") or {}
    order_id = order.get("id")
    return str(order_id) if order_id else None


def write_record(output_dir: str, record: dict[str, Any]) -> str:
    """Write one order record as <output_dir>/<lab_order_id>.json; return the path."""
    os.makedirs(output_dir, exist_ok=True)
    order_id = record["order"]["id"]
    path = os.path.join(output_dir, f"{order_id}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
    return path


def seed(
    source: LabOrderSource,
    store: ProcessedStore,
    log: Callable[[str], None] = print,
) -> int:
    """Mark every currently-signed order as processed WITHOUT writing files.

    Needed once, when cutting over from the old FHIR ServiceRequest source: the
    dedupe store held ServiceRequest ids, and LabOrder ids are a different
    identifier space, so every existing order would otherwise look brand new and
    be re-delivered downstream.
    """
    records = source.signed_orders()
    added = 0
    for record in records:
        order_id = order_id_of(record)
        if order_id and not store.contains(order_id):
            store.add(order_id)
            added += 1
    if added:
        store.save()
    log(f"seeded {added} existing signed order(s) as already-processed")
    return added


def poll_once(
    source: LabOrderSource,
    store: ProcessedStore,
    output_dir: str,
    captured_at: str,
    log: Callable[[str], None] = print,
) -> int:
    """Run one polling cycle. Returns the number of new orders written."""
    records = source.signed_orders()
    log(f"found {len(records)} signed lab order(s)")

    written = 0
    for record in records:
        order_id = order_id_of(record)
        if not order_id or store.contains(order_id):
            continue
        # The plugin supplies order/patient/ordering_provider/diagnoses; the
        # poller owns only the envelope, exactly as it did with the FHIR source.
        output = {"event": "lab_order_polled", "captured_at": captured_at, **record}
        path = write_record(output_dir, output)
        store.add(order_id)
        written += 1
        log(f"wrote {path}")

    if written:
        store.save()
    log(f"{written} new order(s) written")
    return written
