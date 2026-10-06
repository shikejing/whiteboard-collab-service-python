"""Engine level tests: deterministic merge, out-of-order and duplicates."""

from __future__ import annotations

import itertools

import pytest

from whiteboard.engine import BoardEngine
from whiteboard.models import Op, OpId, OpType


def add(cid, seq, shape, author="u", base=0, props=None, z=None, annot=False):
    return Op(
        OpId(cid, seq),
        OpType.ADD_ANNOT if annot else OpType.ADD_SHAPE,
        author,
        base,
        shape_id=None if annot else shape,
        annot_id=shape if annot else None,
        props=props,
        z_index=z,
    )


def upd(cid, seq, shape, author="u", base=0, props=None, annot=False):
    return Op(
        OpId(cid, seq),
        OpType.UPDATE_ANNOT if annot else OpType.UPDATE_SHAPE,
        author,
        base,
        shape_id=None if annot else shape,
        annot_id=shape if annot else None,
        props=props,
    )


def dele(cid, seq, shape, author="u", base=0, annot=False):
    return Op(
        OpId(cid, seq),
        OpType.DELETE_ANNOT if annot else OpType.DELETE_SHAPE,
        author,
        base,
        shape_id=None if annot else shape,
        annot_id=shape if annot else None,
    )


def zmove(cid, seq, shape, z, author="u", base=0):
    return Op(OpId(cid, seq), OpType.ZMOVE_SHAPE, author, base, shape_id=shape, z_index=z)


def render_ids(view):
    return [e["id"] for e in view["entities"]]


def assert_convergent(engine, ops):
    renders = set()
    for perm in itertools.permutations(ops):
        state = engine.init_board("b")
        for op in perm:
            engine.commit(state, op)
        renders.add(repr(engine.render(state)))
    assert len(renders) == 1, renders
    state = engine.init_board("b")
    for op in ops:
        engine.commit(state, op)
    return engine.render(state)


def test_out_of_order_add_then_update_converges():
    e = BoardEngine()
    a = add("c1", 1, "s", props={"color": "red"}, z=0)
    u = upd("c2", 1, "s", props={"color": "blue"})
    view = assert_convergent(e, [a, u])
    assert view["entities"][0]["props"] == {"color": "blue"}


def test_concurrent_field_writes_lww_deterministic():
    e = BoardEngine()
    a = add("c1", 1, "s", props={"color": "red"})
    # total order is (client_id, seq, tag) lexicographic; "c2/7" > "c1/3"
    u1 = upd("c2", 7, "s", props={"color": "green"})
    u2 = upd("c1", 3, "s", props={"color": "blue"})
    view = assert_convergent(e, [a, u1, u2])
    assert view["entities"][0]["props"]["color"] == "green"


def test_concurrent_edits_to_different_fields_merge():
    e = BoardEngine()
    ops = [
        add("c1", 1, "s", props={"a": 1, "b": 2}),
        upd("c2", 1, "s", props={"a": 10}),
        upd("c3", 1, "s", props={"b": 20}),
    ]
    view = assert_convergent(e, ops)
    assert view["entities"][0]["props"] == {"a": 10, "b": 20}


def test_duplicate_client_op_ignored():
    e = BoardEngine()
    st = e.init_board("b")
    op = add("c1", 1, "s")
    r1 = e.commit(st, op)
    r2 = e.commit(st, op)
    assert not r1.duplicate
    assert r2.duplicate
    assert st.version == 1
    assert render_ids(e.render(st)) == ["s"]


def test_delete_tombstone_then_new_add_lww():
    e = BoardEngine()
    a = add("c1", 1, "s")
    d = dele("c2", 1, "s")  # later id -> wins, shape dead
    view = assert_convergent(e, [a, d])
    assert render_ids(view) == []


def test_delete_then_older_add_arrives_late_stays_dead():
    e = BoardEngine()
    st = e.init_board("b")
    d = dele("c9", 9, "s")  # arrives first (offline), high id
    a = add("c1", 1, "s")  # late low-id add cannot resurrect
    e.commit(st, d)
    e.commit(st, a)
    assert render_ids(e.render(st)) == []


def test_z_move_reorders_deterministically():
    e = BoardEngine()
    ops = [
        add("c1", 1, "a", z=0),
        add("c2", 1, "b", z=0),
        zmove("c3", 1, "a", 5),
    ]
    view = assert_convergent(e, ops)
    assert render_ids(view) == ["b", "a"]


def test_concurrent_zmoves_same_shape_lww():
    e = BoardEngine()
    ops = [
        add("c1", 1, "a", z=0),
        zmove("c1", 9, "a", 1),
        zmove("c3", 1, "a", 9),
    ]
    view = assert_convergent(e, ops)
    # (c3,1) > (c1,9) under lexicographic total order
    assert view["entities"][0]["z"] == 9


def test_annotations_are_independent_entities():
    e = BoardEngine()
    ops = [
        add("c1", 1, "note", annot=True, props={"text": "hi"}),
        add("c1", 2, "shape", props={"kind": "rect"}),
    ]
    view = assert_convergent(e, ops)
    kinds = {x["id"]: x["kind"] for x in view["entities"]}
    assert kinds == {"note": "annot", "shape": "shape"}


def test_batch_counts_single_version_and_dedups():
    e = BoardEngine()
    st = e.init_board("b")
    batch = Op(
        OpId("c1", 1),
        OpType.BATCH,
        "u",
        0,
        ops=[
            Op(OpId("c1", 1, 0), OpType.ADD_SHAPE, "u", 0, shape_id="x"),
            Op(OpId("c1", 1, 0), OpType.ADD_SHAPE, "u", 0, shape_id="y"),
        ],
    )
    r = e.commit(st, batch)
    assert r.version == 1
    r2 = e.commit(st, batch)
    assert r2.duplicate
    assert st.version == 1
    assert render_ids(e.render(st)) == ["x", "y"]
