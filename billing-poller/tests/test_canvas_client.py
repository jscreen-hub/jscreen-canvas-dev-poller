import httpx
import pytest

from billing_poller.canvas_client import CanvasClient
from tests.conftest import make_settings

FHIR = "https://fumage-jlab-dev.canvasmedical.com"
AUTH = "https://jlab-dev.canvasmedical.com/auth/token/"


def client_with(handler, **setting_overrides):
    settings = make_settings(**setting_overrides)
    transport = httpx.MockTransport(handler)
    return CanvasClient(settings, http=httpx.Client(transport=transport))


def token_response(expires_in=3600):
    return httpx.Response(200, json={"access_token": "tok", "expires_in": expires_in})


def bundle(resources, next_url=None):
    body = {
        "resourceType": "Bundle",
        "entry": [{"resource": r} for r in resources],
    }
    if next_url:
        body["link"] = [{"relation": "next", "url": next_url}]
    return httpx.Response(200, json=body)


# -- non-FHIR host -------------------------------------------------------


def test_auth_base_url_is_the_instance_host_not_the_fhir_host():
    """Exposed for callers building non-FHIR URLs, e.g. the C-CDA export."""
    client = client_with(lambda request: token_response())
    assert client.auth_base_url == "https://jlab-dev.canvasmedical.com"


# -- auth --------------------------------------------------------------------


def test_token_is_requested_once_and_reused():
    calls = {"token": 0}

    def handler(request):
        if str(request.url) == AUTH:
            calls["token"] += 1
            return token_response()
        return bundle([{"id": "a"}])

    client = client_with(handler)
    client.search("DiagnosticReport")
    client.search("Claim")
    assert calls["token"] == 1


def test_token_is_refreshed_after_expiry():
    calls = {"token": 0}
    clock = {"now": 0.0}

    def handler(request):
        if str(request.url) == AUTH:
            calls["token"] += 1
            return token_response(expires_in=100)
        return bundle([])

    settings = make_settings()
    client = CanvasClient(
        settings,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: clock["now"],
    )
    client.search("DiagnosticReport")
    clock["now"] = 500.0
    client.search("DiagnosticReport")
    assert calls["token"] == 2


def test_scope_is_sent_when_configured():
    seen = {}

    def handler(request):
        if str(request.url) == AUTH:
            seen["body"] = request.content.decode()
            return token_response()
        return bundle([])

    client_with(handler, scope="system/*.read").search("DiagnosticReport")
    assert "scope=system" in seen["body"]


def test_scope_is_omitted_when_unset():
    seen = {}

    def handler(request):
        if str(request.url) == AUTH:
            seen["body"] = request.content.decode()
            return token_response()
        return bundle([])

    client_with(handler, scope=None).search("DiagnosticReport")
    assert "scope" not in seen["body"]


def test_auth_failure_raises():
    def handler(request):
        if str(request.url) == AUTH:
            return httpx.Response(401, json={"error": "bad"})
        return bundle([])

    with pytest.raises(httpx.HTTPStatusError):
        client_with(handler).search("DiagnosticReport")


# -- search ------------------------------------------------------------------


def paged(handler_pages, total):
    """Mock transport serving pages by explicit _offset."""
    def handler(request):
        if str(request.url) == AUTH:
            return token_response()
        offset = request.url.params.get("_offset")
        ids = handler_pages.get(offset, [])
        return httpx.Response(200, json={
            "resourceType": "Bundle",
            "total": total,
            "entry": [{"resource": {"id": i}} for i in ids],
        })
    return handler


def test_search_pages_by_explicit_offset():
    """Canvas's ordering is unstable across requests, so offsets are requested
    directly rather than by following next links."""
    resources = client_with(
        paged({"0": ["a", "b"], "100": ["c"]}, total=3)
    ).search("DiagnosticReport")
    assert sorted(r["id"] for r in resources) == ["a", "b", "c"]


