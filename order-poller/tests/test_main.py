"""Tests for the CLI entry point."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import order_poller.__main__ as cli
from tests.conftest import make_settings


def test_authored_ge_uses_lookback() -> None:
    settings = make_settings(lookback_days=3)
    # Just assert it's an ISO date string (YYYY-MM-DD).
    value = cli._authored_ge(settings)
    assert len(value) == 10 and value[4] == "-"


def test_run_once_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(state_file=str(tmp_path / "s.json"))
    fake_client = MagicMock()
    monkeypatch.setattr(cli, "CanvasClient", MagicMock(return_value=fake_client))
    poll = MagicMock(return_value=1)
    monkeypatch.setattr(cli, "poll_once", poll)

    rc = cli.run(settings, once=True)

    assert rc == 0
    assert poll.call_count == 1
    assert fake_client.close.call_count == 1


def test_run_once_failure_returns_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(state_file=str(tmp_path / "s.json"))
    fake_client = MagicMock()
    monkeypatch.setattr(cli, "CanvasClient", MagicMock(return_value=fake_client))
    monkeypatch.setattr(cli, "poll_once", MagicMock(side_effect=RuntimeError("boom")))

    rc = cli.run(settings, once=True)

    assert rc == 1
    assert fake_client.close.call_count == 1


def test_main_wires_once_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings()
    monkeypatch.setattr(cli, "load_settings", MagicMock(return_value=settings))
    captured: dict[str, object] = {}

    def fake_run(s: object, once: bool) -> int:
        captured["settings"] = s
        captured["once"] = once
        return 0

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["order_poller", "--once"])

    assert cli.main() == 0
    assert captured["once"] is True
    assert captured["settings"] is settings
