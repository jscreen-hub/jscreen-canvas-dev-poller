"""Tests for the CLI entry point."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import billing_poller.__main__ as cli
from tests.conftest import make_settings


def test_date_ge_is_none_for_a_full_sweep() -> None:
    """lookback_days=0 means no date filter at all -- a back-dated result must
    not fall outside the window."""
    assert cli._date_ge(make_settings(lookback_days=0)) is None


def test_date_ge_returns_an_iso_date_when_a_lookback_is_set() -> None:
    value = cli._date_ge(make_settings(lookback_days=30))
    assert value is not None
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


def test_loop_keeps_running_after_a_failed_cycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transient Canvas error must not kill the feed."""
    settings = make_settings(state_file=str(tmp_path / "s.json"))
    fake_client = MagicMock()
    monkeypatch.setattr(cli, "CanvasClient", MagicMock(return_value=fake_client))
    monkeypatch.setattr(
        cli, "poll_once", MagicMock(side_effect=[RuntimeError("boom"), 1])
    )
    # Stop the loop on the second sleep.
    monkeypatch.setattr(cli.time, "sleep", MagicMock(side_effect=[None, KeyboardInterrupt]))

    assert cli.run(settings, once=False) == 0
    assert fake_client.clear_cache.call_count == 2
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
    monkeypatch.setattr("sys.argv", ["billing_poller", "--once"])

    assert cli.main() == 0
    assert captured["once"] is True
    assert captured["settings"] is settings


def test_main_defaults_to_loop_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "load_settings", MagicMock(return_value=make_settings()))
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "run", lambda s, once: captured.setdefault("once", once) or 0)
    monkeypatch.setattr("sys.argv", ["billing_poller"])

    assert cli.main() == 0
    assert captured["once"] is False
