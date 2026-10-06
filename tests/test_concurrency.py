"""True multi-thread concurrency tests against the service lock."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from whiteboard.models import Op, OpId, OpType, Role
from whiteboard.service import WhiteboardService


def _svc():
    svc = WhiteboardService()
    svc.create_board("b", "owner")
    for u in ("u1", "u2", "u3", "u4"):
        svc.grant("b", u, Role.EDITOR)
    return svc


def test_parallel_creates_serialize_without_loss():
    svc = _svc()

    def worker(uid):
        for seq in range(50):
            svc.submit(
                "b",
                Op(OpId(uid, seq + 1), OpType.ADD_SHAPE, uid, 0, shape_id=f"{uid}-{seq}"),
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, ["u1", "u2", "u3", "u4"]))

    view = svc.render("b", "owner")
    assert len(view["entities"]) == 200  # nothing lost
    assert view["version"] == 200  # dense global versions
    log_versions = list(range(1, 201))
    assert svc._state("b").version == 200


def test_parallel_same_field_writes_always_lww_winner():
    # regardless of thread scheduling, final field owner is deterministic:
    # the lexicographically largest (client_id, seq)
    outcomes = set()
    for _ in range(5):
        svc = _svc()
        svc.submit("b", Op(OpId("a0", 1), OpType.ADD_SHAPE, "owner", 0, shape_id="s", props={"c": "0"}))

        def worker(uid, seq, val):
            svc.submit(
                "b",
                Op(OpId(uid, seq), OpType.UPDATE_SHAPE, uid, 1, shape_id="s", props={"c": val}),
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda t: worker(*t), [("u1", 9, "A"), ("u2", 3, "B"), ("u3", 9, "C"), ("u4", 1, "D")]))
        val = svc.render("b", "owner")["entities"][0]["props"]["c"]
        outcomes.add(val)
    assert outcomes == {"D"}  # (u4,1) is the lex-max id


def test_parallel_duplicate_resends_same_version():
    svc = _svc()
    op = Op(OpId("u1", 1), OpType.ADD_SHAPE, "u1", 0, shape_id="once")
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        svc.submit("b", op)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: worker(), range(8)))
    assert svc._state("b").version == 1
    assert len(svc.render("b", "owner")["entities"]) == 1


def test_parallel_undo_while_others_edit_is_isolated():
    svc = _svc()
    svc.submit("b", Op(OpId("u1", 1), OpType.ADD_SHAPE, "u1", 0, shape_id="s", props={"a": "1"}))
    svc.submit("b", Op(OpId("u1", 2), OpType.UPDATE_SHAPE, "u1", 1, shape_id="s", props={"a": "2"}))

    def undoer():
        svc.undo("b", "u1")

    def editor():
        # later op on a different field must survive u1 undo
        for seq in range(20):
            svc.submit("b", Op(OpId("z9", seq + 1), OpType.UPDATE_SHAPE, "u2", 2, shape_id="s", props={"b": seq}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [undoer, editor]))
    props = svc.render("b", "owner")["entities"][0]["props"]
    assert props["b"] == 19
    # undo of u1's a=2 leaves the field at its pre-edit value 1
    assert props["a"] == "1"
