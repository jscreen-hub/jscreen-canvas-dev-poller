"""Polling orchestration: search -> dedupe -> enrich -> write files."""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Collection

from order_poller.canvas_client import CanvasClient
from order_poller.mapper import build_order_record
from order_poller.state import ProcessedStore


def _reference(resource: dict[str, Any], key: str) -> str | None:
    ref = resource.get(key) or {}
    return ref.get("reference")


def _cached_read(
    client: CanvasClient, reference: str | None, cache: dict[str, dict[str, Any] | None]
) -> dict[str, Any] | None:
    if not reference:
        return None
    if reference not in cache:
        cache[reference] = client.read_reference(reference)
    return cache[reference]


def enrich_and_build(
    client: CanvasClient,
    service_request: dict[str, Any],
    captured_at: str,
    patient_cache: dict[str, dict[str, Any] | None],
    practitioner_cache: dict[str, dict[str, Any] | None],
) -> dict[str, Any]:
    """Read the referenced Patient/Practitioner/Conditions and build the record."""
    patient = _cached_read(client, _reference(service_request, "subject"), patient_cache)
    practitioner = _cached_read(
        client, _reference(service_request, "requester"), practitioner_cache
    )
    conditions: list[dict[str, Any]] = []
    for reason in service_request.get("reasonReference", []):
        condition = client.read_reference(reason.get("reference"))
        if condition:
            conditions.append(condition)
    return build_order_record(
        service_request, patient, practitioner, conditions, captured_at
    )


def write_record(output_dir: str, record: dict[str, Any]) -> str:
    """Write one order record as <output_dir>/<order_id>.json; return the path."""
    os.makedirs(output_dir, exist_ok=True)
    order_id = record["order"]["id"]
    path = os.path.join(output_dir, f"{order_id}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
    return path


def poll_once(
    client: CanvasClient,
    store: ProcessedStore,
    output_dir: str,
    category: str,
    authored_ge: str | None,
    captured_at: str,
    allowed_statuses: Collection[str] | None = None,
    log: Callable[[str], None] = print,
) -> int:
    """Run one polling cycle. Returns the number of new orders written.

    If allowed_statuses is given, ServiceRequests whose status is not in the set
    are skipped (Canvas has no server-side status search param, so this is done
    client-side).
    """
    service_requests = client.search_service_requests(category, authored_ge)
    log(f"found {len(service_requests)} lab ServiceRequest(s) in window")

    patient_cache: dict[str, dict[str, Any] | None] = {}
    practitioner_cache: dict[str, dict[str, Any] | None] = {}
    written = 0
    for service_request in service_requests:
        order_id = service_request.get("id")
        if not order_id or store.contains(order_id):
            continue
        if allowed_statuses is not None and service_request.get("status") not in allowed_statuses:
            log(f"skipping {order_id} (status={service_request.get('status')})")
            continue
        record = enrich_and_build(
            client, service_request, captured_at, patient_cache, practitioner_cache
        )
        path = write_record(output_dir, record)
        store.add(order_id)
        written += 1
        log(f"wrote {path}")

    if written:
        store.save()
    log(f"{written} new order(s) written")
    return written
