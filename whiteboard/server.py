"""FastAPI HTTP + WebSocket interface for the whiteboard service.

Wire protocol (JSON):

  POST /boards/{board}/grants
      body: {"by": user, "user_id": user, "role": "owner|editor|viewer"}
      Bootstrap rule: the very first grant on a board may be self-issued.
  POST /boards/{board}/ops
      body: operation JSON (see `op_from_dict`). Idempotent on
      (client_id, client_seq): retransmits return {"duplicate": true}.
  GET  /boards/{board}/sync?user_id=&client_id=&since=
      -> {"version": N, "snapshot": {...}|null, "ops": [...]}
  POST /boards/{board}/compact   body: {"by": user, "upto_version": N}
  WS   /boards/{board}/ws?user_id=&client_id=
      Client sends operation JSON objects; the server broadcasts each
      sequenced operation to every connected client of the board.

Consistency semantics: the server serializes all operations of a board
into one total order (server_seq). Clients apply the stream in that
order and therefore converge. `base_version` is advisory (optimistic
concurrency is *not* enforced); conflicts resolve last-writer-wins in
server_seq order, which is deterministic and identical for everyone.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .auth import AccessDenied
from .models import (Operation, OpKind, OpRef, Role, SequencedOp, Shape,
                     Snapshot)
from .session import SessionManager


# -- (de)serialization ---------------------------------------------------------

def op_from_dict(d: dict[str, Any], board_id: str) -> Operation:
    return Operation(
        ref=OpRef(client_id=d["client_id"], client_seq=int(d["client_seq"])),
        kind=OpKind(d["kind"]),
        board_id=board_id,
        user_id=d["user_id"],
        base_version=int(d.get("base_version", 0)),
        target_id=d.get("target_id"),
        payload=d.get("payload") or {},
    )


def op_to_dict(op: Operation) -> dict[str, Any]:
    return {"client_id": op.ref.client_id, "client_seq": op.ref.client_seq,
            "kind": op.kind.value, "board_id": op.board_id,
            "user_id": op.user_id, "base_version": op.base_version,
            "target_id": op.target_id, "payload": op.payload}


def sequenced_to_dict(sop: SequencedOp) -> dict[str, Any]:
    d = op_to_dict(sop.op)
    d["server_seq"] = sop.server_seq
    return d


def shape_to_dict(s: Shape) -> dict[str, Any]:
    return {"id": s.id, "kind": s.kind, "attrs": s.attrs,
            "z": s.z, "deleted": s.deleted}


def snapshot_to_dict(snap: Snapshot) -> dict[str, Any]:
    return {"version": snap.version,
            "shapes": {sid: shape_to_dict(s) for sid, s in snap.shapes.items()},
            "client_watermarks": snap.client_watermarks}


# -- request bodies --------------------------------------------------------------

class GrantBody(BaseModel):
    by: str
    user_id: str
    role: Role


class CompactBody(BaseModel):
    by: str
    upto_version: int


# -- app -------------------------------------------------------------------------

def create_app(manager: Optional[SessionManager] = None) -> FastAPI:
    app = FastAPI(title="whiteboard-collab-service")
    mgr = manager or SessionManager()
    app.state.manager = mgr
    # board_id -> set of connected websockets (broadcast fan-out)
    app.state.subscribers = {}

    def deny_to_http(exc: AccessDenied) -> HTTPException:
        return HTTPException(status_code=403, detail=str(exc))

    @app.post("/boards/{board}/grants")
    def grant(board: str, body: GrantBody):
        session = mgr.get_or_create(board)
        # bootstrap: first grant on a board may be self-issued
        has_grants = any(b == board for (b, _) in mgr.acl._grants)
        if has_grants:
            try:
                mgr.acl.require_owner(board, body.by)
            except AccessDenied as exc:
                raise deny_to_http(exc)
        mgr.acl.grant(board, body.user_id, body.role)
        return {"ok": True}

    @app.post("/boards/{board}/ops")
    def submit_op(board: str, body: dict[str, Any]):
        session = mgr.get_or_create(board)
        op = op_from_dict(body, board)
        try:
            result = session.submit(op)
        except AccessDenied as exc:
            raise deny_to_http(exc)
        return {"accepted": result.accepted, "duplicate": result.duplicate,
                "server_seq": result.server_seq, "error": result.error,
                "version": session.version}

    @app.get("/boards/{board}/sync")
    def sync(board: str, user_id: str, client_id: str = "", since: int = 0):
        session = mgr.get_or_create(board)
        try:
            snap, ops = session.sync(client_id, user_id, since)
        except AccessDenied as exc:
            raise deny_to_http(exc)
        return {"version": session.version,
                "snapshot": snapshot_to_dict(snap) if snap else None,
                "ops": [sequenced_to_dict(o) for o in ops]}

    @app.post("/boards/{board}/compact")
    def compact(board: str, body: CompactBody):
        session = mgr.get_or_create(board)
        try:
            mgr.acl.require_owner(board, body.by)
            snap = session.compact(body.upto_version)
        except AccessDenied as exc:
            raise deny_to_http(exc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "snapshot_version": snap.version}

    @app.get("/boards/{board}/audit")
    def audit(board: str):
        return {"entries": [
            {"user_id": e.user_id, "action": e.action,
             "reason": e.reason, "ts": e.ts}
            for e in mgr.acl.audit_log if e.board_id == board]}

    @app.websocket("/boards/{board}/ws")
    async def ws(websocket: WebSocket, board: str, user_id: str,
                 client_id: str):
        session = mgr.get_or_create(board)
        try:
            session.connect(client_id, user_id)
        except AccessDenied:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        subs = app.state.subscribers.setdefault(board, set())
        subs.add(websocket)
        try:
            while True:
                raw = await websocket.receive_text()
                op = op_from_dict(json.loads(raw), board)
                try:
                    result = session.submit(op)
                except AccessDenied as exc:
                    await websocket.send_text(json.dumps(
                        {"error": str(exc), "client_seq": op.ref.client_seq}))
                    continue
                if result.accepted and not result.duplicate:
                    sequenced = session.log.ops_since(result.server_seq - 1)
                    msg = json.dumps(
                        {"ops": [sequenced_to_dict(o) for o in sequenced],
                         "version": session.version})
                    for peer in list(subs):
                        try:
                            await peer.send_text(msg)
                        except Exception:
                            subs.discard(peer)
        except WebSocketDisconnect:
            pass
        finally:
            subs.discard(websocket)
            session.disconnect(client_id)

    return app
