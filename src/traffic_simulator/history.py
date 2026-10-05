"""Undo/redo by snapshots: after each change, the state before it becomes an undo step.

The editor calls commit() whenever an action finishes. Edits sharing a merge key
within MERGE_SECONDS (typing into one field) fold into a single step.
"""

from __future__ import annotations

from time import monotonic
from typing import Any

MERGE_SECONDS = 1.5


class History:
    def __init__(self, state: Any, limit: int = 100) -> None:
        self.current = state  # the committed state, never mutated
        self.limit = limit
        self.undo_stack: list[Any] = []
        self.redo_stack: list[Any] = []
        self._last_key: object = None
        self._last_time = 0.0

    def commit(self, state: Any, merge_key: object = None, now: float | None = None) -> bool:
        """Record `state` as the new current state. False if nothing changed."""
        if state == self.current:
            return False
        now = monotonic() if now is None else now
        continuing = merge_key is not None and merge_key == self._last_key \
            and now - self._last_time < MERGE_SECONDS
        if not continuing:
            self.undo_stack.append(self.current)
            del self.undo_stack[:-self.limit]
        self.redo_stack.clear()
        self.current, self._last_key, self._last_time = state, merge_key, now
        return True

    def undo(self) -> Any | None:
        """The state to restore, or None when there's nothing to undo."""
        if not self.undo_stack:
            return None
        self.redo_stack.append(self.current)
        self.current, self._last_key = self.undo_stack.pop(), None
        return self.current

    def redo(self) -> Any | None:
        if not self.redo_stack:
            return None
        self.undo_stack.append(self.current)
        self.current, self._last_key = self.redo_stack.pop(), None
        return self.current
