"""In-memory queue of form submissions awaiting operator confirmation.

Unbound questionnaire submissions (the public Tencent form, or a booth QR
scan with no active robot interaction) must never command the robot on their
own (AGENTS.md invariant 14). They land here instead: the operator console
polls this inbox, pops a confirmation dialog, and the operator's explicit
confirmation is what dispatches voice and escort.

Process-local by design. The booth server is the single process that receives
every submission; after a restart the pending-escort queue endpoints still
hold anything unresolved, so nothing is lost with the popups.
"""

from __future__ import annotations

from threading import Lock
from typing import Any


class OperatorInbox:
    def __init__(self, max_items: int = 20) -> None:
        # dict preserves insertion order: oldest submission first.
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = Lock()
        self._max_items = max_items

    def add(self, item: dict[str, Any]) -> None:
        response_id = str(item.get("response_id") or "")
        if not response_id:
            return
        with self._lock:
            self._items.pop(response_id, None)
            self._items[response_id] = dict(item)
            while len(self._items) > self._max_items:
                del self._items[next(iter(self._items))]

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._items.values()]

    def remove(self, response_id: str) -> None:
        with self._lock:
            self._items.pop(str(response_id), None)
