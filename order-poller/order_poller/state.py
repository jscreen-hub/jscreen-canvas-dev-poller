"""Durable store of already-processed ServiceRequest ids.

The Canvas ServiceRequest search does not support a `_lastUpdated` filter, so
incremental polling relies on this local dedup store: the poller queries a recent
`authored` window each cycle and skips any id already recorded here.
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

    def contains(self, order_id: str) -> bool:
        return order_id in self._ids

    def add(self, order_id: str) -> None:
        self._ids.add(order_id)

    def save(self) -> None:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as handle:
            json.dump(sorted(self._ids), handle, indent=2)

    def new_ids(self, order_ids: Iterable[str]) -> list[str]:
        """Return the subset of order_ids not already recorded (order preserved)."""
        result: list[str] = []
        for order_id in order_ids:
            if order_id not in self._ids:
                result.append(order_id)
        return result
