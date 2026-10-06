"""End-to-end tests of the HTTP + WebSocket API."""
import json

import pytest
from fastapi.testclient import TestClient

from whiteboard.server import create_app

BOARD = "api-board"


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        # bootstrap: first grant may be self-issued (owner)
        r = c.post(f"/boards/{BOARD}/grants",
                   json={"by": "alice", "user_id": "alice", "role": "owner"})
        assert r.status_code == 200
        c.post(f"/boards/{BOARD}/grants",
               json={"by": "alice", "user_id": "bob", "role": "editor"})
        c.post(f"/boards/{BOARD}/grants",
               json={"by": "alice", "user_id": "carol", "role": "viewer"})
        yield c


def op(client_id, seq, kind, user, target=None, payload=None):
    return {"client_id": client_id, "client_seq": seq, "kind": kind,
            "user_id": user, "base_version": 0, "target_id": target,
            "payload": payload or {}}


def test_submit_sync_compact_flow(client):
    r = client.post(f"/boards/{BOARD}/ops",
                    json=op("ca", 1, "add_shape", "alice", "s1", {"v": 1}))
    assert r.status_code == 200 and r.json()["accepted"]
    assert r.json()["server_seq"] == 1

    # duplicate retransmit over HTTP
    r = client.post(f"/boards/{BOARD}/ops",
                    json=op("ca", 1, "add_shape", "alice", "s1", {"v": 1}))
    assert r.json()["duplicate"] is True

    client.post(f"/boards/{BOARD}/ops",
                json=op("ca", 2, "add_shape", "alice", "s2", {"v": 2}))

    r = client.get(f"/boards/{BOARD}/sync",
                   params={"user_id": "bob", "client_id": "cb", "since": 1})
    body = r.json()
    assert body["snapshot"] is None
    assert [o["server_seq"] for o in body["ops"]] == [2]

    # compact (owner only), then a fresh sync returns a snapshot
    r = client.post(f"/boards/{BOARD}/compact",
                    json={"by": "alice", "upto_version": 2})
    assert r.json()["snapshot_version"] == 2
    r = client.get(f"/boards/{BOARD}/sync",
                   params={"user_id": "bob", "client_id": "cb", "since": 0})
    assert r.json()["snapshot"]["version"] == 2
    assert set(r.json()["snapshot"]["shapes"]) == {"s1", "s2"}


def test_viewer_rejected_and_audited_over_http(client):
    r = client.post(f"/boards/{BOARD}/ops",
                    json=op("cc", 1, "add_shape", "carol", "s1"))
    assert r.status_code == 403
    r = client.get(f"/boards/{BOARD}/audit")
    assert any(e["user_id"] == "carol" for e in r.json()["entries"])

    # non-owner cannot compact
    r = client.post(f"/boards/{BOARD}/compact",
                    json={"by": "bob", "upto_version": 1})
    assert r.status_code == 403


def test_grant_requires_owner_after_bootstrap(client):
    r = client.post(f"/boards/{BOARD}/grants",
                    json={"by": "bob", "user_id": "dave", "role": "editor"})
    assert r.status_code == 403
    r = client.post(f"/boards/{BOARD}/grants",
                    json={"by": "alice", "user_id": "dave", "role": "editor"})
    assert r.status_code == 200


def test_websocket_broadcast_to_all_clients(client):
    with client.websocket_connect(
            f"/boards/{BOARD}/ws?user_id=alice&client_id=ca") as wa:
        with client.websocket_connect(
                f"/boards/{BOARD}/ws?user_id=bob&client_id=cb") as wb:
            wa.send_text(json.dumps(
                op("ca", 1, "add_shape", "alice", "s1", {"v": 1})))
            msg_a = json.loads(wa.receive_text())
            msg_b = json.loads(wb.receive_text())
            assert msg_a["ops"][0]["server_seq"] == 1
            assert msg_b["ops"][0]["target_id"] == "s1"
            assert msg_a["version"] == msg_b["version"] == 1


def test_websocket_viewer_cannot_write(client):
    with client.websocket_connect(
            f"/boards/{BOARD}/ws?user_id=carol&client_id=cc") as wc:
        wc.send_text(json.dumps(op("cc", 1, "add_shape", "carol", "s1")))
        msg = json.loads(wc.receive_text())
        assert "error" in msg
