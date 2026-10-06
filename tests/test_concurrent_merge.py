"""Concurrent merge: the server serializes concurrent submissions into
one deterministic total order; every replica applying that order
converges to identical state."""
import random
import threading

from whiteboard.models import OpKind

from .helpers import BOARD, make_manager, make_op, replay, state_of


def test_threaded_submissions_all_applied_exactly_once():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)

    n_clients, n_ops = 8, 50

    def worker(idx):
        client, user = f"c{idx}", ("alice" if idx % 2 == 0 else "bob")
        for seq in range(1, n_ops + 1):
            r = session.submit(make_op(
                client, seq, OpKind.ADD_SHAPE, user,
                f"shape-{idx}-{seq}", {"n": seq}))
            assert r.accepted and not r.duplicate

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(n_clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert session.version == n_clients * n_ops
    assert len(session.doc.shapes) == n_clients * n_ops
    # server_seqs are a contiguous total order 1..N
    seqs = [o.server_seq for o in session.log.ops_since(0)]
    assert seqs == list(range(1, n_clients * n_ops + 1))


def test_commuting_ops_converge_under_any_arrival_order():
    """The same batch of non-conflicting ops, submitted in different
    orders to independent sessions, yields identical final state."""
    ops = []
    for i in range(30):
        # explicit z: implicit "place on top" is intentionally
        # order-dependent, so commuting tests pin z explicitly
        ops.append(make_op(f"c{i % 5}", i, OpKind.ADD_SHAPE,
                           "alice", f"s{i}", {"v": i, "z": i}))
        ops.append(make_op(f"c{i % 5}", 1000 + i, OpKind.ADD_ANNOTATION,
                           "bob", f"a{i}", {"text": f"note {i}", "z": i}))

    states = []
    for trial in range(5):
        mgr = make_manager()
        session = mgr.get_or_create(BOARD)
        shuffled = ops[:]
        random.Random(trial).shuffle(shuffled)
        for op in shuffled:
            session.submit(op)
        states.append(state_of(session))

    for s in states[1:]:
        assert s == states[0]


def test_conflicting_edits_resolve_identically_for_all_replicas():
    """Two users edit the same shape concurrently; the server's total
    order decides (last-writer-wins) and every synced replica agrees."""
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1",
                           {"color": "red"}))
    session.submit(make_op("ca", 2, OpKind.UPDATE_SHAPE, "alice", "s1",
                           {"color": "green"}))
    session.submit(make_op("cb", 1, OpKind.UPDATE_SHAPE, "bob", "s1",
                           {"color": "blue"}))

    # a replica that syncs from scratch replays the same log
    _, ops = session.sync("cb", "bob", 0)
    assert replay(ops) == state_of(session)
    assert session.doc.shapes["s1"].attrs["color"] == "blue"  # last wins


def test_z_order_conflict_is_deterministic():
    mgr = make_manager()
    session = mgr.get_or_create(BOARD)
    session.submit(make_op("ca", 1, OpKind.ADD_SHAPE, "alice", "s1", {"z": 0}))
    session.submit(make_op("ca", 2, OpKind.REORDER, "alice", "s1", {"z": 5}))
    session.submit(make_op("cb", 1, OpKind.REORDER, "bob", "s1", {"z": 9}))
    _, ops = session.sync("ca", "alice", 0)
    assert replay(ops)["s1"][2] == 9 == session.doc.shapes["s1"].z
