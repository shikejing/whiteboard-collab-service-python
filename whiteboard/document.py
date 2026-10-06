"""Board document: deterministic application of a totally ordered op log.

The server serializes all operations for a board into one global order
(the log). This module defines how each operation transforms the board
state. Because every client applies the same log in the same order,
all replicas converge to identical state.
"""
from __future__ import annotations

from typing import Any, Optional

from .models import Operation, OpKind, Shape, Snapshot

_ADD_KINDS = {OpKind.ADD_SHAPE, OpKind.ADD_ANNOTATION}
_UPDATE_KINDS = {OpKind.UPDATE_SHAPE, OpKind.UPDATE_ANNOTATION}
_REMOVE_KINDS = {OpKind.REMOVE_SHAPE, OpKind.REMOVE_ANNOTATION}

_INVERSE_OF = {
    OpKind.ADD_SHAPE: OpKind.REMOVE_SHAPE,
    OpKind.ADD_ANNOTATION: OpKind.REMOVE_ANNOTATION,
    OpKind.REMOVE_SHAPE: OpKind.ADD_SHAPE,
    OpKind.REMOVE_ANNOTATION: OpKind.ADD_ANNOTATION,
}


def _element_kind(op_kind: str) -> str:
    return "annotation" if "annotation" in op_kind else "shape"


class Document:
    """Materialized state of one board, derived from the op log."""

    def __init__(self) -> None:
        self.shapes: dict[str, Shape] = {}

    # -- state transitions -------------------------------------------------

    def apply(self, op: Operation) -> None:
        """Apply one already-sequenced operation. Must be deterministic.

        UNDO/REDO carry their resolved effect in payload["effect"] (put
        there by the session when sequencing), so applying them here is
        just replaying an ordinary content operation.
        """
        if op.kind in (OpKind.UNDO, OpKind.REDO):
            effect = op.payload.get("effect")
            if effect:
                self._apply_effect(effect["kind"], effect.get("target_id"),
                                   effect.get("payload") or {})
            return
        self._apply_effect(op.kind.value, op.target_id, op.payload)

    def _apply_effect(self, kind: str, target_id: Optional[str],
                      payload: dict[str, Any]) -> None:
        if target_id is None:
            return
        if kind in (OpKind.ADD_SHAPE.value, OpKind.ADD_ANNOTATION.value):
            z = payload.get("z")
            if z is None:
                z = self._next_z()
            attrs = {k: v for k, v in payload.items() if k != "z"}
            self.shapes[target_id] = Shape(
                id=target_id, kind=_element_kind(kind), attrs=attrs, z=z)
        elif kind in (OpKind.UPDATE_SHAPE.value, OpKind.UPDATE_ANNOTATION.value):
            shape = self.shapes.get(target_id)
            if shape is not None and not shape.deleted:
                shape.attrs.update(payload)
        elif kind in (OpKind.REMOVE_SHAPE.value, OpKind.REMOVE_ANNOTATION.value):
            shape = self.shapes.get(target_id)
            if shape is not None:
                shape.deleted = True
        elif kind == OpKind.REORDER.value:
            shape = self.shapes.get(target_id)
            if shape is not None and not shape.deleted and "z" in payload:
                shape.z = payload["z"]

    def _next_z(self) -> int:
        live = [s.z for s in self.shapes.values() if not s.deleted]
        return (max(live) + 1) if live else 0

    # -- inverse computation (for undo) -------------------------------------

    def compute_inverse(self, op: Operation) -> Optional[dict[str, Any]]:
        """Inverse of `op` given the *current* state, or None if the op
        is a no-op here (nothing to undo). Deterministic because it runs
        at sequencing time on the authoritative state."""
        tid = op.target_id
        if tid is None:
            return None
        if op.kind in _ADD_KINDS:
            return {"kind": _INVERSE_OF[op.kind].value, "target_id": tid,
                    "payload": {}}
        if op.kind in _REMOVE_KINDS:
            shape = self.shapes.get(tid)
            if shape is None or shape.deleted:
                return None
            return {"kind": _INVERSE_OF[op.kind].value, "target_id": tid,
                    "payload": {**shape.attrs, "z": shape.z}}
        if op.kind in _UPDATE_KINDS:
            shape = self.shapes.get(tid)
            if shape is None or shape.deleted:
                return None
            previous = {k: shape.attrs.get(k) for k in op.payload}
            return {"kind": op.kind.value, "target_id": tid,
                    "payload": previous}
        if op.kind is OpKind.REORDER:
            shape = self.shapes.get(tid)
            if shape is None or shape.deleted:
                return None
            return {"kind": OpKind.REORDER.value, "target_id": tid,
                    "payload": {"z": shape.z}}
        return None

    # -- snapshots -----------------------------------------------------------

    def snapshot(self, version: int, watermarks: dict[str, int]) -> Snapshot:
        """Capture a self-contained snapshot of the current state."""
        shapes = {sid: Shape(id=s.id, kind=s.kind, attrs=dict(s.attrs),
                             z=s.z, deleted=s.deleted)
                  for sid, s in self.shapes.items()}
        return Snapshot(version=version, shapes=shapes,
                        client_watermarks=dict(watermarks))

    @classmethod
    def from_snapshot(cls, snap: Snapshot) -> "Document":
        doc = cls()
        doc.shapes = {sid: Shape(id=s.id, kind=s.kind, attrs=dict(s.attrs),
                                 z=s.z, deleted=s.deleted)
                      for sid, s in snap.shapes.items()}
        return doc
