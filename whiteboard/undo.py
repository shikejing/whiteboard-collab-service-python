"""Undo / redo scoped to the initiator's own operation sequence.

Design
------
* Every committed client op (or client batch) becomes an :class:`UndoUnit`
  pushed on its author's undo stack; a new edit clears that user's redo
  stack, as in standard editors.
* Undo pops the latest own unit and emits **inverse ops** wrapped in one
  server batch. Inverses never touch other users' concurrent edits:

  - each inverse write carries ``anchor`` = the original client op id and
    ``guard`` = the id currently expected to own the register;
  - the engine applies it only while that guard still holds, so any field
    changed by someone else in between is left exactly as is.

* Inverse values are taken from the register's ``prev`` history chain, so an
  undo restores precisely what the original op overwrote (including the
  "field did not exist" case via ``removed_fields``).
* Redo re-applies the original effect, guarding against the undo inverse.
  Undo and redo may therefore interleave with new concurrent ops stably.
"""

from __future__ import annotations

from .engine import BoardEngine
from .models import (
    REDO_TAG,
    UNDO_TAG,
    BoardState,
    CommitResult,
    FieldOwner,
    Op,
    OpId,
    OpType,
    UndoUnit,
    UserState,
)


def _user(state: BoardState, user_id: str) -> UserState:
    us = state.users.get(user_id)
    if us is None:
        us = UserState()
        state.users[user_id] = us
    return us


def _find_owner(owner: FieldOwner | None, target: OpId) -> FieldOwner | None:
    """Walk the prev chain to locate the register owner with id == target."""
    cur = owner
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if cur.op_id == target:
            return cur
        cur = cur.prev
    return None


