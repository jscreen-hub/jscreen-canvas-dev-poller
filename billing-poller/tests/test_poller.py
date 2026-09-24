import json
import os

from billing_poller.poller import (
    enrich_and_build,
    find_claim,
    is_signed_off,
    poll_once,
    write_record,
)
from billing_poller.state import ProcessedStore
from tests.conftest import lab_report, review_encounter


class FakeClient:
    """Stands in for CanvasClient: searches come from `results`, reads from `reads`."""

    def __init__(self, results=None, reads=None, auth_base_url="https://jlab-dev.canvasmedical.com"):
        self.results = results or {}
        self.reads = reads or {}
        self.searches = []
        self.read_refs = []
        self.auth_base_url = auth_base_url

    def search(self, resource_type, **params):
        self.searches.append((resource_type, params))
        value = self.results.get(resource_type, [])
        return value(params) if callable(value) else value

    def read_reference(self, reference):
        self.read_refs.append(reference)
        if not reference or "/" not in reference:
            return None
        return self.reads.get(reference)


# -- sign-off gate -----------------------------------------------------------


def test_signed_off_requires_a_committed_review():
    ok, reason = is_signed_off(lab_report(), review_encounter())
    assert ok is True
    assert reason == "signed off"


def test_unreviewed_report_is_not_signed_off():
    ok, reason = is_signed_off(lab_report(encounter=None), None)
    assert ok is False
    assert reason == "not yet reviewed"


def test_non_final_report_is_not_signed_off():
    ok, reason = is_signed_off(lab_report(status="entered-in-error"), review_encounter())
    assert ok is False
    assert "entered-in-error" in reason


def test_cancelled_review_note_is_not_signed_off():
    """A deleted review note leaves its encounter attached to the report."""
    ok, reason = is_signed_off(lab_report(), review_encounter(status="cancelled"))
    assert ok is False
    assert "cancelled" in reason


def test_in_progress_review_still_counts():
    """Canvas leaves a committed review note's encounter in-progress until it
    is locked; the review has still been committed."""
    ok, _ = is_signed_off(lab_report(), review_encounter(status="in-progress"))
    assert ok is True


def test_other_encounter_types_are_not_sign_off():
    office_visit = review_encounter(type=[{"coding": [{"display": "Office Visit"}]}])
    ok, reason = is_signed_off(lab_report(), office_visit)
    assert ok is False
    assert "Office Visit" in reason


# -- claim lookup ------------------------------------------------------------


def test_find_claim_matches_on_the_shared_note_id():
    claim = {
        "id": "claim-1",
        "extension": [
            {
                "url": "http://schemas.canvasmedical.com/fhir/extensions/note-id",
                "valueId": "note-1",
            }
        ],
    }
    client = FakeClient(results={"Claim": [{"id": "other"}, claim]})
    assert find_claim(client, "Patient/pat-1", review_encounter())["id"] == "claim-1"


def test_find_claim_matches_on_an_item_encounter_reference():
    claim = {
        "id": "claim-2",
        "item": [{"encounter": [{"reference": "Encounter/enc-1"}]}],
    }
    encounter = review_encounter(extension=[])
    client = FakeClient(results={"Claim": [claim]})
    assert find_claim(client, "Patient/pat-1", encounter)["id"] == "claim-2"


def test_find_claim_returns_none_when_the_review_produced_no_claim():
    client = FakeClient(results={"Claim": [{"id": "unrelated"}]})
    assert find_claim(client, "Patient/pat-1", review_encounter()) is None


def test_find_claim_without_an_encounter():
    assert find_claim(FakeClient(), "Patient/pat-1", None) is None


# -- enrichment --------------------------------------------------------------


