"""WhiteboardService: facade wiring every subsystem behind one board lock.

Threading model: all mutating/read operations on a board are serialized by a
per-board lock so concurrent HTTP/WS connections cannot interleave inside a
commit. The engine itself is deterministic; the lock only guards the
in-memory state transition.

Reconnect sync
--------------
Given a client's ``last_version``:

* if it is at/after the compaction boundary, only the missing log tail is
  returned (true incremental sync);
* if it is before the boundary (or asks with 0), the snapshot content plus
  the tail is returned as a full-state delta, guaranteeing an equivalent
  complete picture;
* ops the reconnecting client resends are caught by the dedup index and
  acknowledged as duplicates, never applied twice.
"""

from __future__ import annotations

import threading

from .engine import BoardEngine
from .history import HistoryLog
from .models import (
    BoardState,
    CommitResult,
    DeltaSync,
    Op,
    OpType,
    Role,
)
from .permissions import AccessControl, PermissionDenied
from .sessions import Connection, SessionManager
from .snapshots import SnapshotManager
from .undo import UndoManager


class WhiteboardService:
    def __init__(self) -> None:
        self.engine = BoardEngine()
        self.history = HistoryLog()
        self.snapshots = SnapshotManager(self.engine)
        self.sessions = SessionManager()
        self.acl = AccessControl()
        self.undo_manager = UndoManager(self.engine)
        self._boards: dict[str, BoardState] = {}
        self._locks: dict[str, threading.RLock] = {}

    # ---------------------------------------------------------------- boards

    def create_board(self, board_id: str, owner_id: str) -> BoardState:
        if board_id in self._boards:
            return self._boards[board_id]
        state = self.engine.init_board(board_id)
        self._boards[board_id] = state
        self._locks[board_id] = threading.RLock()
        self.acl.grant(board_id, owner_id, Role.OWNER)
        return state

    def _state(self, board_id: str) -> BoardState:
        state = self._boards.get(board_id)
        if state is None:
            raise KeyError(f"unknown board: {board_id}")
        return state

    def grant(self, board_id: str, user_id: str, role: Role) -> None:
        self.acl.grant(board_id, user_id, role)

    # ------------------------------------------------------------- sessions

    def connect(
        self, board_id: str, client_id: str, user_id: str, last_version: int
    ) -> tuple[Connection, DeltaSync]:
        state = self._state(board_id)
        with self._locks[board_id]:
            self.acl.check_read(board_id, user_id)
            conn = self.sessions.connect(board_id, client_id, user_id, last_version)
            sync = self.sync(board_id, user_id, last_version)
        return conn, sync

    def disconnect(self, conn_id: str) -> None:
        self.sessions.disconnect(conn_id)

    def sync(self, board_id: str, user_id: str, last_version: int) -> DeltaSync:
        state = self._state(board_id)
        with self._locks[board_id]:
            self.acl.check_read(board_id, user_id)
            last_version = max(0, min(last_version, state.version))
            if self.history.has_prefix(state, last_version):
                return DeltaSync(
                    board_id=board_id,
                    from_version=last_version,
                    version=state.version,
                    ops=self.history.since(state, last_version),
                )
            # across/before compaction boundary: snapshot + tail
            snapshot = state.snapshot
            full = (
                self.snapshots.snapshot_view(snapshot)
                if snapshot is not None
                else self.engine.render(state)
            )
            tail = self.history.since(state, state.compacted_seq)
            return DeltaSync(
                board_id=board_id,
                from_version=last_version,
                version=state.version,
                ops=tail,
                full_state=full,
            )

    # ---------------------------------------------------------------- commit

    def submit(self, board_id: str, op: Op) -> CommitResult:
        state = self._state(board_id)
        with self._locks[board_id]:
            # Permission is decided against every concrete write of a batch.
            concrete = op.ops if op.type is OpType.BATCH else [op]
            for child in concrete:
                self.acl.check_write(board_id, op.author, child.type)
            result = self.engine.commit(state, op)
            if not result.duplicate and op.id.is_client_op():
                self.undo_manager.record_client_op(state, op)
            return result

    def undo(self, board_id: str, user_id: str) -> CommitResult:
        state = self._state(board_id)
        with self._locks[board_id]:
            self.acl.check_undo(board_id, user_id)
            return self.undo_manager.undo(state, user_id)

    def redo(self, board_id: str, user_id: str) -> CommitResult:
        state = self._state(board_id)
        with self._locks[board_id]:
            self.acl.check_undo(board_id, user_id)
            return self.undo_manager.redo(state, user_id)

    # -------------------------------------------------------------- snapshot

    def compact(self, board_id: str, through_version: int | None = None):
        state = self._state(board_id)
        with self._locks[board_id]:
            cut = state.version if through_version is None else through_version
            return self.snapshots.compact(state, cut)

    # ----------------------------------------------------------------- views

    def render(self, board_id: str, user_id: str) -> dict:
        state = self._state(board_id)
        with self._locks[board_id]:
            self.acl.check_read(board_id, user_id)
            return self.engine.render(state)
