"""Shared helpers for building operations in tests."""
from __future__ import annotations

from whiteboard.models import Operation, OpKind, OpRef
from whiteboard.session import SessionManager
from whiteboard.models import Role

BOARD = "board-1"


def make_op(client: str, seq: int, kind: OpKind, user: str,
            target: str | None = None, payload: dict | None = None,
            base_version: int = 0) -> Operation:
    return Operation(ref=OpRef(client, seq), kind=kind, board_id=BOARD,
                     user_id=user, base_version=base_version,
                     target_id=target, payload=payload or {})


def make_manager() -> SessionManager:
    mgr = SessionManager()
    mgr.acl.grant(BOARD, "alice", Role.OWNER)
    mgr.acl.grant(BOARD, "bob", Role.EDITOR)
    mgr.acl.grant(BOARD, "carol", Role.VIEWER)
    return mgr


def replay(ops) -> dict:
    """Independently replay sequenced ops into a fresh Document."""
    from whiteboard.document import Document
    doc = Document()
    for sop in ops:
        doc.apply(sop.op)
    return {sid: (s.kind, dict(s.attrs), s.z, s.deleted)
            for sid, s in doc.shapes.items()}


def state_of(session) -> dict:
    return {sid: (s.kind, dict(s.attrs), s.z, s.deleted)
            for sid, s in session.doc.shapes.items()}
