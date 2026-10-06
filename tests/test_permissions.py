"""Access control tests at service and HTTP level."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main
from whiteboard.models import Op, OpId, OpType, Role
from whiteboard.permissions import PermissionDenied
from whiteboard.service import WhiteboardService


def make_service():
    svc = WhiteboardService()
    svc.create_board("b", "owner")
    svc.grant("b", "editor", Role.EDITOR)
    svc.grant("b", "viewer", Role.VIEWER)
    return svc


def test_viewer_write_denied_and_audited():
    svc = make_service()
    op = Op(OpId("c", 1), OpType.DELETE_SHAPE, "viewer", 0, shape_id="s")
    with pytest.raises(PermissionDenied):
        svc.submit("b", op)
    trail = svc.acl.audit_trail("b")
    denials = [e for e in trail if not e.allowed]
    assert any("read-only" in e.reason for e in denials)


def test_unknown_user_denied_read_and_write():
    svc = make_service()
    with pytest.raises(PermissionDenied):
        svc.render("b", "ghost")
    with pytest.raises(PermissionDenied):
        svc.submit("b", Op(OpId("g", 1), OpType.ADD_SHAPE, "ghost", 0, shape_id="x"))


def test_editor_and_owner_can_write():
    svc = make_service()
    svc.submit("b", Op(OpId("e", 1), OpType.ADD_SHAPE, "editor", 0, shape_id="e1"))
    svc.submit("b", Op(OpId("o", 1), OpType.ADD_SHAPE, "owner", 0, shape_id="o1"))
    ids = {x["id"] for x in svc.render("b", "viewer")["entities"]}
    assert ids == {"e1", "o1"}


def test_viewer_cannot_undo():
    svc = make_service()
    with pytest.raises(PermissionDenied):
        svc.undo("b", "viewer")


def test_concurrent_conflict_resolves_deterministically_and_explainably():
    svc = make_service()
    svc.grant("b", "a2", Role.EDITOR)
    svc.submit("b", Op(OpId("c0", 1), OpType.ADD_SHAPE, "owner", 0, shape_id="s", props={"c": "0"}))
    # two editors write the same field "concurrently" (different base version)
    svc.submit("b", Op(OpId("c1", 5), OpType.UPDATE_SHAPE, "editor", 1, shape_id="s", props={"c": "A"}))
    svc.submit("b", Op(OpId("c2", 5), OpType.UPDATE_SHAPE, "a2", 1, shape_id="s", props={"c": "B"}))
    # deterministic: lex-max (client_id, seq) = c2/5 wins
    val = svc.render("b", "owner")["entities"][0]["props"]["c"]
    assert val == "B"
    rec = svc._state("b").shapes["s"]
    winner = rec.fields["c"]
    assert winner.op_id == OpId("c2", 5)


# --------------------------------------------------------------------- HTTP


@pytest.fixture()
def client():
    from whiteboard.api import service as svc
    svc._boards.clear()
    svc._locks.clear()
    svc.acl._roles.clear()
    svc.acl.audit.clear()
    return TestClient(main.app)


def test_http_viewer_submit_403(client):
    client.post("/api/boards/b?owner=owner")
    client.post("/api/boards/b/roles", headers={"X-User-Id": "owner"}, json={"user_id": "eve", "role": "viewer"})
    op = {"id": {"client_id": "c", "seq": 1}, "type": "delete_shape", "author": "eve", "shape_id": "s"}
    r = client.post("/api/boards/b/ops", headers={"X-User-Id": "eve"}, json=op)
    assert r.status_code == 403
    r = client.get("/api/boards/b/audit", headers={"X-User-Id": "owner"})
    assert any(not e["allowed"] for e in r.json()["entries"])


def test_http_spoofed_author_403(client):
    client.post("/api/boards/b?owner=owner")
    client.post("/api/boards/b/roles", headers={"X-User-Id": "owner"}, json={"user_id": "alice", "role": "editor"})
    op = {"id": {"client_id": "c", "seq": 1}, "type": "add_shape", "author": "owner", "shape_id": "s"}
    r = client.post("/api/boards/b/ops", headers={"X-User-Id": "alice"}, json=op)
    assert r.status_code == 403


def test_http_role_grant_only_owner(client):
    client.post("/api/boards/b?owner=owner")
    client.post("/api/boards/b/roles", headers={"X-User-Id": "owner"}, json={"user_id": "alice", "role": "editor"})
    r = client.post("/api/boards/b/roles", headers={"X-User-Id": "alice"}, json={"user_id": "x", "role": "viewer"})
    assert r.status_code == 403
