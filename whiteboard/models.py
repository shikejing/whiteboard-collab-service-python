"""Core data types for the whiteboard collaboration service.

All types are plain dataclasses so they can be serialized to/from JSON
for the wire protocol and for persistence of the operation log.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class OpKind(str, enum.Enum):
    """Kinds of operations a client may submit."""

    ADD_SHAPE = "add_shape"
    UPDATE_SHAPE = "update_shape"
    REMOVE_SHAPE = "remove_shape"
    ADD_ANNOTATION = "add_annotation"
    UPDATE_ANNOTATION = "update_annotation"
    REMOVE_ANNOTATION = "remove_annotation"
    REORDER = "reorder"          # adjust z-order of a shape/annotation
    UNDO = "undo"
    REDO = "redo"


class Role(str, enum.Enum):
    """Access-control roles, per (user, board)."""

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"            # read-only


@dataclass(frozen=True)
class OpRef:
    """Uniquely identifies an operation submitted by a client.

    The pair (client_id, client_seq) is the idempotency key: a client
    numbers its operations with a local monotonic sequence, so a
    retransmitted operation can be detected and ignored.
    """

    client_id: str
    client_seq: int

    def key(self) -> str:
        return f"{self.client_id}:{self.client_seq}"


@dataclass
class Operation:
    """A single editing operation as submitted by a client.

    Attributes:
        ref: idempotency identity (client_id, client_seq).
        kind: what the operation does.
        board_id: target board.
        user_id: acting user (for ACL checks and undo scoping).
        base_version: the board version the client based this op on
            (last server version the client had seen).
        target_id: id of the shape/annotation the op applies to
            (for ADD_* this is the id of the new element).
        payload: kind-specific data (geometry, style, text, z-index...).
    """

    ref: OpRef
    kind: OpKind
    board_id: str
    user_id: str
    base_version: int
    target_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SequencedOp:
    """An operation after the server assigned it a global sequence number.

    `server_seq` is the position in the board's totally ordered,
    authoritative operation log. `version` equals `server_seq` and is
    what clients use as `base_version` for subsequent operations.
    """

    op: Operation
    server_seq: int
    # Inverse effect, computed by the session at sequencing time and
    # stored in the log so undo stays valid after restarts/compaction.
    inverse: Optional[dict[str, Any]] = None

    @property
    def version(self) -> int:
        return self.server_seq


@dataclass
class Shape:
    """A drawable element or annotation on the board."""

    id: str
    kind: str                      # "shape" | "annotation"
    attrs: dict[str, Any] = field(default_factory=dict)
    z: int = 0                     # z-order; higher renders on top
    deleted: bool = False          # tombstone so reorder/undo stay stable


@dataclass
class Snapshot:
    """A compacted, self-contained view of the board at `version`."""

    version: int
    shapes: dict[str, Shape] = field(default_factory=dict)
    # (client_id -> last applied client_seq) so dedup survives compaction
    client_watermarks: dict[str, int] = field(default_factory=dict)


@dataclass
class SubmitResult:
    """Outcome of submitting one operation to a board."""

    accepted: bool
    server_seq: Optional[int] = None
    duplicate: bool = False
    error: Optional[str] = None