def _enrichment_client():
    order = {
        "id": "sr-1",
        "status": "active",
        "authoredOn": "2026-08-01",
        "code": {"coding": [{"system": "http://loinc.org", "code": "L1", "display": "Core Panel"}]},
        "requester": {"reference": "Practitioner/prac-1"},
        "reasonReference": [{"reference": "Condition/cond-1"}],
    }
    coverage = {
        "id": "cov-1",
        "status": "active",
        "order": 1,
        "beneficiary": {"reference": "Patient/pat-1"},
        "payor": [{"reference": "Organization/org-1", "display": "Aetna"}],
    }
    return FakeClient(
        results={
            "ServiceRequest": [order, {"id": "draft-1", "status": "draft", "authoredOn": "2026-08-20"}],
            "Coverage": [coverage],
            "Claim": [],
        },
        reads={
            "Patient/pat-1": {"id": "pat-1", "name": [{"given": ["Ada"], "family": "Lovelace"}]},
            "Practitioner/prac-1": {"id": "prac-1", "name": [{"family": "Lomas"}]},
            "Practitioner/rev-1": {"id": "rev-1", "name": [{"family": "Reviewer"}]},
            "Condition/cond-1": {
                "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "K219", "display": "GERD"}]}
            },
            "Organization/org-1": {"id": "org-1", "name": "Aetna"},
        },
    )


def test_enrich_builds_a_full_billing_record():
    client = _enrichment_client()
    record = enrich_and_build(client, lab_report(), review_encounter(), "NOW")
    assert record["order"]["id"] == "sr-1"
    assert record["order"]["ordering_provider"]["name"] == "Lomas"
    assert record["sign_off"]["reviewed_by"]["name"] == "Reviewer"
    assert record["diagnoses"] == [{"code": "K219", "display": "GERD"}]
    assert record["insurance"][0]["payor"]["name"] == "Aetna"
    assert record["captured_at"] == "NOW"


def test_enrich_includes_the_patients_ccda_url():
    """`build_ccda_url` is given the client's auth host, not the FHIR host."""
    client = _enrichment_client()
    record = enrich_and_build(client, lab_report(), review_encounter(), "NOW")
    assert record["patient"]["ccda_url"] == (
        "https://jlab-dev.canvasmedical.com/api/data-export/ccda/pat-1"
        "?document=continuity"
    )


def test_enrich_scopes_diagnoses_to_the_matched_order():
    """A patient with several orders must not have every diagnosis swept in."""
    client = _enrichment_client()
    client.results["ServiceRequest"] = client.results["ServiceRequest"] + [
        {
            "id": "sr-other",
            "status": "active",
            "authoredOn": "2026-08-02",
            "code": {"coding": [{"display": "Renal Panel"}]},
            "reasonReference": [{"reference": "Condition/other"}],
        }
    ]
    client.reads["Condition/other"] = {
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "N189", "display": "CKD"}]}
    }
    record = enrich_and_build(client, lab_report(), review_encounter(), "NOW")
    assert record["order"]["id"] == "sr-1"
    assert [d["code"] for d in record["diagnoses"]] == ["K219"]


def test_enrich_only_searches_the_reports_own_patient():
    client = _enrichment_client()
    enrich_and_build(client, lab_report(), review_encounter(), "NOW")
    for resource_type, params in client.searches:
        assert params.get("patient") == "Patient/pat-1", resource_type


def test_enrich_survives_a_patient_with_no_orders():
    client = FakeClient(results={"ServiceRequest": [], "Coverage": [], "Claim": []})
    record = enrich_and_build(client, lab_report(), review_encounter(), "NOW")
    assert record["order"] is None
    assert "no_order_match" in record["data_gaps"]


# -- writing -----------------------------------------------------------------


def test_write_record_names_the_file_after_the_report(tmp_path):
    record = {"result": {"diagnostic_report_id": "dr-42"}}
    path = write_record(str(tmp_path / "billing"), record)
    assert os.path.basename(path) == "dr-42.json"
    with open(path, encoding="utf-8") as handle:
        assert json.load(handle) == record


# -- cycle -------------------------------------------------------------------


def _poll_client(reports):
    client = _enrichment_client()
    client.results["DiagnosticReport"] = reports
    client.reads["Encounter/enc-1"] = review_encounter()
    return client


def test_poll_once_writes_only_signed_off_reports(tmp_path):
    reports = [
        lab_report(id="signed"),
        lab_report(id="unreviewed", encounter=None),
        lab_report(id="voided", status="entered-in-error"),
    ]
    client = _poll_client(reports)
    store = ProcessedStore(str(tmp_path / "state.json"))
    written = poll_once(client, store, str(tmp_path / "out"), None, "NOW", log=lambda m: None)
    assert written == 1
    assert os.listdir(tmp_path / "out") == ["signed.json"]