def test_search_deduplicates_records_repeated_across_pages():
    """A record shown on two pages as the result set shifts is returned once."""
    resources = client_with(
        paged({"0": ["a", "b"], "100": ["b", "c"]}, total=3)
    ).search("DiagnosticReport")
    assert sorted(r["id"] for r in resources) == ["a", "b", "c"]


def test_search_rewalks_when_it_comes_up_short_of_total():
    """The real failure mode: fewer distinct records than `total` promises must
    trigger another walk, not be silently accepted."""
    walks = {"n": 0}

    def handler(request):
        if str(request.url) == AUTH:
            return token_response()
        offset = request.url.params.get("_offset")
        if offset == "0":
            walks["n"] += 1
            ids = ["a"] if walks["n"] == 1 else ["a", "b"]
        else:
            ids = []
        return httpx.Response(200, json={
            "resourceType": "Bundle", "total": 2,
            "entry": [{"resource": {"id": i}} for i in ids],
        })

    resources = client_with(handler).search("DiagnosticReport")
    assert sorted(r["id"] for r in resources) == ["a", "b"]
    assert walks["n"] == 2


def test_search_gives_up_after_a_bounded_number_of_rewalks():
    """An unsatisfiable total must not loop forever."""
    resources = client_with(paged({"0": ["a"]}, total=99)).search("DiagnosticReport")
    assert [r["id"] for r in resources] == ["a"]


def test_search_terminates_when_the_server_reports_no_total():
    """Without `total` completeness cannot be checked; the walk must still end."""
    def handler(request):
        if str(request.url) == AUTH:
            return token_response()
        return httpx.Response(200, json={
            "resourceType": "Bundle",
            "entry": [{"resource": {"id": "a"}}],
        })

    assert [r["id"] for r in client_with(handler).search("DiagnosticReport")] == ["a"]


def test_search_sends_page_size_and_drops_empty_params():
    seen = {}

    def handler(request):
        if str(request.url) == AUTH:
            return token_response()
        seen["url"] = str(request.url)
        return bundle([])

    client_with(handler, page_size=100).search(
        "Coverage", patient="Patient/p1", status=""
    )
    assert "_count=100" in seen["url"]
    assert "patient=Patient" in seen["url"]
    assert "status=" not in seen["url"]


def test_search_skips_entries_without_a_resource():
    def handler(request):
        if str(request.url) == AUTH:
            return token_response()
        return httpx.Response(
            200, json={"entry": [{"resource": {"id": "a"}}, {"search": {}}]}
        )

    assert len(client_with(handler).search("Claim")) == 1


def test_search_raises_on_error_status():
    def handler(request):
        if str(request.url) == AUTH:
            return token_response()
        return httpx.Response(500, json={})

    with pytest.raises(httpx.HTTPStatusError):
        client_with(handler).search("DiagnosticReport")


# -- read --------------------------------------------------------------------


def test_read_reference_fetches_and_caches():
    calls = {"n": 0}

    def handler(request):
        if str(request.url) == AUTH:
            return token_response()
        calls["n"] += 1
        return httpx.Response(200, json={"id": "pat-1"})

    client = client_with(handler)
    assert client.read_reference("Patient/pat-1")["id"] == "pat-1"
    assert client.read_reference("Patient/pat-1")["id"] == "pat-1"
    assert calls["n"] == 1


def test_clear_cache_forces_a_refetch():
    calls = {"n": 0}

    def handler(request):
        if str(request.url) == AUTH:
            return token_response()
        calls["n"] += 1
        return httpx.Response(200, json={"id": "pat-1"})

    client = client_with(handler)
    client.read_reference("Patient/pat-1")
    client.clear_cache()
    client.read_reference("Patient/pat-1")
    assert calls["n"] == 2


@pytest.mark.parametrize("reference", [None, "", "malformed"])
def test_read_reference_is_none_safe(reference):
    def handler(request):
        raise AssertionError("should not issue a request")

    assert client_with(handler).read_reference(reference) is None


def test_close_closes_the_transport():
    def handler(request):
        return token_response()

    client = client_with(handler)
    client.close()
