from billing_poller.matcher import (
    orders_matching_codes,
    report_order_codes,
    match_by_order_codes,
    eligible_orders,
    match_order,
    order_names,
    report_names,
)


def order(order_id, authored, status="active", codings=None, text=None):
    resource = {"id": order_id, "status": status, "authoredOn": authored}
    code = {}
    if codings:
        code["coding"] = codings
    if text:
        code["text"] = text
    if code:
        resource["code"] = code
    return resource


# -- candidate narrowing -----------------------------------------------------


def test_eligible_orders_drops_uncommitted():
    orders = [
        order("draft", "2026-08-01", status="draft"),
        order("void", "2026-08-01", status="entered-in-error"),
        order("live", "2026-08-01"),
    ]
    result = eligible_orders(orders, ("active", "completed"), "2026-08-26")
    assert [o["id"] for o in result] == ["live"]


def test_eligible_orders_drops_orders_authored_after_the_result():
    orders = [order("before", "2026-08-01"), order("after", "2026-09-01")]
    result = eligible_orders(orders, ("active", "completed"), "2026-08-26")
    assert [o["id"] for o in result] == ["before"]


def test_eligible_orders_keeps_same_day_order():
    """An order signed the same day the specimen was collected still counts,
    even when the timestamps run backwards."""
    orders = [order("same-day", "2026-08-26T18:00:00+00:00")]
    result = eligible_orders(
        orders, ("active", "completed"), "2026-08-26T04:00:00+00:00"
    )
    assert [o["id"] for o in result] == ["same-day"]


def test_eligible_orders_sorts_most_recent_first():
    orders = [
        order("old", "2026-07-01"),
        order("new", "2026-08-20"),
        order("mid", "2026-08-01"),
    ]
    result = eligible_orders(orders, ("active", "completed"), "2026-08-26")
    assert [o["id"] for o in result] == ["new", "mid", "old"]


def test_eligible_orders_without_result_date_keeps_everything():
    orders = [order("a", "2026-08-01"), order("b", "2026-09-01")]
    assert len(eligible_orders(orders, ("active", "completed"), None)) == 2


# -- name parsing ------------------------------------------------------------


def test_report_names_splits_a_multi_test_report():
    report = {"code": {"text": "Core Panel, Test, Test 2"}}
    assert report_names(report) == ["Core Panel", "Test", "Test 2"]


def test_report_names_empty_when_untitled():
    assert report_names({"code": {}}) == []


def test_order_names_uses_display_and_text():
    resource = order("a", "2026-08-01", codings=[{"display": "Lipid Panel"}], text="LP")
    assert order_names(resource) == ["Lipid Panel", "LP"]


# -- matching ----------------------------------------------------------------


def test_match_on_shared_coding_is_high_confidence():
    report = {
        "code": {
            "coding": [{"system": "http://loinc.org", "code": "24325-3"}],
            "text": "Hepatic Function Panel",
        }
    }
    candidates = [
        order("wrong", "2026-08-20", codings=[{"system": "http://loinc.org", "code": "X"}]),
        order(
            "right",
            "2026-08-01",
            codings=[{"system": "http://loinc.org", "code": "24325-3"}],
        ),
    ]
    matched, meta = match_order(report, candidates)
    assert matched["id"] == "right"
    assert meta["method"] == "code"
    assert meta["confidence"] == "high"
    assert meta["matched_on"] == ["http://loinc.org|24325-3"]


def test_match_ignores_same_code_in_a_different_system():
    report = {"code": {"coding": [{"system": "http://loinc.org", "code": "100"}]}}
    candidates = [
        order("other", "2026-08-01", codings=[{"system": "http://snomed.info/sct", "code": "100"}])
    ]
    _, meta = match_order(report, candidates)
    assert meta["method"] == "sole_open_order"


def test_match_on_test_name_when_the_report_has_no_coding():
    report = {"code": {"text": "Core Panel, Test 2"}}
    candidates = [
        order("lipid", "2026-08-20", codings=[{"display": "Lipid Panel"}]),
        order("core", "2026-08-01", codings=[{"display": "Core Panel"}]),
    ]
    matched, meta = match_order(report, candidates)
    assert matched["id"] == "core"
    assert meta["method"] == "name"
    assert meta["confidence"] == "medium"


def test_name_match_ignores_case_punctuation_and_filler_words():
    report = {"code": {"text": "HEPATIC-FUNCTION panel"}}
    candidates = [
        order("a", "2026-08-20", codings=[{"display": "Lipid"}]),
        order("b", "2026-08-01", codings=[{"display": "Hepatic Function Panel"}]),
    ]
    matched, meta = match_order(report, candidates)
    assert matched["id"] == "b"
    assert meta["method"] == "name"