def test_poll_once_skips_already_processed_reports(tmp_path):
    client = _poll_client([lab_report(id="dr-1")])
    store = ProcessedStore(str(tmp_path / "state.json"))
    store.add("dr-1")
    assert poll_once(client, store, str(tmp_path / "out"), None, "NOW", log=lambda m: None) == 0


def test_poll_once_persists_ids_so_the_next_cycle_skips_them(tmp_path):
    state_file = str(tmp_path / "state.json")
    out = str(tmp_path / "out")
    client = _poll_client([lab_report(id="dr-1")])
    assert poll_once(client, ProcessedStore(state_file), out, None, "NOW", log=lambda m: None) == 1
    reloaded = ProcessedStore(state_file).load()
    assert poll_once(client, reloaded, out, None, "NOW", log=lambda m: None) == 0


def test_unreviewed_report_is_not_recorded_so_it_returns_after_sign_off(tmp_path):
    """The whole point of the feed: a report seen before review must be picked
    up on the cycle after the provider signs it."""
    state_file = str(tmp_path / "state.json")
    out = str(tmp_path / "out")
    store = ProcessedStore(state_file)

    pending = _poll_client([lab_report(id="dr-1", encounter=None)])
    assert poll_once(pending, store, out, None, "NOW", log=lambda m: None) == 0

    signed = _poll_client([lab_report(id="dr-1")])
    assert poll_once(signed, store, out, None, "LATER", log=lambda m: None) == 1


def test_poll_once_applies_the_date_filter_when_given(tmp_path):
    client = _poll_client([])
    poll_once(client, ProcessedStore(str(tmp_path / "s.json")), str(tmp_path), "2026-01-01", "NOW", log=lambda m: None)
    assert ("DiagnosticReport", {"date": "ge2026-01-01"}) in client.searches


def test_poll_once_sweeps_everything_without_a_date_filter(tmp_path):
    client = _poll_client([])
    poll_once(client, ProcessedStore(str(tmp_path / "s.json")), str(tmp_path), None, "NOW", log=lambda m: None)
    assert ("DiagnosticReport", {}) in client.searches


def test_poll_once_logs_data_gaps(tmp_path):
    messages = []
    client = FakeClient(
        results={"DiagnosticReport": [lab_report()], "ServiceRequest": [], "Coverage": [], "Claim": []},
        reads={"Encounter/enc-1": review_encounter()},
    )
    poll_once(client, ProcessedStore(str(tmp_path / "s.json")), str(tmp_path / "o"), None, "NOW", log=messages.append)
    assert any("gaps:" in m and "no_order_match" in m for m in messages)


def test_poll_once_ignores_reports_without_an_id(tmp_path):
    client = _poll_client([{"status": "final"}])
    assert poll_once(client, ProcessedStore(str(tmp_path / "s.json")), str(tmp_path), None, "NOW", log=lambda m: None) == 0


# -- the posted-order-codes flow (end to end) --------------------------------


class FakeLookup:
    """Stands in for the compendium half of the lab_billing_lookup plugin."""

    def __init__(self, compendium, enabled=True):
        self._compendium = compendium
        self.enabled = enabled
        self.report_calls = 0

    def compendium(self):
        return self._compendium

    def report(self, report_id):
        self.report_calls += 1
        return None


COMPENDIUM = {
    "100002": {"order_code": "100002", "order_name": "Core Panel", "cpt_code": "80053"},
    "100833": {"order_code": "100833", "order_name": "Carrier Screen", "cpt_code": "81443"},
    "100900": {"order_code": "100900", "order_name": "Uncoded", "cpt_code": None},
}


def posted_report(*codes, report_id="dr-1"):
    """A report as the poster will send it: order codes, no CPT."""
    return lab_report(
        id=report_id,
        code={"text": "Core Panel", "coding": [
            {"system": "http://loinc.org", "code": c} for c in codes
        ]},
    )


def _client_with_order(*order_codes, order_id="sr-1"):
    client = _enrichment_client()
    client.results["ServiceRequest"] = [{
        "id": order_id,
        "status": "active",
        "authoredOn": "2026-08-01",
        "code": {"coding": [{"system": "http://loinc.org", "code": c} for c in order_codes]},
        "requester": {"reference": "Practitioner/prac-1"},
        "reasonReference": [{"reference": "Condition/cond-1"}],
    }]
    return client


