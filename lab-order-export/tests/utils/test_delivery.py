"""Tests for the delivery util (API POST + NDJSON file append)."""

import json
from unittest.mock import MagicMock, call, mock_open, patch

from lab_order_export.utils.delivery import append_to_file, post_to_api

PAYLOAD = {"order": {"id": "order-uuid"}}


class TestPostToApi:
    def test_successful_post_with_token(self) -> None:
        with patch("lab_order_export.utils.delivery.Http") as mock_http:
            response = MagicMock()
            response.ok = True
            mock_http.return_value.post.return_value = response

            result = post_to_api("https://example.com/orders", "secret-token", PAYLOAD)

            assert result is True
            assert mock_http.mock_calls == [
                call(),
                call().post(
                    "https://example.com/orders",
                    json=PAYLOAD,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer secret-token",
                    },
                ),
            ]

    def test_successful_post_without_token(self) -> None:
        with patch("lab_order_export.utils.delivery.Http") as mock_http:
            response = MagicMock()
            response.ok = True
            mock_http.return_value.post.return_value = response

            result = post_to_api("https://example.com/orders", None, PAYLOAD)

            assert result is True
            assert mock_http.mock_calls == [
                call(),
                call().post(
                    "https://example.com/orders",
                    json=PAYLOAD,
                    headers={"Content-Type": "application/json"},
                ),
            ]

    def test_non_ok_response_returns_false(self) -> None:
        with patch("lab_order_export.utils.delivery.Http") as mock_http:
            response = MagicMock()
            response.ok = False
            response.status_code = 500
            mock_http.return_value.post.return_value = response

            result = post_to_api("https://example.com/orders", "t", PAYLOAD)

            assert result is False

    def test_exception_returns_false(self) -> None:
        with patch("lab_order_export.utils.delivery.Http") as mock_http:
            mock_http.return_value.post.side_effect = RuntimeError("boom")

            result = post_to_api("https://example.com/orders", "t", PAYLOAD)

            assert result is False

    def test_missing_url_skips_and_returns_false(self) -> None:
        with patch("lab_order_export.utils.delivery.Http") as mock_http:
            result = post_to_api(None, "t", PAYLOAD)

            assert result is False
            assert mock_http.mock_calls == []


class TestAppendToFile:
    def test_successful_append_writes_ndjson_line(self) -> None:
        m = mock_open()
        with patch("builtins.open", m):
            result = append_to_file("/tmp/orders.ndjson", PAYLOAD)

        assert result is True
        assert m.mock_calls == [
            call("/tmp/orders.ndjson", "a", encoding="utf-8"),
            call().__enter__(),
            call().write(json.dumps(PAYLOAD) + "\n"),
            call().__exit__(None, None, None),
            call().close(),
        ]

    def test_missing_path_skips_and_returns_false(self) -> None:
        m = mock_open()
        with patch("builtins.open", m):
            result = append_to_file(None, PAYLOAD)

        assert result is False
        assert m.mock_calls == []

    def test_exception_returns_false(self) -> None:
        with patch("builtins.open", side_effect=OSError("read-only fs")):
            result = append_to_file("/tmp/orders.ndjson", PAYLOAD)

        assert result is False