class UndoManager:
    def __init__(self, engine: BoardEngine | None = None) -> None:
        self.engine = engine or BoardEngine()

    # ------------------------------------------------------------- recording

    def record_client_op(self, state: BoardState, op: Op) -> None:
        """Record a committed client op/batch on its author's undo stack."""
        if not op.id.is_client_op():
            return
        us = _user(state, op.author)
        children = op.ops if op.type is OpType.BATCH else [op]
        us.undo_stack.append(UndoUnit(unit_id=op.id, ops=list(children)))
        us.redo_stack.clear()

    # --------------------------------------------------------------- helpers

    def _inverse_for(
        self, state: BoardState, orig: Op, guard: OpId, inverse_id: OpId
    ) -> list[Op]:
        """Build inverse ops reverting concrete op ``orig``.

        ``guard`` is the id that must currently own every touched register.
        ``alive`` selects liveness inverse value semantics: False for undo of
        an add, True for undo of a delete.
        """
        out: list[Op] = []
        shape_id = orig.shape_id or orig.annot_id
        assert shape_id is not None
        is_annot_type = orig.type in (
            OpType.ADD_ANNOT,
            OpType.UPDATE_ANNOT,
            OpType.DELETE_ANNOT,
        )
        if orig.type in (OpType.ADD_SHAPE, OpType.ADD_ANNOT):
            out.append(
                Op(
                    id=inverse_id,
                    type=OpType.DELETE_ANNOT if is_annot_type else OpType.DELETE_SHAPE,
                    author=orig.author,
                    base_version=state.version,
                    shape_id=shape_id,
                    annot_id=orig.annot_id,
                    anchor=orig.id,
                    guard=guard,
                    alive_value=False,
                )
            )
        elif orig.type in (OpType.DELETE_SHAPE, OpType.DELETE_ANNOT):
            out.append(
                Op(
                    id=inverse_id,
                    type=OpType.ADD_ANNOT if is_annot_type else OpType.ADD_SHAPE,
                    author=orig.author,
                    base_version=state.version,
                    shape_id=shape_id,
                    annot_id=orig.annot_id,
                    anchor=orig.id,
                    guard=guard,
                    alive_value=True,
                )
            )
        elif orig.type in (OpType.UPDATE_SHAPE, OpType.UPDATE_ANNOT):
            rec = state.shapes.get(shape_id)
            restore: dict = {}
            removals: list[str] = []
            for name in (orig.props or {}):
                current = rec.fields.get(name) if rec else None
                owner = _find_owner(current, orig.id)
                if owner is None:
                    continue  # original no longer owns this field: skip safely
                prev = owner.prev
                if prev is None:
                    removals.append(name)
                else:
                    restore[name] = prev.value
            if restore or removals:
                out.append(
                    Op(
                        id=inverse_id,
                        type=OpType.UPDATE_ANNOT if is_annot_type else OpType.UPDATE_SHAPE,
                        author=orig.author,
                        base_version=state.version,
                        shape_id=shape_id,
                        annot_id=orig.annot_id,
                        props=restore,
                        removed_fields=removals,
                        anchor=orig.id,
                        guard=guard,
                    )
                )
        elif orig.type is OpType.ZMOVE_SHAPE:
            rec = state.shapes.get(shape_id) if orig.shape_id else None
            value = 0
            if rec is not None and rec.z is not None:
                owner = _find_owner(rec.z, orig.id)
                if owner is not None and owner.prev is not None:
                    value = owner.prev.value
            out.append(
                Op(
                    id=inverse_id,
                    type=OpType.ZMOVE_SHAPE,
                    author=orig.author,
                    base_version=state.version,
                    shape_id=shape_id,
                    z_index=value,
                    anchor=orig.id,
                    guard=guard,
                )
            )
        return out

    def _flip(
        self, state: BoardState, unit: UndoUnit, undoing: bool, user_id: str
    ) -> Op:
        us = _user(state, user_id)
        us.undo_counter += 1
        head = OpId(user_id, us.undo_counter, UNDO_TAG if undoing else REDO_TAG)
        if undoing:
            children = self._undo_children(state, unit, head, us)
        else:
            children = self._redo_children(state, unit, head, us)
        batch = Op(
            id=head,
            type=OpType.BATCH,
            author=user_id,
            base_version=state.version,
            ops=children,
            target=unit.unit_id,
        )
        return batch

    def _undo_children(self, state: BoardState, unit: UndoUnit, head: OpId, us: UserState) -> list[Op]:
        children: list[Op] = []
        # newest concrete op first so guards chain naturally within the batch
        for idx, orig in enumerate(reversed(unit.ops)):
            child_id = OpId(head.client_id, us.undo_counter * 1000 + idx + 1, head.tag)
            children.extend(self._inverse_for(state, orig, orig.id, child_id))
        return children

    def _redo_children(self, state: BoardState, unit: UndoUnit, head: OpId, us: UserState) -> list[Op]:
        children: list[Op] = []
        for idx, orig in enumerate(reversed(unit.ops)):
            undo_inv = unit.inverses[idx] if idx < len(unit.inverses) else None
            guard = undo_inv.id if undo_inv is not None else orig.id
            child_id = OpId(head.client_id, us.undo_counter * 1000 + idx + 1, head.tag)
            shape_id = orig.shape_id or orig.annot_id
            is_annot_type = orig.type in (
                OpType.ADD_ANNOT,
                OpType.UPDATE_ANNOT,
                OpType.DELETE_ANNOT,
            )
            if orig.type in (OpType.ADD_SHAPE, OpType.ADD_ANNOT):
                children.append(
                    Op(
                        id=child_id,
                        type=OpType.ADD_ANNOT if is_annot_type else OpType.ADD_SHAPE,
                        author=orig.author,
                        base_version=state.version,
                        shape_id=shape_id,
                        annot_id=orig.annot_id,
                        props=dict(orig.props or {}),
                        z_index=orig.z_index,
                        anchor=orig.id,
                        guard=guard,
                        alive_value=True,
                    )
                )
            elif orig.type in (OpType.DELETE_SHAPE, OpType.DELETE_ANNOT):
                children.append(
                    Op(
                        id=child_id,
                        type=OpType.DELETE_ANNOT if is_annot_type else OpType.DELETE_SHAPE,
                        author=orig.author,
                        base_version=state.version,
                        shape_id=shape_id,
                        annot_id=orig.annot_id,
                        anchor=orig.id,
                        guard=guard,
                        alive_value=False,
                    )
                )
            elif orig.type in (OpType.UPDATE_SHAPE, OpType.UPDATE_ANNOT):
                children.append(
                    Op(
                        id=child_id,
                        type=OpType.UPDATE_ANNOT if is_annot_type else OpType.UPDATE_SHAPE,
                        author=orig.author,
                        base_version=state.version,
                        shape_id=shape_id,
                        annot_id=orig.annot_id,
                        props=dict(orig.props or {}),
                        anchor=orig.id,
                        guard=guard,
                    )
                )
            elif orig.type is OpType.ZMOVE_SHAPE:
                children.append(
                    Op(
                        id=child_id,
                        type=OpType.ZMOVE_SHAPE,
                        author=orig.author,
                        base_version=state.version,
                        shape_id=shape_id,
                        z_index=orig.z_index,
                        anchor=orig.id,
                        guard=guard,
                    )
                )
        return children

    # ------------------------------------------------------------------ undo

    def undo(self, state: BoardState, user_id: str) -> CommitResult:
        us = _user(state, user_id)
        if not us.undo_stack:
            return CommitResult(accepted=False, version=state.version, reason="nothing to undo")
        unit = us.undo_stack.pop()
        batch = self._flip(state, unit, undoing=True, user_id=user_id)
        unit.inverses = list(batch.ops)
        result = self.engine.commit(state, batch)
        unit.undone = True
        us.redo_stack.append(unit)
        return result

    def redo(self, state: BoardState, user_id: str) -> CommitResult:
        us = _user(state, user_id)
        if not us.redo_stack:
            return CommitResult(accepted=False, version=state.version, reason="nothing to redo")
        unit = us.redo_stack.pop()
        batch = self._flip(state, unit, undoing=False, user_id=user_id)
        unit.inverses = list(batch.ops)
        result = self.engine.commit(state, batch)
        unit.undone = False
        us.undo_stack.append(unit)
        return result