def test_one_posted_code_gives_an_exact_order_match_and_its_cpt():
    client = _client_with_order("100002")
    record = enrich_and_build(
        client, posted_report("100002"), review_encounter(), "NOW",
        FakeLookup(COMPENDIUM),
    )
    assert record["order"]["id"] == "sr-1"
    assert record["order_match"]["method"] == "code"
    assert record["order_match"]["confidence"] == "high"
    assert [p["code"] for p in record["procedures"]] == ["80053"]
    assert "no_cpt_codes" not in record["data_gaps"]
    assert "no_order_match" not in record["data_gaps"]


def test_many_posted_codes_on_one_order_give_every_cpt():
    client = _client_with_order("100002", "100833")
    record = enrich_and_build(
        client, posted_report("100002", "100833"), review_encounter(), "NOW",
        FakeLookup(COMPENDIUM),
    )
    assert record["order"]["id"] == "sr-1"
    assert [p["code"] for p in record["procedures"]] == ["80053", "81443"]
    assert "no_cpt_codes" not in record["data_gaps"]


def test_a_code_with_no_cpt_configured_is_named_in_the_gaps():
    client = _client_with_order("100002", "100900")
    record = enrich_and_build(
        client, posted_report("100002", "100900"), review_encounter(), "NOW",
        FakeLookup(COMPENDIUM),
    )
    assert [p["code"] for p in record["procedures"]] == ["80053"]
    assert any("cpt_unresolved_for_100900" in g for g in record["data_gaps"])
    assert "no_cpt_codes" not in record["data_gaps"]  # partially billed, not unbilled


def test_a_report_spanning_two_orders_is_flagged_not_silently_bound():
    client = _enrichment_client()
    client.results["ServiceRequest"] = [
        {"id": "sr-a", "status": "active", "authoredOn": "2026-08-01",
         "code": {"coding": [{"system": "http://loinc.org", "code": "100002"}]}},
        {"id": "sr-b", "status": "active", "authoredOn": "2026-08-02",
         "code": {"coding": [{"system": "http://loinc.org", "code": "100833"}]}},
    ]
    record = enrich_and_build(
        client, posted_report("100002", "100833"), review_encounter(), "NOW",
        FakeLookup(COMPENDIUM),
    )
    assert "report_spans_multiple_orders" in record["data_gaps"]
    assert set(record["order_match"]["spans_orders"]) == {"sr-a", "sr-b"}
    # both orders' CPTs are still captured
    assert [p["code"] for p in record["procedures"]] == ["80053", "81443"]


def test_posted_codes_need_no_per_report_plugin_call():
    """The compendium is cached; a posted-code report costs no extra round trip."""
    lookup = FakeLookup(COMPENDIUM)
    enrich_and_build(
        _client_with_order("100002"), posted_report("100002"),
        review_encounter(), "NOW", lookup,
    )
    assert lookup.report_calls == 0


def test_without_the_plugin_posted_codes_still_match_the_order():
    """Order linking is local; only CPT needs the compendium."""
    record = enrich_and_build(
        _client_with_order("100002"), posted_report("100002"),
        review_encounter(), "NOW", None,
    )
    assert record["order"]["id"] == "sr-1"
    assert record["order_match"]["confidence"] == "high"
    assert "no_cpt_codes" in record["data_gaps"]


# -- one order ready, others outstanding -------------------------------------


def _multi_order_client():
    """A patient with three committed orders; only one has resulted."""
    client = _enrichment_client()
    client.results["ServiceRequest"] = [
        {"id": "sr-ready", "status": "active", "authoredOn": "2026-08-01",
         "code": {"coding": [{"system": "http://loinc.org", "code": "100002"}]},
         "requester": {"reference": "Practitioner/prac-1"},
         "reasonReference": [{"reference": "Condition/cond-1"}]},
        {"id": "sr-pending-a", "status": "active", "authoredOn": "2026-08-02",
         "code": {"coding": [{"system": "http://loinc.org", "code": "100833"}]}},
        {"id": "sr-pending-b", "status": "active", "authoredOn": "2026-08-03",
         "code": {"coding": [{"system": "http://loinc.org", "code": "100900"}]}},
    ]
    return client


