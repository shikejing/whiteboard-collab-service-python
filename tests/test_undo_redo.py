"""Undo/redo: scoped to the initiator's own ops; concurrent edits by
others are untouched; interleaving of undo and new ops is stable."""
from whiteboard.models import OpKind

from .helpers import BOARD, make_manager, make_op, replay, state_of


def test_undo_only_affects_own_ops():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "a1"))
    session.submit(make_op("cb", 1, OpKind.ADD_SHAPE, "bob", "b1"))

    # alice undoes: only her own shape is removed
    r = session.submit(make_op("ca", 2, OpKind.UNDO, "alice"))
    assert r.accepted
    assert session.doc.shapes["a1"].deleted
    assert not session.doc.shapes["b1"].deleted

    # bob's undo affects his own, not alice's (already undone) op
    r = session.submit(make_op("cb", 2, OpKind.UNDO, "bob"))
    assert r.accepted
    assert session.doc.shapes["b1"].deleted


def test_undo_with_nothing_to_undo_is_rejected_cleanly():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    r = session.submit(make_op("ca", 1, OpKind.UNDO, "alice"))
    assert not r.accepted and r.error == "nothing to undo"
    assert session.version == 0          # rejected ops are not logged


def test_undo_restores_previous_value_not_other_users_edit():
    """Interleaving: alice sets color, bob renames, alice undoes ->
    color restored, bob's rename survives."""
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1",
                           {"color": "red", "name": "box"}))
    session.submit(make_op("ca", 2, OpKind.UPDATE_SHAPE, "alice", "s1",
                           {"color": "green"}))
    session.submit(make_op("cb", 1, OpKind.UPDATE_SHAPE, "bob", "s1",
                           {"name": "renamed"}))

    session.submit(make_op("ca", 3, OpKind.UNDO, "alice"))  # undoes color=green
    shape = session.doc.shapes["s1"]
    assert shape.attrs["color"] == "red"        # alice's change reverted
    assert shape.attrs["name"] == "renamed"     # bob's edit untouched


def test_undo_of_remove_restores_shape():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1",
                           {"v": 42, "z": 3}))
    session.submit(make_op("ca", 2, OpKind.REMOVE_SHAPE, "alice", "s1"))
    assert session.doc.shapes["s1"].deleted
    session.submit(make_op("ca", 3, OpKind.UNDO, "alice"))
    shape = session.doc.shapes["s1"]
    assert not shape.deleted and shape.attrs["v"] == 42 and shape.z == 3


def test_redo_roundtrip_and_new_edit_invalidates_redo():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1"))
    session.submit(make_op("ca", 2, OpKind.UNDO, "alice"))
    assert session.doc.shapes["s1"].deleted

    session.submit(make_op("ca", 3, OpKind.REDO, "alice"))
    assert not session.doc.shapes["s1"].deleted

    # undo again, then a *new* edit: redo stack is invalidated
    session.submit(make_op("ca", 4, OpKind.UNDO, "alice"))
    session.submit(make_op("ca", 5, OpKind.ADD_SHAPE, "alice", "s2"))
    r = session.submit(make_op("ca", 6, OpKind.REDO, "alice"))
    assert not r.accepted and r.error == "nothing to redo"


def test_undo_redo_interleaved_with_other_users_is_stable():
    """Undo and fresh ops from multiple users interleave arbitrarily;
    the final state always equals replaying the authoritative log."""
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    script = [
        ("ca", 1, OpKind.ADD_SHAPE, "alice", "a1", {"v": 1}),
        ("cb", 1, OpKind.ADD_SHAPE, "bob", "b1", {"v": 2}),
        ("ca", 2, OpKind.ADD_SHAPE, "alice", "a2", {"v": 3}),
        ("ca", 3, OpKind.UNDO, "alice", None, None),       # undoes a2
        ("cb", 2, OpKind.UPDATE_SHAPE, "bob", "b1", {"v": 20}),
        ("ca", 4, OpKind.REDO, "alice", None, None),       # redoes a2
        ("cb", 3, OpKind.UNDO, "bob", None, None),         # undoes v=20
        ("ca", 5, OpKind.UPDATE_SHAPE, "alice", "a1", {"v": 10}),
        ("cb", 4, OpKind.REDO, "bob", None, None),         # redoes v=20
    ]
    for client, seq, kind, user, target, payload in script:
        r = session.submit(make_op(client, seq, kind, user, target, payload))
        assert r.accepted, (kind, r.error)

    _, ops = session.sync("observer", "bob", 0)
    assert replay(ops) == state_of(session)
    assert session.doc.shapes["a1"].attrs["v"] == 10
    assert not session.doc.shapes["a2"].deleted
    assert session.doc.shapes["b1"].attrs["v"] == 20


def test_undo_stack_is_per_user_even_with_same_shapes():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1",
                           {"v": 0}))
    session.submit(make_op("ca", 2, OpKind.UPDATE_SHAPE, "alice", "s1",
                           {"v": 1}))
    session.submit(make_op("cb", 1, OpKind.UPDATE_SHAPE, "bob", "s1",
                           {"v": 2}))
    # bob undoes -> restores value captured before *his* edit (v=1)
    session.submit(make_op("cb", 2, OpKind.UNDO, "bob"))
    assert session.doc.shapes["s1"].attrs["v"] == 1
    # alice undoes -> restores value before *her* edit (v=0)
    session.submit(make_op("ca", 3, OpKind.UNDO, "alice"))
    assert session.doc.shapes["s1"].attrs["v"] == 0
