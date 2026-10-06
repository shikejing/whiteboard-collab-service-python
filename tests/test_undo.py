"""Undo/redo tests: own-sequence scope, concurrency isolation, interleaving."""

from __future__ import annotations

from whiteboard.engine import BoardEngine
from whiteboard.models import Op, OpId, OpType
from whiteboard.undo import UndoManager


def make():
    e = BoardEngine()
    return e, UndoManager(e), e.init_board("b")


def submit(um, e, st, op, record=True):
    r = e.commit(st, op)
    if record and not r.duplicate:
        um.record_client_op(st, op)
    return r


def props(e, st, sid):
    for x in e.render(st)["entities"]:
        if x["id"] == sid:
            return x["props"]
    return None


def alive(e, st, sid):
    return any(x["id"] == sid for x in e.render(st)["entities"])


def test_undo_then_redo_update():
    e, um, st = make()
    submit(um, e, st, Op(OpId("a", 1), OpType.ADD_SHAPE, "a", 0, shape_id="s", props={"c": "red"}))
    submit(um, e, st, Op(OpId("a", 2), OpType.UPDATE_SHAPE, "a", 1, shape_id="s", props={"c": "blue"}))
    assert props(e, st, "s") == {"c": "blue"}
    assert um.undo(st, "a").accepted
    assert props(e, st, "s") == {"c": "red"}
    assert um.redo(st, "a").accepted
    assert props(e, st, "s") == {"c": "blue"}


def test_undo_add_and_delete():
    e, um, st = make()
    submit(um, e, st, Op(OpId("a", 1), OpType.ADD_SHAPE, "a", 0, shape_id="s"))
    submit(um, e, st, Op(OpId("a", 2), OpType.DELETE_SHAPE, "a", 1, shape_id="s"))
    assert not alive(e, st, "s")
    um.undo(st, "a")  # undo delete -> resurrected
    assert alive(e, st, "s")
    um.undo(st, "a")  # undo add -> dead again
    assert not alive(e, st, "s")
    um.redo(st, "a")
    assert alive(e, st, "s")


def test_undo_zmove_restores_layer():
    e, um, st = make()
    submit(um, e, st, Op(OpId("a", 1), OpType.ADD_SHAPE, "a", 0, shape_id="a", z_index=0))
    submit(um, e, st, Op(OpId("a", 2), OpType.ADD_SHAPE, "a", 0, shape_id="b", z_index=0))
    submit(um, e, st, Op(OpId("a", 3), OpType.ZMOVE_SHAPE, "a", 2, shape_id="a", z_index=5))
    assert [x["id"] for x in e.render(st)["entities"]] == ["b", "a"]
    um.undo(st, "a")
    assert [x["id"] for x in e.render(st)["entities"]] == ["a", "b"]


def test_undo_does_not_touch_other_users_concurrent_edit():
    e, um, st = make()
    submit(um, e, st, Op(OpId("a", 1), OpType.ADD_SHAPE, "a", 0, shape_id="s", props={"c": "white"}))
    submit(um, e, st, Op(OpId("a", 2), OpType.UPDATE_SHAPE, "a", 1, shape_id="s", props={"c": "red"}))
    # bob writes the same field with a later op before alice undoes
    submit(um, e, st, Op(OpId("z", 1), OpType.UPDATE_SHAPE, "bob", 2, shape_id="s", props={"c": "green"}))
    um.undo(st, "a")
    # bob's green must survive; alice's undo is a guarded no-op on that field
    assert props(e, st, "s") == {"c": "green"}


def test_undo_scoped_to_own_sequence_only():
    e, um, st = make()
    submit(um, e, st, Op(OpId("a", 1), OpType.ADD_SHAPE, "a", 0, shape_id="a"))
    submit(um, e, st, Op(OpId("b", 1), OpType.ADD_SHAPE, "bob", 0, shape_id="b"))
    # bob has nothing to undo -> rejected, board untouched
    r = um.undo(st, "nobody")
    assert not r.accepted
    r = um.undo(st, "bob")
    assert r.accepted
    # bob undo removes only b, alice's a remains
    ids = {x["id"] for x in e.render(st)["entities"]}
    assert ids == {"a"}


def test_new_edit_clears_redo_stack_but_interleaving_is_stable():
    e, um, st = make()
    submit(um, e, st, Op(OpId("a", 1), OpType.ADD_SHAPE, "a", 0, shape_id="s", props={"c": "r"}))
    submit(um, e, st, Op(OpId("a", 2), OpType.UPDATE_SHAPE, "a", 1, shape_id="s", props={"c": "g"}))
    um.undo(st, "a")
    assert props(e, st, "s") == {"c": "r"}
    # a fresh edit after undo empties redo stack
    submit(um, e, st, Op(OpId("a", 3), OpType.UPDATE_SHAPE, "a", 2, shape_id="s", props={"c": "b"}))
    r = um.redo(st, "a")
    assert not r.accepted
    assert props(e, st, "s") == {"c": "b"}
    # undoing the fresh edit returns to red (the state before that edit)
    um.undo(st, "a")
    assert props(e, st, "s") == {"c": "r"}


def test_repeated_undo_same_unit_is_noop_after_pop():
    e, um, st = make()
    submit(um, e, st, Op(OpId("a", 1), OpType.ADD_SHAPE, "a", 0, shape_id="s"))
    assert um.undo(st, "a").accepted
    assert not um.undo(st, "a").accepted  # stack empty


def test_convergent_undo_regardless_of_commit_order():
    # the inverse batch itself is one committed op set; re-derive determinism
    import itertools
    e, um, st = make()
    add = Op(OpId("a", 1), OpType.ADD_SHAPE, "a", 0, shape_id="s", props={"c": "r"})
    updp = Op(OpId("a", 2), OpType.UPDATE_SHAPE, "a", 1, shape_id="s", props={"c": "g"})
    e.commit(st, add)
    e.commit(st, updp)
    um.record_client_op(st, add)
    um.record_client_op(st, updp)
    inverse = um._flip(st, st.users["a"].undo_stack[-1], undoing=True, user_id="a")
    # set of 3 ops must converge in any arrival order
    renders = set()
    for perm in itertools.permutations([add, updp, inverse]):
        e2 = BoardEngine()
        st2 = e2.init_board("b")
        for op in perm:
            e2.commit(st2, op)
        renders.add(repr(e2.render(st2)))
    assert len(renders) == 1
