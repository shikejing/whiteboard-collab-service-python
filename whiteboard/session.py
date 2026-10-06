"""Board sessions: connection registry + the authoritative merge point.

A BoardSession owns one OpLog, one Document, one UndoStack and shares
the ACL. All operations for a board are funneled through `submit`,
which takes a lock and assigns the global sequence — this serialization
is what makes the merge of concurrent clients deterministic: the
server's arrival order defines the single authoritative total order,
and every replica that applies the log in that order converges to the
same state regardless of the order in which clients *sent* their ops.
"""
from __future__ import annotations

import threading
from typing import Optional

from .auth import AccessControl
from .document import Document
from .history import OpLog
from .models import (Operation, OpKind, SequencedOp, Snapshot, SubmitResult)
from .undo import UndoStack

_CONTENT_KINDS = {
    OpKind.ADD_SHAPE, OpKind.UPDATE_SHAPE, OpKind.REMOVE_SHAPE,
    OpKind.ADD_ANNOTATION, OpKind.UPDATE_ANNOTATION,
    OpKind.REMOVE_ANNOTATION, OpKind.REORDER,
}


class BoardSession:
    """All state for one board; serializes and applies operations."""

    def __init__(self, board_id: str, acl: AccessControl) -> None:
        self.board_id = board_id
        self.acl = acl
        self.log = OpLog()
        self.doc = Document()
        self.undo = UndoStack()
        self._lock = threading.RLock()
        # --- idempotency / dedup state ---
        self._applied: dict[str, set[int]] = {}    # client -> applied client_seqs
        self._watermarks: dict[str, int] = {}      # client -> contiguous prefix
        self._seq_by_ref: dict[str, int] = {}      # ref key -> server_seq
        # --- connections (informational; never blocks editing) ---
        self._clients: dict[str, str] = {}         # client_id -> user_id

    # -- connections ---------------------------------------------------------

    def connect(self, client_id: str, user_id: str) -> None:
        self.acl.require_read(self.board_id, user_id)
        with self._lock:
            self._clients[client_id] = user_id

    def disconnect(self, client_id: str) -> None:
        # A disconnect never affects other clients or in-flight history;
        # the client's watermark is kept so retransmits after reconnect
        # are still deduplicated.
        with self._lock:
            self._clients.pop(client_id, None)

    @property
    def version(self) -> int:
        return self.log.version

    # -- the merge point -------------------------------------------------------

    def submit(self, op: Operation) -> SubmitResult:
        """Authorize, dedup, sequence and apply one operation."""
        with self._lock:
            self.acl.require_write(self.board_id, op.user_id)

            key = op.ref.key()
            if self._is_duplicate(op.ref.client_id, op.ref.client_seq):
                return SubmitResult(accepted=True, duplicate=True,
                                    server_seq=self._seq_by_ref.get(key))

            if op.kind is OpKind.UNDO:
                effect = self._resolve_undo(op.user_id)
                if effect is None:
                    return SubmitResult(accepted=False, error="nothing to undo")
                op = self._with_effect(op, effect)
                inverse = None
            elif op.kind is OpKind.REDO:
                effect = self._resolve_redo(op.user_id)
                if effect is None:
                    return SubmitResult(accepted=False, error="nothing to redo")
                op = self._with_effect(op, effect)
                inverse = None
            else:
                inverse = self.doc.compute_inverse(op)

            seq = self.log.version + 1
            self.log.append(SequencedOp(op=op, server_seq=seq, inverse=inverse))
            self.doc.apply(op)
            if op.kind in _CONTENT_KINDS:
                self.undo.record(op)
            self._mark_applied(op.ref.client_id, op.ref.client_seq)
            self._seq_by_ref[key] = seq
            return SubmitResult(accepted=True, server_seq=seq)

    # -- undo/redo resolution (called with lock held) -------------------------

    @staticmethod
    def _with_effect(op: Operation, effect: dict) -> Operation:
        merged = Operation(ref=op.ref, kind=op.kind, board_id=op.board_id,
                           user_id=op.user_id, base_version=op.base_version,
                           target_id=op.target_id,
                           payload={**op.payload, "effect": effect})
        return merged

    def _resolve_undo(self, user_id: str) -> Optional[dict]:
        while True:
            key = self.undo.pop_undo(user_id)
            if key is None:
                return None
            sequenced = self.log.find(key)
            if sequenced is None or sequenced.inverse is None:
                continue  # compacted away or was a no-op; try the next one
            self.undo.push_redo(user_id, key)
            return sequenced.inverse

    def _resolve_redo(self, user_id: str) -> Optional[dict]:
        while True:
            key = self.undo.pop_redo(user_id)
            if key is None:
                return None
            sequenced = self.log.find(key)
            if sequenced is None:
                continue
            orig = sequenced.op
            self.undo.push_undo(user_id, key)
            return {"kind": orig.kind.value, "target_id": orig.target_id,
                    "payload": orig.payload}

    # -- dedup -----------------------------------------------------------------

    def _is_duplicate(self, client_id: str, client_seq: int) -> bool:
        if client_seq <= self._watermarks.get(client_id, 0):
            return True
        return client_seq in self._applied.get(client_id, ())

    def _mark_applied(self, client_id: str, client_seq: int) -> None:
        applied = self._applied.setdefault(client_id, set())
        applied.add(client_seq)
        w = self._watermarks.get(client_id, 0)
        while w + 1 in applied:
            w += 1
        self._watermarks[client_id] = w

    # -- sync & compaction -----------------------------------------------------

    def sync(self, client_id: str, user_id: str, since_version: int):
        """Return (snapshot|None, ops) to bring a client up to date.

        A reconnecting client passes the last version it saw; it receives
        exactly the operations it missed. If its version fell behind the
        compaction point, a snapshot is prepended — the result is
        equivalent to the full history.
        """
        self.acl.require_read(self.board_id, user_id)
        with self._lock:
            return self.log.sync_for(since_version)

    def compact(self, upto_version: int) -> Snapshot:
        """Compact history at/below `upto_version` into a snapshot.

        The snapshot is rebuilt from the previous snapshot plus the
        retained log, so it exactly equals replaying the full history.
        Client watermarks are carried into the snapshot so dedup of
        retransmitted old operations keeps working afterwards.
        """
        with self._lock:
            if upto_version > self.log.version:
                raise ValueError("cannot compact beyond current version")
            base = self.log.snapshot
            doc = Document.from_snapshot(base) if base else Document()
            for sop in self.log.ops_upto(upto_version):
                doc.apply(sop.op)
            snap = doc.snapshot(upto_version, self._watermarks)
            self.log.compact(snap)
            return snap


class SessionManager:
    """Creates and looks up board sessions (one per board)."""

    def __init__(self) -> None:
        self.acl = AccessControl()
        self._boards: dict[str, BoardSession] = {}
        self._lock = threading.Lock()

    def get_or_create(self, board_id: str) -> BoardSession:
        with self._lock:
            session = self._boards.get(board_id)
            if session is None:
                session = BoardSession(board_id, self.acl)
                self._boards[board_id] = session
            return session

    def get(self, board_id: str) -> BoardSession | None:
        return self._boards.get(board_id)
