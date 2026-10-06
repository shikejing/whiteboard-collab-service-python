"""Per-user undo/redo bookkeeping.

Undo is scoped to the *initiating user*: it inverts that user's own
most recent undoable operation, without touching concurrent edits made
by others. Undo/redo are themselves appended to the shared log as
regular operations (carrying their resolved effect), so every replica
applies them in the same order and all replicas stay consistent.

Semantics when operations interleave: undoing restores the value that
was captured when the original operation was sequenced. If another
user edited the same element afterwards, the undo still restores the
captured value (last-writer-wins) — deterministic and explainable.
"""
from __future__ import annotations

from .models import Operation, OpKind

_CONTENT_KINDS = {
    OpKind.ADD_SHAPE, OpKind.UPDATE_SHAPE, OpKind.REMOVE_SHAPE,
    OpKind.ADD_ANNOTATION, OpKind.UPDATE_ANNOTATION,
    OpKind.REMOVE_ANNOTATION, OpKind.REORDER,
}


class UndoStack:
    """Tracks, per user, which of their ops are undoable/redoable."""

    def __init__(self) -> None:
        # user_id -> stack of op-ref keys ("client:seq"), most recent last
        self._undoable: dict[str, list[str]] = {}
        self._redoable: dict[str, list[str]] = {}

    def record(self, op: Operation) -> None:
        """Record a newly applied content op. A fresh edit invalidates
        the user's redo stack (standard editor semantics)."""
        if op.kind not in _CONTENT_KINDS:
            return
        key = op.ref.key()
        self._undoable.setdefault(op.user_id, []).append(key)
        self._redoable[op.user_id] = []

    # -- undo side -----------------------------------------------------------

    def pop_undo(self, user_id: str) -> str | None:
        """Pop the user's most recent undoable op-ref, or None."""
        stack = self._undoable.get(user_id)
        if not stack:
            return None
        return stack.pop()

    def push_undo(self, user_id: str, key: str) -> None:
        """Make an op undoable again (used after a successful redo)."""
        self._undoable.setdefault(user_id, []).append(key)

    # -- redo side -----------------------------------------------------------

    def pop_redo(self, user_id: str) -> str | None:
        """Pop the user's most recent redoable op-ref, or None."""
        stack = self._redoable.get(user_id)
        if not stack:
            return None
        return stack.pop()

    def push_redo(self, user_id: str, key: str) -> None:
        """Mark an op as redoable (used after a successful undo)."""
        self._redoable.setdefault(user_id, []).append(key)
