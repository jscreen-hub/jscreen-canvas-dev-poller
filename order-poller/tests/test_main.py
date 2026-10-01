"""Tests for the CLI entry point."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import order_poller.__main__ as cli
from tests.conftest import make_settings


def _patch_source(monkeypatch, configured=True):
    source = MagicMock()
    source.configured = configured
    monkeypatch.setattr(cli, "LabOrderSource", MagicMock(return_value=source))
    return source


def test_run_once_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(state_file=str(tmp_path / "s.json"))
    source = _patch_source(monkeypatch)
    poll = MagicMock(return_value=1)
    monkeypatch.setattr(cli, "poll_once", poll)

    assert cli.run(settings, once=True) == 0
    assert poll.call_count == 1
    assert source.close.call_count == 1


def test_run_once_failure_returns_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(state_file=str(tmp_path / "s.json"))
    source = _patch_source(monkeypatch)
    monkeypatch.setattr(cli, "poll_once", MagicMock(side_effect=RuntimeError("boom")))

    assert cli.run(settings, once=True) == 1
    assert source.close.call_count == 1


def test_run_refuses_when_the_source_is_not_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without the plugin there is no order feed at all, so fail loudly rather
    than poll forever writing nothing."""
    settings = make_settings(state_file=str(tmp_path / "s.json"))
    _patch_source(monkeypatch, configured=False)
    poll = MagicMock()
    monkeypatch.setattr(cli, "poll_once", poll)

    assert cli.run(settings, once=True) == 1
    assert poll.call_count == 0


def test_seed_runs_once_and_skips_polling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = make_settings(state_file=str(tmp_path / "s.json"))
    _patch_source(monkeypatch)
    seed = MagicMock(return_value=5)
    poll = MagicMock()
    monkeypatch.setattr(cli, "seed", seed)
    monkeypatch.setattr(cli, "poll_once", poll)

    assert cli.run(settings, once=False, do_seed=True) == 0
    assert seed.call_count == 1
    assert poll.call_count == 0


def test_loop_keeps_running_after_a_failed_cycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = make_settings(state_file=str(tmp_path / "s.json"))
    source = _patch_source(monkeypatch)
    monkeypatch.setattr(cli, "poll_once", MagicMock(side_effect=[RuntimeError("boom"), 1]))
    monkeypatch.setattr(cli.time, "sleep", MagicMock(side_effect=[None, KeyboardInterrupt]))

    assert cli.run(settings, once=False) == 0
    assert source.close.call_count == 1


def test_main_wires_the_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings()
    monkeypatch.setattr(cli, "load_settings", MagicMock(return_value=settings))
    captured: dict[str, object] = {}

    def fake_run(s: object, once: bool, do_seed: bool = False) -> int:
        captured.update(settings=s, once=once, do_seed=do_seed)
        return 0

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["order_poller", "--once"])

    assert cli.main() == 0
    assert captured["once"] is True
    assert captured["do_seed"] is False
    assert captured["settings"] is settings


def test_main_wires_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "load_settings", MagicMock(return_value=make_settings()))
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli, "run", lambda s, once, do_seed=False: captured.update(do_seed=do_seed) or 0
    )
    monkeypatch.setattr("sys.argv", ["order_poller", "--seed"])

    assert cli.main() == 0
    assert captured["do_seed"] is True
