"""Per-board session / connection management.

A board session groups all live connections of one board. A single logical
client may hold several physical connections (reconnect race, multi-tab);
they share a ``client_id``. Disconnecting or reconnecting one client only
flips that connection's flag -- board state, other clients and their
connections are untouched.

The tracker is pure bookkeeping: it does not mutate board content.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field


@dataclass
class Connection:
    conn_id: str
    client_id: str
    user_id: str
    board_id: str
    connected: bool = True
    last_version: int = 0


@dataclass
class _BoardSessions:
    connections: dict[str, Connection] = field(default_factory=dict)
    # client_id -> set of conn ids (a client can reconnect in parallel)
    client_conns: dict[str, set[str]] = field(default_factory=dict)


class SessionManager:
    def __init__(self) -> None:
        self._boards: dict[str, _BoardSessions] = {}
        self._ids = itertools.count(1)

    def _board(self, board_id: str) -> _BoardSessions:
        bs = self._boards.get(board_id)
        if bs is None:
            bs = _BoardSessions()
            self._boards[board_id] = bs
        return bs

    def connect(
        self, board_id: str, client_id: str, user_id: str, last_version: int = 0
    ) -> Connection:
        bs = self._board(board_id)
        conn_id = f"conn-{next(self._ids)}"
        conn = Connection(
            conn_id=conn_id,
            client_id=client_id,
            user_id=user_id,
            board_id=board_id,
            last_version=last_version,
        )
        bs.connections[conn_id] = conn
        bs.client_conns.setdefault(client_id, set()).add(conn_id)
        return conn

    def disconnect(self, conn_id: str) -> None:
        for bs in self._boards.values():
            conn = bs.connections.get(conn_id)
            if conn is not None:
                conn.connected = False
                conns = bs.client_conns.get(conn.client_id)
                if conns is not None:
                    conns.discard(conn_id)
                    if not conns:
                        bs.client_conns.pop(conn.client_id, None)
                return

    def disconnect_client(self, board_id: str, client_id: str) -> None:
        bs = self._boards.get(board_id)
        if bs is None:
            return
        for conn_id in list(bs.client_conns.get(client_id, set())):
            conn = bs.connections.get(conn_id)
            if conn is not None:
                conn.connected = False
        bs.client_conns.pop(client_id, None)

    def advance(self, conn_id: str, version: int) -> None:
        for bs in self._boards.values():
            conn = bs.connections.get(conn_id)
            if conn is not None:
                conn.last_version = max(conn.last_version, version)
                return

    def connections_of(self, board_id: str) -> list[Connection]:
        bs = self._boards.get(board_id)
        if bs is None:
            return []
        return [c for c in bs.connections.values() if c.connected]

    def clients_of(self, board_id: str) -> set[str]:
        bs = self._boards.get(board_id)
        if bs is None:
            return set()
        return set(bs.client_conns.keys())
