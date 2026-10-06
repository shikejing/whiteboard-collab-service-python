"""Out-of-order delivery and retransmission: duplicates are detected
via (client_id, client_seq) and never create duplicate shapes or gaps."""
import random

from whiteboard.models import OpKind

from .helpers import BOARD, make_manager, make_op, state_of


def test_duplicate_retransmits_are_ignored():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    op = make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1", {"v": 1})
    r1 = session.submit(op)
    for _ in range(5):  # network retransmit storm
        r = session.submit(op)
        assert r.duplicate and r.server_seq == r1.server_seq
    assert session.version == 1
    assert list(session.doc.shapes) == ["s1"]


def test_out_of_order_arrival_from_one_client():
    """A client's ops may arrive out of order (retry + reorder); each is
    applied exactly once and retransmits are still detected."""
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    ops = [make_op("ca", i, OpKind.ADD_SHAPE, "alice", f"s{i}", {"z": i})
           for i in range(1, 21)]
    shuffled = ops[:]
    random.Random(7).shuffle(shuffled)
    for op in shuffled:
        assert session.submit(op).accepted
    # retransmit the whole batch again, again shuffled
    random.Random(8).shuffle(shuffled)
    for op in shuffled:
        assert session.submit(op).duplicate
    assert session.version == 20
    assert len(session.doc.shapes) == 20


def test_interleaved_duplicates_between_new_ops():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1"))
    session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1"))  # dup
    session.submit(make_op("ca", 2, OpKind.ADD_SHAPE, "alice", "s2"))
    session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1"))  # dup
    session.submit(make_op("ca", 2, OpKind.ADD_SHAPE, "alice", "s2"))  # dup
    session.submit(make_op("ca", 3, OpKind.REMOVE_SHAPE, "alice", "s1"))
    session.submit(make_op("ca", 2, OpKind.ADD_SHAPE, "alice", "s2"))  # dup
    assert session.version == 3
    state = state_of(session)
    assert state["s1"][3] is True       # deleted
    assert state["s2"][3] is False      # alive, applied once


def test_no_lost_edits_when_client_retries_while_others_edit():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    # alice retries op 1 while bob's ops land in between
    session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "a1"))
    session.submit(make_op("cb", 1, OpKind.ADD_SHAPE, "bob", "b1"))
    assert session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "a1")).duplicate
    session.submit(make_op("cb", 2, OpKind.ADD_SHAPE, "bob", "b2"))
    session.submit(make_op("ca", 2, OpKind.ADD_SHAPE, "alice", "a2"))
    assert session.version == 4
    assert set(session.doc.shapes) == {"a1", "a2", "b1", "b2"}
