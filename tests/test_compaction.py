"""Snapshot compaction: old ops merge into a snapshot; new clients get
equivalent content; offline clients can still incrementally sync."""
import pytest

from whiteboard.models import OpKind

from .helpers import BOARD, make_manager, make_op, replay, state_of
from whiteboard.document import Document


def _populate(session, n=20):
    for i in range(1, n + 1):
        session.submit(make_op("ca", i, OpKind.ADD_SHAPE, "alice",
                               f"s{i}", {"z": i, "v": i}))
    session.submit(make_op("ca", n + 1, OpKind.REMOVE_SHAPE, "alice", "s2"))
    session.submit(make_op("ca", n + 2, OpKind.UPDATE_SHAPE, "alice", "s3",
                           {"v": 300}))
    return n + 2  # final version


def test_snapshot_equivalent_to_full_replay():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    final = _populate(session)
    _, full_ops = session.sync("x", "bob", 0)
    full_state = replay(full_ops)

    snap = session.compact(final)
    doc = Document.from_snapshot(snap)
    snap_state = {sid: (s.kind, dict(s.attrs), s.z, s.deleted)
                  for sid, s in doc.shapes.items()}
    assert snap_state == full_state == state_of(session)


def test_new_client_after_compaction_gets_equivalent_content():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    final = _populate(session)
    before = state_of(session)

    session.compact(15)
    # more edits after the compaction point
    session.submit(make_op("cb", 1, OpKind.ADD_SHAPE, "bob", "t1"))
    session.submit(make_op("cb", 2, OpKind.REORDER, "bob", "s5", {"z": 99}))

    snap, tail = session.sync("new-client", "bob", 0)
    assert snap is not None and snap.version == 15
    # retained tail: everything after the snapshot point
    assert [o.server_seq for o in tail] == list(range(16, final + 3))

    doc = Document.from_snapshot(snap)
    for sop in tail:
        doc.apply(sop.op)
    merged = {sid: (s.kind, dict(s.attrs), s.z, s.deleted)
              for sid, s in doc.shapes.items()}
    assert merged == state_of(session)
    assert len(merged) == len(before) + 1


def test_offline_client_syncs_across_compaction_point():
    """A client that went offline before compaction still catches up:
    it receives the snapshot plus the retained tail."""
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    final = _populate(session)          # client saw up to v5, then offline
    session.compact(18)
    session.submit(make_op("cb", 1, OpKind.ADD_SHAPE, "bob", "t1"))

    snap, tail = session.sync("stale", "bob", 5)
    assert snap is not None and snap.version == 18
    assert [o.server_seq for o in tail] == list(range(19, final + 2))

    doc = Document.from_snapshot(snap)
    for sop in tail:
        doc.apply(sop.op)
    merged = {sid: (s.kind, dict(s.attrs), s.z, s.deleted)
              for sid, s in doc.shapes.items()}
    assert merged == state_of(session)


def test_dedup_survives_compaction():
    """Retransmitting an op that was folded into the snapshot is still
    recognized as a duplicate (watermarks are carried into snapshots)."""
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    op = make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1")
    session.submit(op)
    session.submit(make_op("ca", 2, OpKind.ADD_SHAPE, "alice", "s2"))
    session.compact(2)

    r = session.submit(op)
    assert r.duplicate and session.version == 2
    assert len(session.doc.shapes) == 2


def test_compaction_is_monotonic():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    _populate(session, 5)
    session.compact(4)
    with pytest.raises(ValueError):
        session.compact(3)              # cannot move backwards
    with pytest.raises(ValueError):
        session.compact(10_000)         # cannot exceed current version
