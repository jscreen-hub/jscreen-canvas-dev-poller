"""Durable store of already-processed DiagnosticReport ids.

The Canvas DiagnosticReport search ignores `_lastUpdated` and `_sort`, so there
is no server-side cursor for "what is new since last time". Incremental polling
relies on this local dedup store instead: the poller sweeps reports each cycle
and skips any id already recorded here.
"""

from __future__ import annotations

import json
import os
from typing import Iterable


class ProcessedStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._ids: set[str] = set()

    def load(self) -> "ProcessedStore":
        """Load ids from disk. A missing file starts an empty store."""
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            self._ids = set()
        else:
            self._ids = {str(item) for item in data}
        return self

    def contains(self, report_id: str) -> bool:
        return report_id in self._ids

    def add(self, report_id: str) -> None:
        self._ids.add(report_id)

    def save(self) -> None:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as handle:
            json.dump(sorted(self._ids), handle, indent=2)

    def new_ids(self, report_ids: Iterable[str]) -> list[str]:
        """Return the subset of report_ids not already recorded (order preserved)."""
        result: list[str] = []
        for report_id in report_ids:
            if report_id not in self._ids:
                result.append(report_id)
        return result
