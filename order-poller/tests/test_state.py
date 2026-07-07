"""Tests for the processed-id store."""

import json
from pathlib import Path

from order_poller.state import ProcessedStore


def test_load_missing_file_starts_empty(tmp_path: Path) -> None:
    store = ProcessedStore(str(tmp_path / "nope.json")).load()
    assert store.contains("anything") is False


def test_add_contains_and_save_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state" / "processed.json"
    store = ProcessedStore(str(path)).load()
    store.add("a")
    store.add("b")
    store.save()

    assert json.loads(path.read_text(encoding="utf-8")) == ["a", "b"]

    reloaded = ProcessedStore(str(path)).load()
    assert reloaded.contains("a") is True
    assert reloaded.contains("b") is True
    assert reloaded.contains("c") is False


def test_new_ids_filters_known_and_preserves_order(tmp_path: Path) -> None:
    store = ProcessedStore(str(tmp_path / "s.json")).load()
    store.add("b")
    assert store.new_ids(["a", "b", "c"]) == ["a", "c"]
