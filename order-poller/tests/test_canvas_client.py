"""Tests for CanvasClient using httpx.MockTransport."""

from typing import Callable

import httpx

from order_poller.canvas_client import CanvasClient
from tests.conftest import make_settings

TOKEN_PATH = "/auth/token/"


def _token_response() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})


def _client_with(
    handler: Callable[[httpx.Request], httpx.Response], now: Callable[[], float]
) -> CanvasClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CanvasClient(make_settings(), http=http, now=now)


class TestTokenManagement:
    def test_token_fetched_once_and_cached(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == TOKEN_PATH:
                return _token_response()
            return httpx.Response(200, json={"resourceType": "Bundle", "entry": []})

        client = _client_with(handler, now=lambda: 1000.0)
        client.search_service_requests("cat")
        client.search_service_requests("cat")

        assert calls.count(TOKEN_PATH) == 1

    def test_token_refreshed_after_expiry(self) -> None:
        calls: list[str] = []
        clock = {"t": 1000.0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == TOKEN_PATH:
                return _token_response()
            return httpx.Response(200, json={"resourceType": "Bundle", "entry": []})

        client = _client_with(handler, now=lambda: clock["t"])
        client.search_service_requests("cat")
        clock["t"] = 1000.0 + 3600  # past expiry (issued_at + 3600 - 60 skew)
        client.search_service_requests("cat")

        assert calls.count(TOKEN_PATH) == 2

    def test_token_post_sends_scope(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == TOKEN_PATH:
                body = request.content.decode()
                captured["body"] = body
                return _token_response()
            return httpx.Response(200, json={"resourceType": "Bundle", "entry": []})

        client = _client_with(handler, now=lambda: 0.0)
        client.search_service_requests("cat")

        assert "grant_type=client_credentials" in captured["body"]
        assert "scope=system" in captured["body"]


class TestSearch:
    def test_search_sends_category_and_authored(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == TOKEN_PATH:
                return _token_response()
            seen["category"] = request.url.params.get("category", "")
            seen["authored"] = request.url.params.get("authored", "")
            return httpx.Response(200, json={"resourceType": "Bundle", "entry": []})

        client = _client_with(handler, now=lambda: 0.0)
        client.search_service_requests("http://snomed.info/sct|108252007", authored_ge="2026-07-01")

        assert seen["category"] == "http://snomed.info/sct|108252007"
        assert seen["authored"] == "ge2026-07-01"

    def test_search_follows_pagination(self) -> None:
        fhir = "https://fumage-jlab-dev.canvasmedical.com"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == TOKEN_PATH:
                return _token_response()
            if request.url.params.get("page") == "2":
                return httpx.Response(
                    200,
                    json={"resourceType": "Bundle", "entry": [{"resource": {"id": "sr2"}}]},
                )
            return httpx.Response(
                200,
                json={
                    "resourceType": "Bundle",
                    "entry": [{"resource": {"id": "sr1"}}],
                    "link": [{"relation": "next", "url": f"{fhir}/ServiceRequest?page=2"}],
                },
            )

        client = _client_with(handler, now=lambda: 0.0)
        results = client.search_service_requests("cat")

        assert [r["id"] for r in results] == ["sr1", "sr2"]

    def test_search_follows_relative_pagination(self) -> None:
        """Canvas returns relative next links; they must resolve against the base."""
        seen_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == TOKEN_PATH:
                return _token_response()
            seen_paths.append(request.url.path)
            if request.url.params.get("_offset") == "10":
                return httpx.Response(
                    200,
                    json={"resourceType": "Bundle", "entry": [{"resource": {"id": "sr2"}}]},
                )
            return httpx.Response(
                200,
                json={
                    "resourceType": "Bundle",
                    "entry": [{"resource": {"id": "sr1"}}],
                    # relative URL, exactly as Canvas emits it
                    "link": [{"relation": "next", "url": "/ServiceRequest?_offset=10"}],
                },
            )

        client = _client_with(handler, now=lambda: 0.0)
        results = client.search_service_requests("cat")

        assert [r["id"] for r in results] == ["sr1", "sr2"]
        assert seen_paths == ["/ServiceRequest", "/ServiceRequest"]

    def test_search_skips_entries_without_resource(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == TOKEN_PATH:
                return _token_response()
            return httpx.Response(
                200,
                json={"resourceType": "Bundle", "entry": [{"resource": {"id": "sr1"}}, {}]},
            )

        client = _client_with(handler, now=lambda: 0.0)
        assert [r["id"] for r in client.search_service_requests("cat")] == ["sr1"]


class TestReadReference:
    def test_read_reference_returns_resource(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == TOKEN_PATH:
                return _token_response()
            assert request.url.path == "/Patient/patient-1"
            return httpx.Response(200, json={"resourceType": "Patient", "id": "patient-1"})

        client = _client_with(handler, now=lambda: 0.0)
        result = client.read_reference("Patient/patient-1")
        assert result == {"resourceType": "Patient", "id": "patient-1"}

    def test_read_reference_none_for_empty_or_invalid(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _token_response()

        client = _client_with(handler, now=lambda: 0.0)
        assert client.read_reference(None) is None
        assert client.read_reference("no-slash") is None


def test_close_closes_underlying_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _token_response()

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = CanvasClient(make_settings(), http=http, now=lambda: 0.0)
    client.close()
    assert http.is_closed is True
