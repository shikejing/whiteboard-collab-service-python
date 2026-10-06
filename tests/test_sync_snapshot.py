"""Offline reconnect incremental sync + snapshot compaction tests."""

from __future__ import annotations

from whiteboard.models import Op, OpId, OpType, Role
from whiteboard.service import WhiteboardService


def make_service():
    svc = WhiteboardService()
    svc.create_board("b", "owner")
    svc.grant("b", "alice", Role.EDITOR)
    svc.grant("b", "bob", Role.EDITOR)
    return svc


def add_op(cid, seq, sid, user="alice", base=0, **kw):
    return Op(OpId(cid, seq), OpType.ADD_SHAPE, user, base, shape_id=sid, **kw)


def upd_op(cid, seq, sid, props, user="alice", base=0):
    return Op(OpId(cid, seq), OpType.UPDATE_SHAPE, user, base, shape_id=sid, props=props)


def test_reconnect_gets_only_missing_ops():
    svc = make_service()
    svc.submit("b", add_op("c1", 1, "s1"))
    svc.submit("b", add_op("c1", 2, "s2"))
    svc.submit("b", add_op("c1", 3, "s3"))
    sync = svc.sync("b", "alice", last_version=1)
    assert sync.full_state is None
    assert [o.shape_id for o in sync.ops] == ["s2", "s3"]
    assert sync.version == 3


def test_resent_ops_after_disconnect_are_deduped():
    svc = make_service()
    op = add_op("c1", 1, "s1")
    svc.submit("b", op)
    # client disconnected, resends the same local seq on reconnect
    r = svc.submit("b", op)
    assert r.duplicate
    ids = [e["id"] for e in svc.render("b", "alice")["entities"]]
    assert ids == ["s1"]  # no duplicate shape


def test_missing_then_resent_window_no_loss_no_dup():
    svc = make_service()
    o1 = add_op("c1", 1, "s1")
    o2 = add_op("c1", 2, "s2")
    svc.submit("b", o1)
    # client never got ack for o1 but o2 also queued; reconnect resends both
    svc.submit("b", o1)  # dup
    svc.submit("b", o2)
    svc.submit("b", o2)  # dup
    ids = sorted(e["id"] for e in svc.render("b", "alice")["entities"])
    assert ids == ["s1", "s2"]


def test_snapshot_then_full_state_for_old_client_equivalent():
    svc = make_service()
    for i in range(5):
        svc.submit("b", add_op("c1", i + 1, f"s{i}", props={"v": i}))
    svc.compact("b", through_version=3)
    # post-compaction edits still apply normally
    svc.submit("b", upd_op("c1", 6, "s0", {"v": 99}))
    # fresh client (last_version=0) crosses the boundary -> full_state + tail
    svc.grant("b", "carol", Role.VIEWER)
    sync = svc.sync("b", "carol", last_version=0)
    assert sync.full_state is not None
    # reconstruct what carol sees: snapshot entities folded with tail ops
    ids_snapshot = {e["id"] for e in sync.full_state["entities"]}
    assert ids_snapshot == {"s0", "s1", "s2"}
    assert [o.shape_id for o in sync.ops] == ["s3", "s4", "s0"]
    # live view is complete with 5 entities and latest prop
    live = svc.render("b", "carol")["entities"]
    assert len(live) == 5
    s0 = [e for e in live if e["id"] == "s0"][0]
    assert s0["props"] == {"v": 99}


def test_compaction_does_not_break_offline_incremental_sync():
    svc = make_service()
    for i in range(4):
        svc.submit("b", add_op("c1", i + 1, f"s{i}"))
    # bob is alive at version 2, then goes offline
    svc.compact("b", through_version=3)  # folds ops 1..3 while bob is offline
    svc.submit("b", add_op("c1", 5, "s4"))  # version 5
    # bob reconnects with last_version=2 (< compacted_seq 3)
    sync = svc.sync("b", "bob", last_version=2)
    assert sync.full_state is not None
    # full snapshot (ops 1..3) + tail (ops 4..5) => sees everything
    seen = {e["id"] for e in sync.full_state["entities"]}
    seen |= {o.shape_id for o in sync.ops}
    assert seen == {"s0", "s1", "s2", "s3", "s4"}


def test_duplicate_resend_before_and_after_compaction_still_ignored():
    svc = make_service()
    o1 = add_op("c1", 1, "s1")
    svc.submit("b", o1)
    svc.submit("b", add_op("c1", 2, "s2"))
    svc.compact("b", through_version=2)
    # a client that lost its acks resends an op older than the snapshot
    r = svc.submit("b", o1)
    assert r.duplicate
    assert len(svc.render("b", "alice")["entities"]) == 2


def test_snapshot_content_equals_prefix_only():
    svc = make_service()
    for i in range(4):
        svc.submit("b", add_op("c1", i + 1, f"s{i}"))
    snap = svc.compact("b", through_version=2)
    assert snap.seq == 2
    view = svc.snapshots.snapshot_view(snap)
    assert [e["id"] for e in view["entities"]] == ["s0", "s1"]


def test_connect_returns_sync_and_disconnect_isolates_others():
    svc = make_service()
    svc.submit("b", add_op("c1", 1, "s1"))
    conn_a, sync_a = svc.connect("b", "c1", "alice", 0)
    conn_b, _ = svc.connect("b", "c2", "bob", 1)
    assert len(svc.sessions.connections_of("b")) == 2
    svc.disconnect(conn_a.conn_id)
    remaining = [c.client_id for c in svc.sessions.connections_of("b")]
    assert remaining == ["c2"]
    # alice reconnects as a new connection; board content intact
    conn_a2, sync_a2 = svc.connect("b", "c1", "alice", sync_a.version)
    assert sync_a2.ops == []  # already up to date
    assert set(svc.sessions.clients_of("b")) == {"c1", "c2"}
