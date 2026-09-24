import httpx
import pytest

from billing_poller.lookup import (
    procedures_from_compendium,
    unresolved_order_codes,
    BillingLookup,
    order_codes_from_lookup,
    procedures_from_lookup,
)

BASE = "https://jlab-dev.canvasmedical.com/plugin-io/api/lab_billing_lookup/billing"

REPORT = {
    "diagnostic_report_id": "dr-1",
    "lab_report_id": "lab-1",
    "requisition_number": "REQ-9",
    "orders": [
        {
            "lab_order_id": "order-1",
            "requisition_number": "REQ-9",
            "lab_partner": "Labcorp",
            "tests": [
                {"name": "Core Panel", "order_code": "100002",
                 "cpt_code": "80053", "compendium_name": "Core Panel"},
                {"name": "No CPT", "order_code": "100833", "cpt_code": None},
            ],
        }
    ],
}


def lookup_with(handler, base_url=BASE, api_key="key", log=None):
    return BillingLookup(
        base_url,
        api_key,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        log=log or (lambda m: None),
    )


# -- enablement --------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url,api_key",
    [(None, "key"), (BASE, None), ("", ""), (None, None)],
)
def test_disabled_without_both_url_and_key(base_url, api_key):
    """A half-configured lookup must stay off rather than fail every request."""
    assert BillingLookup(base_url, api_key).enabled is False


def test_enabled_when_fully_configured():
    assert BillingLookup(BASE, "key").enabled is True


def test_disabled_lookup_makes_no_requests():
    def handler(request):
        raise AssertionError("should not issue a request")

    lookup = lookup_with(handler, api_key=None)
    assert lookup.report("dr-1") is None
    assert lookup.compendium() == {}


# -- failure handling --------------------------------------------------------


def test_unreachable_endpoint_disables_the_lookup_instead_of_raising():
    """The plugin is optional; the feed must keep flowing without it."""
    def handler(request):
        raise httpx.ConnectError("refused")

    messages = []
    lookup = lookup_with(handler, log=messages.append)
    assert lookup.report("dr-1") is None
    assert lookup.enabled is False
    assert any("unreachable" in m for m in messages)


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_api_key_stops_further_calls(status):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(status, json={})

    lookup = lookup_with(handler)
    lookup.report("dr-1")
    lookup.report("dr-2")
    assert calls["n"] == 1
    assert lookup.enabled is False


def test_404_returns_none_but_keeps_the_lookup_enabled():
    """An unknown report is normal; a missing plugin is not."""
    lookup = lookup_with(lambda request: httpx.Response(404, json={}))
    assert lookup.report("dr-1") is None
    assert lookup.enabled is True


def test_server_error_returns_none_and_keeps_trying():
    lookup = lookup_with(lambda request: httpx.Response(500, json={}))
    assert lookup.report("dr-1") is None
    assert lookup.enabled is True


# -- requests ----------------------------------------------------------------


def test_report_sends_the_api_key_and_hits_the_right_path():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=REPORT)

    assert lookup_with(handler).report("dr-1")["lab_report_id"] == "lab-1"
    assert seen["url"] == f"{BASE}/report/dr-1"
    assert seen["auth"] == "key"


def test_compendium_is_fetched_once_and_cached():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"count": 1, "tests": [
                {"order_code": "100002", "order_name": "Core Panel", "cpt_code": "80053"}
            ]},
        )

    lookup = lookup_with(handler)
    assert lookup.compendium()["100002"]["cpt_code"] == "80053"
    lookup.compendium()
    assert calls["n"] == 1


def test_compendium_skips_entries_without_an_order_code():
    lookup = lookup_with(
        lambda r: httpx.Response(200, json={"tests": [{"order_code": "", "cpt_code": "1"}]})
    )
    assert lookup.compendium() == {}


# -- payload shaping ---------------------------------------------------------


def test_procedures_from_lookup_emits_cpt_lines():
    procedures = procedures_from_lookup(REPORT)
    assert len(procedures) == 1
    assert procedures[0]["code"] == "80053"
    assert procedures[0]["is_cpt"] is True
    assert procedures[0]["source"] == "compendium"
    assert procedures[0]["order_code"] == "100002"


def test_procedures_from_lookup_skips_tests_with_no_cpt():
    """A test with no CPT configured must not become a blank procedure line."""
    assert [p["code"] for p in procedures_from_lookup(REPORT)] == ["80053"]


def test_procedures_from_lookup_dedupes_repeated_cpt():
    report = {"orders": [
        {"tests": [{"order_code": "a", "cpt_code": "80053"},
                   {"order_code": "b", "cpt_code": "80053"}]}
    ]}
    assert len(procedures_from_lookup(report)) == 1


def test_procedures_from_lookup_none_safe():
    assert procedures_from_lookup(None) == []
    assert procedures_from_lookup({"orders": []}) == []


def test_order_codes_from_lookup_collects_every_code():
    assert order_codes_from_lookup(REPORT) == {"100002", "100833"}
    assert order_codes_from_lookup(None) == set()


# -- CPT from posted order codes (the primary path) --------------------------

COMPENDIUM = {
    "100002": {"order_code": "100002", "order_name": "Core Panel", "cpt_code": "80053"},
    "100833": {"order_code": "100833", "order_name": "Carrier Screen", "cpt_code": "81443"},
    "100900": {"order_code": "100900", "order_name": "No CPT Configured", "cpt_code": None},
}


def test_one_posted_code_resolves_to_one_cpt():
    procedures = procedures_from_compendium(["100002"], COMPENDIUM)
    assert [p["code"] for p in procedures] == ["80053"]
    assert procedures[0]["order_code"] == "100002"
    assert procedures[0]["display"] == "Core Panel"


def test_many_posted_codes_resolve_to_many_cpts():
    procedures = procedures_from_compendium(["100002", "100833"], COMPENDIUM)
    assert [p["code"] for p in procedures] == ["80053", "81443"]


def test_two_codes_sharing_a_cpt_collapse_to_one_line():
    compendium = dict(COMPENDIUM, x={"order_code": "x", "cpt_code": "80053"})
    assert len(procedures_from_compendium(["100002", "x"], compendium)) == 1


def test_a_code_with_no_cpt_configured_yields_no_line():
    assert procedures_from_compendium(["100900"], COMPENDIUM) == []


def test_an_unknown_code_yields_no_line():
    assert procedures_from_compendium(["999999"], COMPENDIUM) == []


def test_no_codes_or_no_compendium_is_safe():
    assert procedures_from_compendium([], COMPENDIUM) == []
    assert procedures_from_compendium(["100002"], {}) == []


def test_partial_resolution_keeps_what_it_can():
    """One good code and one bad must still bill the good one."""
    procedures = procedures_from_compendium(["100002", "999999"], COMPENDIUM)
    assert [p["code"] for p in procedures] == ["80053"]


# -- naming the gap ----------------------------------------------------------


def test_unresolved_lists_unknown_and_cptless_codes():
    unresolved = unresolved_order_codes(["100002", "100900", "999999"], COMPENDIUM)
    assert unresolved == ["100900", "999999"]


def test_unresolved_is_empty_when_everything_resolves():
    assert unresolved_order_codes(["100002", "100833"], COMPENDIUM) == []
