"""Access control: viewers are read-only, unknown users have no access,
every violation is rejected and audited; concurrent edits to the same
shape resolve deterministically."""
import pytest

from whiteboard.auth import AccessDenied
from whiteboard.models import OpKind, Role

from .helpers import BOARD, make_manager, make_op, replay, state_of


def test_viewer_cannot_write_but_can_read():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1"))

    session.connect("cc", "carol")                  # read ok
    snap, ops = session.sync("cc", "carol", 0)
    assert len(ops) == 1

    for kind in (OpKind.ADD_SHAPE, OpKind.UPDATE_SHAPE, OpKind.REMOVE_SHAPE,
                 OpKind.REORDER, OpKind.UNDO):
        with pytest.raises(AccessDenied):
            session.submit(make_op("cc", 1, kind, "carol", "s1"))
    assert session.version == 1                     # nothing slipped through


def test_unknown_user_has_no_access_at_all():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    with pytest.raises(AccessDenied):
        session.connect("cx", "mallory")
    with pytest.raises(AccessDenied):
        session.submit(make_op("cx", 1, OpKind.ADD_SHAPE, "mallory", "s1"))


def test_violations_are_audited():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    for i in range(3):
        with pytest.raises(AccessDenied):
            session.submit(make_op("cc", i, OpKind.ADD_SHAPE, "carol", f"s{i}"))
    with pytest.raises(AccessDenied):
        session.submit(make_op("cx", 1, OpKind.ADD_SHAPE, "mallory", "s1"))

    entries = [e for e in mgr.acl.audit_log if e.board_id == BOARD]
    assert len(entries) == 4
    assert {e.user_id for e in entries} == {"carol", "mallory"}
    assert all(e.action == "write" for e in entries)
    viewer_entries = [e for e in entries if e.user_id == "carol"]
    assert all(e.reason == "role=viewer" for e in viewer_entries)


def test_role_change_takes_effect_immediately():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    with pytest.raises(AccessDenied):
        session.submit(make_op("cc", 1, OpKind.ADD_SHAPE, "carol", "s1"))
    mgr.acl.grant(BOARD, "carol", Role.EDITOR)      # promoted
    r = session.submit(make_op("cc", 1, OpKind.ADD_SHAPE, "carol", "s1"))
    assert r.accepted
    mgr.acl.revoke(BOARD, "carol")                  # demoted
    with pytest.raises(AccessDenied):
        session.submit(make_op("cc", 2, OpKind.ADD_SHAPE, "carol", "s2"))


def test_concurrent_same_shape_edits_deterministic_and_explainable():
    """Many users edit the same shape; the result equals replaying the
    authoritative log (last-writer-wins in server_seq order)."""
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1",
                           {"color": "red"}))
    users = [("ca", "alice"), ("cb", "bob")]
    seq = {"ca": 2, "cb": 1}
    for i in range(20):
        client, user = users[i % 2]
        session.submit(make_op(client, seq[client], OpKind.UPDATE_SHAPE,
                               user, "s1", {"color": f"c{i}"}))
        seq[client] += 1

    _, ops = session.sync("anyone", "carol", 0)
    assert replay(ops) == state_of(session)
    # explainable: winner is the submitter of the last logged update
    last_update = [o for o in ops
                   if o.op.kind is OpKind.UPDATE_SHAPE][-1]
    assert session.doc.shapes["s1"].attrs["color"] == \
        last_update.op.payload["color"]
