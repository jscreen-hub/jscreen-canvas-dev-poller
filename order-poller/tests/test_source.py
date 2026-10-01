"""Tests for the signed lab-order feed client."""

import httpx
import pytest

from order_poller.source import LabOrderSource

BASE = "https://jlab-dev.canvasmedical.com/plugin-io/api/lab_billing_lookup/billing"


def source_with(handler, base_url=BASE, api_key="key"):
    return LabOrderSource(
        base_url, api_key, http=httpx.Client(transport=httpx.MockTransport(handler))
    )


@pytest.mark.parametrize("base_url,api_key", [("", "key"), (BASE, ""), ("", "")])
def test_not_configured_without_both_url_and_key(base_url, api_key):
    assert LabOrderSource(base_url, api_key).configured is False


def test_configured_when_both_present():
    assert LabOrderSource(BASE, "key").configured is True


def test_unconfigured_source_raises_rather_than_returning_empty():
    """An empty list would be indistinguishable from 'no new orders'."""
    with pytest.raises(RuntimeError, match="not configured"):
        LabOrderSource("", "").signed_orders()


def test_signed_orders_sends_the_key_and_hits_the_right_path():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"count": 1, "orders": [{"order": {"id": "lo-1"}}]})

    orders = source_with(handler).signed_orders()
    assert [o["order"]["id"] for o in orders] == ["lo-1"]
    assert seen["url"] == f"{BASE}/lab-orders"
    assert seen["auth"] == "key"


def test_empty_feed_is_a_valid_empty_list():
    orders = source_with(
        lambda r: httpx.Response(200, json={"count": 0, "orders": []})
    ).signed_orders()
    assert orders == []


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_http_errors_raise(status):
    """A failing plugin must stop the cycle, not silently deliver nothing."""
    with pytest.raises(httpx.HTTPStatusError):
        source_with(lambda r: httpx.Response(status, json={})).signed_orders()


def test_a_malformed_body_raises():
    with pytest.raises(RuntimeError, match="unexpected response"):
        source_with(lambda r: httpx.Response(200, json={"count": 0})).signed_orders()


def test_transport_failure_propagates():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        source_with(handler).signed_orders()
