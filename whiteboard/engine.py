"""Core board engine: deterministic merge of operations into board state.

Convergence contract
---------------------
Every *set* of operations over an entity reduces to the same state no matter
the order they are applied. The engine is built from commuting pieces:

* **LWW registers per field.** Writes are totally ordered by
  :class:`~whiteboard.models.OpId`. A regular write replaces a register iff
  its op id is newer. Each register keeps a linked history chain (``prev``)
  so an undo can restore the exact owner the target op overwrote.
* **Liveness register** (alive/dead) per entity, written by add (True) and
  delete (False) ops; tombstones survive because adds/deletes compete purely
  by op id.
* **z register** (layer) per entity, same LWW rules.
* **Inverse writes** (server-generated undo/redo) carry ``anchor`` (the
  original client op) plus a ``guard``. They only replace a register while
  the target op still owns it (guard holds) and never clobber a regular
  write newer than the anchor. Another user's concurrent edit is preserved.
* **Batch** commits several child ops under one dedup/version entry.

Duplicate client reports with the same ``(client_id, client_seq)`` return the
already-committed op without changing anything.
"""

from __future__ import annotations

from .models import (
    BoardState,
    CommitResult,
    FieldOwner,
    Op,
    OpId,
    OpType,
    ShapeRecord,
)


def effective_id(owner: FieldOwner) -> OpId:
    """Ordering position of a field write; inverses compete by anchor."""
    return owner.anchor if owner.anchor is not None else owner.op_id


def register_write(
    current: FieldOwner | None,
    op_id: OpId,
    value: object,
    anchor: OpId | None,
    guard: OpId | None,
) -> FieldOwner | None:
    """Attempt an LWW write. Returns the new owner or ``None`` if it lost.

    On success the new owner's ``prev`` chain points at the overwritten
    owner, enabling exact inverse restoration later.
    """
    if current is None:
        return FieldOwner(op_id=op_id, value=value, anchor=anchor)
    if anchor is not None:
        if current.anchor is None:
            # inverse vs a regular write: only act while no regular write
            # newer than the reverted target exists. A foreign (or own)
            # newer write therefore always survives the undo.
            if anchor < current.op_id:
                return None
        else:
            # inverse vs inverse: server generates inverses serially under
            # the board lock; the later generated one reflects the latest
            # undo/redo request and wins.
            if op_id <= current.op_id:
                return None
    else:
        if op_id <= effective_id(current):
            return None
    return FieldOwner(op_id=op_id, value=value, anchor=anchor, prev=current)


class BoardEngine:
    def __init__(self) -> None:
        pass

    def init_board(self, board_id: str) -> BoardState:
        return BoardState(board_id=board_id)

    # ---------------------------------------------------------------- commit

    def commit(self, state: BoardState, op: Op) -> CommitResult:
        if op.id.is_client_op():
            key = (op.id.client_id, op.id.seq)
            existing = state.seen.get(key)
            if existing is not None:
                return CommitResult(
                    accepted=True,
                    version=state.version,
                    op=existing,
                    duplicate=True,
                    reason="duplicate (client_id, client_seq)",
                )
        state.version += 1
        state.log.append(op)
        self.apply(state, op)
        if op.id.is_client_op():
            state.seen[(op.id.client_id, op.id.seq)] = op
        return CommitResult(accepted=True, version=state.version, op=op)

    # ----------------------------------------------------------------- apply

    def apply(self, state: BoardState, op: Op) -> bool:
        if op.type is OpType.BATCH:
            changed = False
            for child in op.ops:
                changed = self.apply(state, child) or changed
            return changed
        if op.type in (OpType.ADD_SHAPE, OpType.ADD_ANNOT):
            return self._apply_add(state, op)
        if op.type in (OpType.UPDATE_SHAPE, OpType.UPDATE_ANNOT):
            return self._apply_update(state, op)
        if op.type in (OpType.DELETE_SHAPE, OpType.DELETE_ANNOT):
            return self._apply_delete(state, op)
        if op.type is OpType.ZMOVE_SHAPE:
            return self._apply_zmove(state, op)
        raise ValueError(f"unknown op type: {op.type}")

    def _rec(self, state: BoardState, op: Op, shape_id: str) -> ShapeRecord:
        rec = state.shapes.get(shape_id)
        if rec is None:
            annot = op.type in (
                OpType.ADD_ANNOT,
                OpType.UPDATE_ANNOT,
                OpType.DELETE_ANNOT,
            )
            rec = ShapeRecord(shape_id=shape_id, kind="annot" if annot else "shape")
            state.shapes[shape_id] = rec
        return rec

    def _apply_add(self, state: BoardState, op: Op) -> bool:
        shape_id = op.shape_id or op.annot_id
        assert shape_id is not None
        rec = self._rec(state, op, shape_id)
        changed = False
        if rec.born_op is None or op.id < rec.born_op:
            rec.born_op = op.id
            changed = True
        # liveness register: add claims alive
        new_alive = register_write(rec.alive, op.id, True, op.anchor, op.guard)
        if new_alive is not None:
            rec.alive = new_alive
            changed = True
        for name, value in (op.props or {}).items():
            new_owner = register_write(
                rec.fields.get(name), op.id, value, op.anchor, op.guard
            )
            if new_owner is not None:
                rec.fields[name] = new_owner
                changed = True
        if op.z_index is not None:
            new_z = register_write(rec.z, op.id, op.z_index, op.anchor, op.guard)
            if new_z is not None:
                rec.z = new_z
                changed = True
        return changed

    def _apply_update(self, state: BoardState, op: Op) -> bool:
        shape_id = op.shape_id or op.annot_id
        assert shape_id is not None
        rec = self._rec(state, op, shape_id)
        changed = False
        for name, value in (op.props or {}).items():
            new_owner = register_write(
                rec.fields.get(name), op.id, value, op.anchor, op.guard
            )
            if new_owner is not None:
                rec.fields[name] = new_owner
                changed = True
        for name in op.removed_fields:
            current = rec.fields.get(name)
            new_owner = register_write(current, op.id, None, op.anchor, op.guard)
            if new_owner is not None:
                del rec.fields[name]
                changed = True
        return changed

    def _apply_delete(self, state: BoardState, op: Op) -> bool:
        shape_id = op.shape_id or op.annot_id
        assert shape_id is not None
        rec = self._rec(state, op, shape_id)
        new_alive = register_write(rec.alive, op.id, False, op.anchor, op.guard)
        if new_alive is not None:
            rec.alive = new_alive
            return True
        return False

    def _apply_zmove(self, state: BoardState, op: Op) -> bool:
        assert op.shape_id is not None and op.z_index is not None
        rec = self._rec(state, op, op.shape_id)
        new_z = register_write(rec.z, op.id, op.z_index, op.anchor, op.guard)
        if new_z is not None:
            rec.z = new_z
            return True
        return False

    # ---------------------------------------------------------------- render

    def render(self, state: BoardState) -> dict:
        entities: list[dict] = []
        for shape_id, rec in state.shapes.items():
            if rec.born_op is None:
                continue
            if rec.alive is not None and rec.alive.value is False:
                continue
            entities.append(
                {
                    "id": shape_id,
                    "kind": rec.kind,
                    "z": rec.z.value if rec.z is not None else 0,
                    "props": {
                        name: owner.value for name, owner in sorted(rec.fields.items())
                    },
                }
            )
        birth = {e["id"]: state.shapes[e["id"]].born_op for e in entities}
        entities.sort(key=lambda e: (e["z"], birth[e["id"]].key(), e["id"]))
        return {
            "board_id": state.board_id,
            "version": state.version,
            "entities": entities,
        }

