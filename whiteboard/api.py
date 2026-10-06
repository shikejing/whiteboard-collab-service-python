"""FastAPI HTTP layer for the whiteboard collaboration service.

Auth convention
---------------
Examples/tests pass the acting user with the ``X-User-Id`` header. In
production this would be replaced by a verified session/JWT; every endpoint
still enforces board-scoped authorization afterwards.

Endpoints (all per board)
-------------------------
* ``POST /boards/{board_id}?owner=...`` create a board
* ``POST /boards/{board_id}/roles`` grant a role
* ``GET  /boards/{board_id}/state`` current full content
* ``POST /boards/{board_id}/ops`` submit one op (or batch)
* ``POST /boards/{board_id}/undo`` / ``/redo``
* ``POST /boards/{board_id}/connect`` open a session connection + sync
* ``POST /boards/{board_id}/disconnect``
* ``GET  /boards/{board_id}/sync?since=`` incremental sync
* ``POST /boards/{board_id}/compact`` fold history into a snapshot
* ``GET  /boards/{board_id}/audit`` access audit trail
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .models import Role
from .permissions import PermissionDenied
from .serialization import op_from_json, op_id_to_json, op_to_json
from .service import WhiteboardService

router = APIRouter(prefix="/api", tags=["whiteboard"])

# A single process-wide in-memory service instance.
service = WhiteboardService()


def _sync_to_json(sync) -> dict:
    return {
        "board_id": sync.board_id,
        "from_version": sync.from_version,
        "version": sync.version,
        "ops": [op_to_json(o) for o in sync.ops],
        "full_state": sync.full_state,
    }


def _denied(exc: PermissionDenied) -> HTTPException:
    return HTTPException(status_code=403, detail=exc.reason)


# ----------------------------------------------------------------- schemas


class OpIdIn(BaseModel):
    client_id: str
    seq: int
    tag: str = "client"


class OpIn(BaseModel):
    id: OpIdIn
    type: str
    author: str
    base_version: int = 0
    shape_id: str | None = None
    annot_id: str | None = None
    props: dict | None = None
    z_index: int | None = None
    alive_value: bool | None = None
    removed_fields: list[str] = Field(default_factory=list)
    ops: list["OpIn"] = Field(default_factory=list)


class RoleIn(BaseModel):
    user_id: str
    role: Role


class ConnectIn(BaseModel):
    client_id: str
    last_version: int = 0


# ---------------------------------------------------------------- endpoints


@router.post("/boards/{board_id}")
def create_board(board_id: str, owner: str = Query(...)):
    state = service.create_board(board_id, owner)
    return {"board_id": board_id, "version": state.version}


@router.post("/boards/{board_id}/roles")
def grant_role(board_id: str, body: RoleIn, x_user_id: str = Header(alias="X-User-Id")):
    # only owner may provision roles
    role = service.acl.role_of(board_id, x_user_id)
    if role is not Role.OWNER:
        service.acl._record(board_id, x_user_id, "grant_role", False, "only owner can grant roles")
        raise HTTPException(status_code=403, detail="only owner can grant roles")
    service.grant(board_id, body.user_id, body.role)
    return {"board_id": board_id, "user_id": body.user_id, "role": body.role.value}


@router.get("/boards/{board_id}/state")
def get_state(board_id: str, x_user_id: str = Header(alias="X-User-Id")):
    try:
        return service.render(board_id, x_user_id)
    except PermissionDenied as exc:
        raise _denied(exc)


@router.post("/boards/{board_id}/ops")
def submit_op(board_id: str, body: OpIn, x_user_id: str = Header(alias="X-User-Id")):
    # never trust a mismatched author header vs payload
    if body.author != x_user_id:
        raise HTTPException(status_code=403, detail="op author must match X-User-Id")
    try:
        op = op_from_json(body.model_dump())
        result = service.submit(board_id, op)
    except PermissionDenied as exc:
        raise _denied(exc)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {
        "accepted": result.accepted,
        "version": result.version,
        "duplicate": result.duplicate,
        "op": op_to_json(result.op) if result.op else None,
        "reason": result.reason,
    }


@router.post("/boards/{board_id}/undo")
def undo(board_id: str, x_user_id: str = Header(alias="X-User-Id")):
    try:
        result = service.undo(board_id, x_user_id)
    except PermissionDenied as exc:
        raise _denied(exc)
    return {"accepted": result.accepted, "version": result.version, "reason": result.reason}


@router.post("/boards/{board_id}/redo")
def redo(board_id: str, x_user_id: str = Header(alias="X-User-Id")):
    try:
        result = service.redo(board_id, x_user_id)
    except PermissionDenied as exc:
        raise _denied(exc)
    return {"accepted": result.accepted, "version": result.version, "reason": result.reason}


@router.post("/boards/{board_id}/connect")
def connect(board_id: str, body: ConnectIn, x_user_id: str = Header(alias="X-User-Id")):
    try:
        conn, sync = service.connect(board_id, body.client_id, x_user_id, body.last_version)
    except PermissionDenied as exc:
        raise _denied(exc)
    return {"conn_id": conn.conn_id, "sync": _sync_to_json(sync)}


@router.post("/boards/{board_id}/disconnect")
def disconnect(board_id: str, conn_id: str = Query(...)):
    service.disconnect(conn_id)
    return {"conn_id": conn_id, "connected": False}


@router.get("/boards/{board_id}/sync")
def sync(board_id: str, since: int = 0, x_user_id: str = Header(alias="X-User-Id")):
    try:
        delta = service.sync(board_id, x_user_id, since)
    except PermissionDenied as exc:
        raise _denied(exc)
    return _sync_to_json(delta)


@router.post("/boards/{board_id}/compact")
def compact(board_id: str, through_version: int | None = None, x_user_id: str = Header(alias="X-User-Id")):
    # owner-only housekeeping
    role = service.acl.role_of(board_id, x_user_id)
    if role is not Role.OWNER:
        raise HTTPException(status_code=403, detail="only owner can compact history")
    snapshot = service.compact(board_id, through_version)
    return {"board_id": board_id, "snapshot_seq": snapshot.seq, "version": snapshot.version}


@router.get("/boards/{board_id}/audit")
def audit(board_id: str, x_user_id: str = Header(alias="X-User-Id")):
    role = service.acl.role_of(board_id, x_user_id)
    if role is not Role.OWNER:
        raise HTTPException(status_code=403, detail="only owner can read audit")
    entries = [asdict(e) for e in service.acl.audit_trail(board_id)]
    return {"board_id": board_id, "entries": entries}