def test_partial_name_overlap_does_not_count_as_a_name_match():
    """'Core Panel' must not match 'Core Panel Extended' -- they are different
    tests, and a wrong order means a wrong claim."""
    report = {"code": {"text": "Core Panel"}}
    candidates = [
        order("a", "2026-08-20", codings=[{"display": "Core Panel Extended"}]),
        order("b", "2026-08-01", codings=[{"display": "Renal"}]),
    ]
    matched, meta = match_order(report, candidates)
    assert meta["method"] == "nearest_prior"
    assert meta["confidence"] == "low"
    assert matched["id"] == "a"


def test_single_candidate_is_medium_confidence():
    report = {"code": {"text": "Anything At All"}}
    matched, meta = match_order(report, [order("only", "2026-08-01")])
    assert matched["id"] == "only"
    assert meta["method"] == "sole_open_order"
    assert meta["confidence"] == "medium"


def test_falls_back_to_the_nearest_prior_order():
    report = {"code": {"text": "Unrecognized"}}
    candidates = [order("new", "2026-08-20"), order("old", "2026-07-01")]
    matched, meta = match_order(report, candidates)
    assert matched["id"] == "new"
    assert meta["method"] == "nearest_prior"
    assert meta["confidence"] == "low"
    assert meta["candidates_considered"] == 2


def test_no_candidates_yields_no_match():
    matched, meta = match_order({"code": {"text": "Core Panel"}}, [])
    assert matched is None
    assert meta["method"] == "none"
    assert meta["confidence"] == "none"
    assert meta["candidates_considered"] == 0


# -- exact matching via the lab_billing_lookup plugin ------------------------


def test_lookup_order_codes_give_an_exact_match():
    """Codes from the plugin come from Canvas's own foreign keys, so a hit is
    the order, not an inference."""
    candidates = [
        order("wrong", "2026-08-20", codings=[{"code": "999999"}]),
        order("right", "2026-08-01", codings=[{"code": "100002"}]),
    ]
    matched, meta = match_by_order_codes({"100002"}, candidates)
    assert matched["id"] == "right"
    assert meta["method"] == "lookup_order_code"
    assert meta["confidence"] == "exact"
    assert meta["matched_on"] == ["100002"]


def test_lookup_match_beats_recency():
    """The exact code wins even though another candidate is newer."""
    candidates = [
        order("newer", "2026-08-25", codings=[{"code": "555"}]),
        order("older", "2026-07-01", codings=[{"code": "100002"}]),
    ]
    matched, _ = match_by_order_codes({"100002"}, candidates)
    assert matched["id"] == "older"


def test_lookup_match_defers_when_no_candidate_carries_the_code():
    matched, meta = match_by_order_codes(
        {"100002"}, [order("a", "2026-08-01", codings=[{"code": "999"}])]
    )
    assert matched is None
    assert meta["method"] == "none"


def test_lookup_match_defers_when_the_plugin_is_unavailable():
    matched, _ = match_by_order_codes(set(), [order("a", "2026-08-01")])
    assert matched is None


def test_lookup_match_with_no_candidates():
    matched, meta = match_by_order_codes({"100002"}, [])
    assert matched is None
    assert meta["candidates_considered"] == 0


# -- posted order codes ------------------------------------------------------


def test_report_order_codes_returns_every_posted_code():
    report = {"code": {"coding": [
        {"system": "http://loinc.org", "code": "100002"},
        {"system": "http://loinc.org", "code": "100833"},
    ]}}
    assert report_order_codes(report) == ["100002", "100833"]


def test_report_order_codes_excludes_cpt_codings():
    """A CPT is a billing code, not an order code -- it must not be matched on."""
    report = {"code": {"coding": [
        {"system": "http://loinc.org", "code": "100002"},
        {"system": "http://www.ama-assn.org/go/cpt", "code": "80053"},
    ]}}
    assert report_order_codes(report) == ["100002"]


def test_report_order_codes_empty_when_none_posted():
    assert report_order_codes({"code": {"text": "Core Panel"}}) == []


def test_orders_matching_codes_finds_a_single_order():
    candidates = [
        order("a", "2026-08-01", codings=[{"code": "100002"}]),
        order("b", "2026-08-02", codings=[{"code": "999"}]),
    ]
    assert [o["id"] for o in orders_matching_codes({"100002"}, candidates)] == ["a"]


def test_orders_matching_codes_detects_a_report_spanning_two_orders():
    candidates = [
        order("a", "2026-08-01", codings=[{"code": "100002"}]),
        order("b", "2026-08-02", codings=[{"code": "100833"}]),
    ]
    matched = orders_matching_codes({"100002", "100833"}, candidates)
    assert {o["id"] for o in matched} == {"a", "b"}


def test_all_codes_on_one_order_is_not_a_span():
    candidates = [
        order("a", "2026-08-01", codings=[{"code": "100002"}, {"code": "100833"}]),
    ]
    assert len(orders_matching_codes({"100002", "100833"}, candidates)) == 1
