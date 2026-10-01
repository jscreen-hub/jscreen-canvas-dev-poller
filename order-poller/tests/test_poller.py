"""Tests for the polling cycle over the signed lab-order feed."""

import json
import os

import pytest

from order_poller.poller import order_id_of, poll_once, seed, write_record
from order_poller.state import ProcessedStore


class FakeSource:
    """Stands in for LabOrderSource."""

    def __init__(self, records=None, error=None):
        self._records = records or []
        self._error = error
        self.calls = 0

    def signed_orders(self):
        self.calls += 1
        if self._error:
            raise self._error
        return self._records


def record(order_id="lo-1", requisition="REQ-1", codes=("100002",)):
    """A record shaped as the plugin returns it."""
    return {
        "order": {
            "id": order_id,
            "requisition_number": requisition,
            "status": "active",
            "authored_on": "2026-09-09T15:25:33+00:00",
            "lab_partner": "Generic Lab",
            "tests": [
                {
                    "system": "http://loinc.org",
                    "code": c,
                    "display": "Core Panel",
                    "cpt_code": "81220",
                }
                for c in codes
            ],
        },
        "patient": {
            "id": "pat-1",
            "mrn": "MRN-9",
            "name": "Ada Lovelace",
            "date_of_birth": "1980-01-02",
        },
        "ordering_provider": {"id": "prov-1", "name": "Ian Lomas", "npi": "111"},
        "diagnoses": [{"code": "Z1371", "display": "Carrier screening"}],
    }


# -- identity ----------------------------------------------------------------


def test_order_id_is_the_lab_order_uuid():
    assert order_id_of(record(order_id="4b7452dc")) == "4b7452dc"


def test_order_id_none_when_absent():
    assert order_id_of({"order": {}}) is None
    assert order_id_of({}) is None


# -- output shape ------------------------------------------------------------


def test_written_record_keeps_the_legacy_envelope(tmp_path):
    """Downstream consumers must not have to change: same event name, same
    top-level keys, same nesting as the old ServiceRequest-sourced records."""
    store = ProcessedStore(str(tmp_path / "s.json"))
    poll_once(FakeSource([record()]), store, str(tmp_path / "out"), "NOW", log=lambda m: None)

    written = json.loads((tmp_path / "out" / "lo-1.json").read_text(encoding="utf-8"))
    assert written["event"] == "lab_order_polled"
    assert written["captured_at"] == "NOW"
    assert set(written) == {
        "event", "captured_at", "order", "patient", "ordering_provider", "diagnoses",
    }
    assert set(written["patient"]) == {"id", "mrn", "name", "date_of_birth"}
    assert set(written["ordering_provider"]) == {"id", "name", "npi"}
    assert written["diagnoses"] == [{"code": "Z1371", "display": "Carrier screening"}]


def test_order_carries_both_the_uuid_and_the_requisition(tmp_path):
    store = ProcessedStore(str(tmp_path / "s.json"))
    poll_once(
        FakeSource([record(order_id="4b7452dc", requisition="D9B126B04F3")]),
        store, str(tmp_path / "out"), "NOW", log=lambda m: None,
    )
    order = json.loads((tmp_path / "out" / "4b7452dc.json").read_text(encoding="utf-8"))["order"]
    assert order["id"] == "4b7452dc"
    assert order["requisition_number"] == "D9B126B04F3"
    assert order["tests"][0]["cpt_code"] == "81220"


def test_file_is_named_after_the_lab_order_id(tmp_path):
    path = write_record(str(tmp_path / "out"), record(order_id="lo-42"))
    assert os.path.basename(path) == "lo-42.json"


# -- cycle -------------------------------------------------------------------


def test_new_orders_are_written_once(tmp_path):
    source = FakeSource([record("lo-1"), record("lo-2")])
    store = ProcessedStore(str(tmp_path / "s.json"))
    out = str(tmp_path / "out")

    assert poll_once(source, store, out, "NOW", log=lambda m: None) == 2
    assert poll_once(source, store, out, "NOW", log=lambda m: None) == 0
    assert sorted(os.listdir(out)) == ["lo-1.json", "lo-2.json"]


def test_processed_ids_survive_a_restart(tmp_path):
    state = str(tmp_path / "s.json")
    out = str(tmp_path / "out")
    source = FakeSource([record("lo-1")])

    poll_once(source, ProcessedStore(state), out, "NOW", log=lambda m: None)
    assert poll_once(source, ProcessedStore(state).load(), out, "NOW", log=lambda m: None) == 0


def test_records_without_an_id_are_skipped(tmp_path):
    source = FakeSource([{"order": {"requisition_number": "x"}}, record("lo-1")])
    store = ProcessedStore(str(tmp_path / "s.json"))
    assert poll_once(source, store, str(tmp_path / "out"), "NOW", log=lambda m: None) == 1


def test_a_source_failure_propagates(tmp_path):
    """The feed must fail loudly: an empty result would look like 'no new orders'."""
    source = FakeSource(error=RuntimeError("plugin down"))
    with pytest.raises(RuntimeError, match="plugin down"):
        poll_once(source, ProcessedStore(str(tmp_path / "s.json")),
                  str(tmp_path / "out"), "NOW", log=lambda m: None)


# -- cutover seeding ---------------------------------------------------------


def test_seed_marks_existing_orders_without_writing_files(tmp_path):
    """Cutting over from ServiceRequest ids must not re-deliver known orders."""
    out = tmp_path / "out"
    store = ProcessedStore(str(tmp_path / "s.json"))
    source = FakeSource([record("lo-1"), record("lo-2")])

    assert seed(source, store, log=lambda m: None) == 2
    assert not out.exists()
    assert poll_once(source, store, str(out), "NOW", log=lambda m: None) == 0


def test_seed_then_a_genuinely_new_order_still_flows(tmp_path):
    store = ProcessedStore(str(tmp_path / "s.json"))
    seed(FakeSource([record("lo-1")]), store, log=lambda m: None)

    source = FakeSource([record("lo-1"), record("lo-2")])
    assert poll_once(source, store, str(tmp_path / "out"), "NOW", log=lambda m: None) == 1
    assert os.listdir(tmp_path / "out") == ["lo-2.json"]


def test_seed_is_idempotent(tmp_path):
    store = ProcessedStore(str(tmp_path / "s.json"))
    source = FakeSource([record("lo-1")])
    assert seed(source, store, log=lambda m: None) == 1
    assert seed(source, store, log=lambda m: None) == 0
