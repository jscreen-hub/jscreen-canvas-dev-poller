import json

from billing_poller.ledger import (
    BilledLedger,
    order_key,
    split_already_billed,
)


def cpt(code, source="compendium"):
    return {"code": code, "is_cpt": True, "source": source, "system": "cpt"}


# -- the ledger itself -------------------------------------------------------


def test_missing_file_starts_empty(tmp_path):
    ledger = BilledLedger(str(tmp_path / "nope.json")).load()
    assert ledger.billed_by("ServiceRequest/x", "80053") is None


def test_record_and_reload_roundtrip(tmp_path):
    path = tmp_path / "state" / "billed.json"
    ledger = BilledLedger(str(path)).load()
    ledger.record("ServiceRequest/x", "80053", "dr-1")
    ledger.save()

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "ServiceRequest/x": {"80053": "dr-1"}
    }
    reloaded = BilledLedger(str(path)).load()
    assert reloaded.billed_by("ServiceRequest/x", "80053") == "dr-1"


def test_first_report_keeps_the_claim_on_a_cpt(tmp_path):
    """A later report must not overwrite who billed it first."""
    ledger = BilledLedger(str(tmp_path / "l.json")).load()
    ledger.record("ServiceRequest/x", "80053", "dr-1")
    ledger.record("ServiceRequest/x", "80053", "dr-2")
    assert ledger.billed_by("ServiceRequest/x", "80053") == "dr-1"


def test_the_same_cpt_on_a_different_order_is_tracked_separately(tmp_path):
    ledger = BilledLedger(str(tmp_path / "l.json")).load()
    ledger.record("ServiceRequest/x", "80053", "dr-1")
    assert ledger.billed_by("ServiceRequest/y", "80053") is None


# -- order identity ----------------------------------------------------------


def test_order_key_prefers_the_canvas_lab_order_id():
    lookup = {"orders": [{"lab_order_id": "abc"}]}
    assert order_key({"id": "sr-1"}, lookup) == "LabOrder/abc"


def test_order_key_falls_back_to_the_service_request():
    assert order_key({"id": "sr-1"}, None) == "ServiceRequest/sr-1"


def test_order_key_is_none_when_nothing_resolved():
    """No order means no safe de-duplication -- the record says so."""
    assert order_key(None, None) is None


def test_order_key_ignores_an_ambiguous_multi_order_lookup():
    """Two lab orders on one report cannot key a single ledger entry."""
    lookup = {"orders": [{"lab_order_id": "a"}, {"lab_order_id": "b"}]}
    assert order_key({"id": "sr-1"}, lookup) == "ServiceRequest/sr-1"


# -- suppression -------------------------------------------------------------


def test_a_repeat_report_has_its_cpt_held_back(tmp_path):
    """The core protection: order re-reported, CPT already billed."""
    ledger = BilledLedger(str(tmp_path / "l.json")).load()
    ledger.record("ServiceRequest/x", "80053", "dr-1")

    fresh, suppressed = split_already_billed(
        [cpt("80053")], "ServiceRequest/x", ledger, "dr-2"
    )
    assert fresh == []
    assert suppressed[0]["code"] == "80053"
    assert suppressed[0]["already_billed_on"] == "dr-1"


def test_re_exporting_the_same_report_is_idempotent(tmp_path):
    """Clearing the processed-id store must not turn a record into a duplicate."""
    ledger = BilledLedger(str(tmp_path / "l.json")).load()
    ledger.record("ServiceRequest/x", "80053", "dr-1")

    fresh, suppressed = split_already_billed(
        [cpt("80053")], "ServiceRequest/x", ledger, "dr-1"
    )
    assert [p["code"] for p in fresh] == ["80053"]
    assert suppressed == []


def test_a_new_cpt_on_a_re_reported_order_still_bills(tmp_path):
    """Partial results: the second report adds a test the first did not cover."""
    ledger = BilledLedger(str(tmp_path / "l.json")).load()
    ledger.record("ServiceRequest/x", "80053", "dr-1")

    fresh, suppressed = split_already_billed(
        [cpt("80053"), cpt("81443")], "ServiceRequest/x", ledger, "dr-2"
    )
    assert [p["code"] for p in fresh] == ["81443"]
    assert [p["code"] for p in suppressed] == ["80053"]


def test_the_same_cpt_for_a_different_order_is_not_suppressed(tmp_path):
    """A genuine second service on another order must still be billed."""
    ledger = BilledLedger(str(tmp_path / "l.json")).load()
    ledger.record("ServiceRequest/x", "80053", "dr-1")

    fresh, _ = split_already_billed([cpt("80053")], "ServiceRequest/y", ledger, "dr-2")
    assert [p["code"] for p in fresh] == ["80053"]


def test_without_an_order_key_nothing_is_suppressed(tmp_path):
    """Better to emit and flag than to drop a code we cannot attribute."""
    ledger = BilledLedger(str(tmp_path / "l.json")).load()
    ledger.record("ServiceRequest/x", "80053", "dr-1")
    fresh, suppressed = split_already_billed([cpt("80053")], None, ledger, "dr-2")
    assert len(fresh) == 1
    assert suppressed == []


def test_without_a_ledger_nothing_is_suppressed():
    fresh, suppressed = split_already_billed([cpt("80053")], "ServiceRequest/x", None, "dr-1")
    assert len(fresh) == 1
    assert suppressed == []
