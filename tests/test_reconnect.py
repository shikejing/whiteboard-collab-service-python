"""Offline reconnect & incremental sync: a reconnecting client receives
exactly the ops it missed; its own queued retransmits are deduplicated."""
from whiteboard.models import OpKind

from .helpers import BOARD, make_manager, make_op, replay, state_of


def test_disconnect_does_not_disturb_other_clients():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    session.connect("ca", "alice")
    session.connect("cb", "bob")
    session.connect("cc", "carol")
    session.disconnect("cb")  # bob drops
    r = session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1"))
    assert r.accepted
    session.disconnect("ca")
    session.disconnect("cc")
    r = session.submit(make_op("cb", 1, OpKind.ADD_SHAPE, "bob", "s2"))
    assert r.accepted and session.version == 2


def test_reconnect_receives_exactly_missed_ops():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    session.connect("cb", "bob")
    session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1"))
    session.submit(make_op("ca", 2, OpKind.ADD_SHAPE, "alice", "s2"))

    session.disconnect("cb")  # bob goes offline; alice keeps editing
    session.submit(make_op("ca", 3, OpKind.UPDATE_SHAPE, "alice", "s1",
                           {"x": 10}))
    session.submit(make_op("ca", 4, OpKind.REMOVE_SHAPE, "alice", "s2"))
    session.submit(make_op("cc", 1, OpKind.ADD_ANNOTATION, "alice", "n1",
                           {"text": "hi"}))

    session.connect("cb", "bob")  # reconnect: sync since last seen (v2)
    snap, ops = session.sync("cb", "bob", since_version=2)
    assert snap is None
    assert [o.server_seq for o in ops] == [3, 4, 5]


def test_reconnect_with_queued_retransmits_no_duplicates():
    """Client sent ops, lost the connection before the ack, and
    retransmits after reconnect: no duplicate shapes appear."""
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    op = make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1")
    assert session.submit(op).accepted      # applied, but ack "lost"
    session.disconnect("ca")
    session.connect("ca", "alice")
    r = session.submit(op)                  # retransmit after reconnect
    assert r.duplicate and r.server_seq == 1
    assert session.version == 1 and len(session.doc.shapes) == 1


def test_full_replay_and_incremental_sync_agree():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    for i in range(1, 11):
        session.submit(make_op("ca", i, OpKind.ADD_SHAPE, "alice",
                               f"s{i}", {"z": i}))
    session.submit(make_op("ca", 11, OpKind.REMOVE_SHAPE, "alice", "s3"))
    session.submit(make_op("ca", 12, OpKind.UPDATE_SHAPE, "alice", "s4",
                           {"c": 1}))

    _, full = session.sync("new", "bob", 0)
    assert replay(full) == state_of(session)

    _, tail = session.sync("old", "bob", 8)
    assert [o.server_seq for o in tail] == [9, 10, 11, 12]
