"""Tests for poller orchestration."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from order_poller.poller import enrich_and_build, poll_once, write_record
from order_poller.state import ProcessedStore

PATIENT = {"resourceType": "Patient", "id": "patient-1"}
PRACTITIONER = {"resourceType": "Practitioner", "id": "prac-1"}
CONDITION = {"resourceType": "Condition", "id": "cond-1", "code": {"coding": []}}

SERVICE_REQUEST = {
    "id": "sr-1",
    "status": "active",
    "authoredOn": "2026-07-07T14:02:55+00:00",
    "code": {"coding": []},
    "subject": {"reference": "Patient/patient-1"},
    "requester": {"reference": "Practitioner/prac-1"},
    "reasonReference": [{"reference": "Condition/cond-1"}],
}


def _reference_reader() -> MagicMock:
    def read(reference: str | None) -> dict | None:
        return {
            "Patient/patient-1": PATIENT,
            "Practitioner/prac-1": PRACTITIONER,
            "Condition/cond-1": CONDITION,
        }.get(reference or "")

    client = MagicMock()
    client.read_reference.side_effect = read
    return client


def test_write_record_creates_file(tmp_path: Path) -> None:
    record = {"order": {"id": "abc"}, "event": "lab_order_polled"}
    path = write_record(str(tmp_path / "orders"), record)

    assert Path(path).name == "abc.json"
    assert json.loads(Path(path).read_text(encoding="utf-8")) == record


def test_enrich_and_build_caches_reads() -> None:
    client = _reference_reader()
    patient_cache: dict = {}
    practitioner_cache: dict = {}

    enrich_and_build(client, SERVICE_REQUEST, "t", patient_cache, practitioner_cache)
    enrich_and_build(client, SERVICE_REQUEST, "t", patient_cache, practitioner_cache)

    # Patient and Practitioner read once each (cached); Condition read each time.
    reads = [c.args[0] for c in client.read_reference.call_args_list]
    assert reads.count("Patient/patient-1") == 1
    assert reads.count("Practitioner/prac-1") == 1
    assert reads.count("Condition/cond-1") == 2


def test_poll_once_writes_new_orders_and_updates_store(tmp_path: Path) -> None:
    client = _reference_reader()
    client.search_service_requests.return_value = [SERVICE_REQUEST]
    store = ProcessedStore(str(tmp_path / "state.json")).load()
    out_dir = str(tmp_path / "orders")

    written = poll_once(client, store, out_dir, "cat", "2026-07-01", "t", log=lambda m: None)

    assert written == 1
    assert store.contains("sr-1") is True
    record = json.loads((tmp_path / "orders" / "sr-1.json").read_text(encoding="utf-8"))
    assert record["order"]["id"] == "sr-1"
    assert record["patient"]["id"] == "patient-1"
    # State persisted to disk.
    assert ProcessedStore(str(tmp_path / "state.json")).load().contains("sr-1") is True


def test_poll_once_skips_already_processed(tmp_path: Path) -> None:
    client = _reference_reader()
    client.search_service_requests.return_value = [SERVICE_REQUEST]
    store = ProcessedStore(str(tmp_path / "state.json")).load()
    store.add("sr-1")
    out_dir = str(tmp_path / "orders")

    written = poll_once(client, store, out_dir, "cat", "2026-07-01", "t", log=lambda m: None)

    assert written == 0
    assert not (tmp_path / "orders" / "sr-1.json").exists()
    assert client.read_reference.call_args_list == []


def test_poll_once_filters_by_status(tmp_path: Path) -> None:
    draft = dict(SERVICE_REQUEST, id="sr-draft", status="draft")
    client = _reference_reader()
    client.search_service_requests.return_value = [draft, SERVICE_REQUEST]
    store = ProcessedStore(str(tmp_path / "state.json")).load()
    out_dir = str(tmp_path / "orders")

    written = poll_once(
        client,
        store,
        out_dir,
        "cat",
        None,
        "t",
        allowed_statuses=("active", "completed"),
        log=lambda m: None,
    )

    assert written == 1
    assert (tmp_path / "orders" / "sr-1.json").exists()
    assert not (tmp_path / "orders" / "sr-draft.json").exists()
    assert store.contains("sr-draft") is False


def test_poll_once_skips_resource_without_id(tmp_path: Path) -> None:
    client = _reference_reader()
    client.search_service_requests.return_value = [{"status": "active"}]
    store = ProcessedStore(str(tmp_path / "state.json")).load()

    written = poll_once(
        client, store, str(tmp_path / "orders"), "cat", None, "t", log=lambda m: None
    )

    assert written == 0