def test_only_the_resulted_orders_cpt_is_billed():
    """Three open orders, one report: bill that order's CPT and nothing else."""
    record = enrich_and_build(
        _multi_order_client(), posted_report("100002"), review_encounter(), "NOW",
        FakeLookup(COMPENDIUM),
    )
    assert record["order"]["id"] == "sr-ready"
    assert [p["code"] for p in record["procedures"]] == ["80053"]
    # 81443 belongs to sr-pending-a and must not appear
    assert "81443" not in [p["code"] for p in record["procedures"]]


def test_the_pending_orders_diagnoses_are_not_swept_in():
    record = enrich_and_build(
        _multi_order_client(), posted_report("100002"), review_encounter(), "NOW",
        FakeLookup(COMPENDIUM),
    )
    assert [d["code"] for d in record["diagnoses"]] == ["K219"]


def test_a_posted_code_with_no_cpt_does_not_fall_back_to_the_order_walk():
    """The scope must stay the report's codes even when they resolve to nothing,
    or an unresulted test on the same order could get billed."""
    class WalkLookup(FakeLookup):
        def report(self, report_id):
            self.report_calls += 1
            return {"orders": [{"lab_order_id": "lo-1", "tests": [
                {"order_code": "100833", "cpt_code": "81443"}]}]}

    record = enrich_and_build(
        _multi_order_client(), posted_report("100900"), review_encounter(), "NOW",
        WalkLookup(COMPENDIUM),
    )
    assert record["procedures"] == []
    assert "no_cpt_codes" in record["data_gaps"]


# -- double-bill protection --------------------------------------------------


def test_a_reissued_report_does_not_bill_the_same_cpt_twice(tmp_path):
    from billing_poller.ledger import BilledLedger

    ledger = BilledLedger(str(tmp_path / "l.json")).load()
    first = enrich_and_build(
        _multi_order_client(), posted_report("100002", report_id="dr-1"),
        review_encounter(), "NOW", FakeLookup(COMPENDIUM), ledger,
    )
    assert [p["code"] for p in first["procedures"]] == ["80053"]
    for p in first["procedures"]:
        ledger.record(first["lab_order_key"], p["code"], "dr-1")

    # Same order, corrected report re-issued under a new id.
    second = enrich_and_build(
        _multi_order_client(), posted_report("100002", report_id="dr-2"),
        review_encounter(), "NOW", FakeLookup(COMPENDIUM), ledger,
    )
    assert second["procedures"] == []
    assert second["suppressed_procedures"][0]["already_billed_on"] == "dr-1"
    assert "duplicate_cpt_suppressed" in second["data_gaps"]
    assert "no_cpt_codes" not in second["data_gaps"]  # a repeat, not a gap


def test_poll_once_records_billed_cpts_only_after_writing(tmp_path):
    from billing_poller.ledger import BilledLedger

    ledger = BilledLedger(str(tmp_path / "l.json")).load()
    client = _multi_order_client()
    client.results["DiagnosticReport"] = [posted_report("100002", report_id="dr-1")]
    client.reads["Encounter/enc-1"] = review_encounter()

    poll_once(
        client, ProcessedStore(str(tmp_path / "s.json")), str(tmp_path / "out"),
        None, "NOW", lookup=FakeLookup(COMPENDIUM), ledger=ledger, log=lambda m: None,
    )
    assert ledger.billed_by("ServiceRequest/sr-ready", "80053") == "dr-1"


def test_a_second_order_bills_its_own_cpt_normally(tmp_path):
    """Suppression is per order -- a different order still bills."""
    from billing_poller.ledger import BilledLedger

    ledger = BilledLedger(str(tmp_path / "l.json")).load()
    ledger.record("ServiceRequest/sr-ready", "80053", "dr-1")

    client = _multi_order_client()
    client.results["ServiceRequest"][1]["code"]["coding"] = [
        {"system": "http://loinc.org", "code": "100833"}
    ]
    record = enrich_and_build(
        client, posted_report("100833", report_id="dr-9"), review_encounter(), "NOW",
        FakeLookup(COMPENDIUM), ledger,
    )
    assert record["order"]["id"] == "sr-pending-a"
    assert [p["code"] for p in record["procedures"]] == ["81443"]
