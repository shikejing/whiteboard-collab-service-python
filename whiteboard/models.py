"""Domain model: operations, shapes, snapshots, roles and results.

Identity model
---------------
Every edit is identified by an ``OpId`` which is a fully ordered, globally
deterministic key:

* client originated operations: ``(client_id, client_seq, 0)``
* server originated undo inverse:  ``(client_id, undo_counter, 1)``
* server originated redo:          ``(client_id, undo_counter, 2)``

The key properties:

* ``(client_id, client_seq)`` dedupes retries / duplicate reports.
* The total order is used for last-writer-wins tie breaking, so the same
  batch of operations converges to the same content regardless of arrival
  order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

CLIENT_TAG = 0
UNDO_TAG = 1
REDO_TAG = 2


class OpType(str, Enum):
    ADD_SHAPE = "add_shape"
    UPDATE_SHAPE = "update_shape"
    DELETE_SHAPE = "delete_shape"
    ZMOVE_SHAPE = "zmove_shape"
    ADD_ANNOT = "add_annot"
    UPDATE_ANNOT = "update_annot"
    DELETE_ANNOT = "delete_annot"
    BATCH = "batch"  # client batches several ops under one dedup id


@dataclass(frozen=True, order=True)
class OpId:
    """Deterministic, total-ordered operation identifier."""

    client_id: str
    seq: int
    tag: int = CLIENT_TAG

    def key(self) -> tuple[str, int, int]:
        return (self.client_id, self.seq, self.tag)

    def is_client_op(self) -> bool:
        return self.tag == CLIENT_TAG


@dataclass
class Op:
    """A single client or server generated operation."""

    id: OpId
    type: OpType
    author: str
    base_version: int
    # generic payloads (interpreted by the engine)
    shape_id: str | None = None
    props: dict | None = None
    # layering: zmove semantics
    z_index: int | None = None
    # annotations are just a special shape kind; kept explicit for clarity
    annot_id: str | None = None
    # undo/redo bookkeeping
    target: OpId | None = None
    anchor: OpId | None = None  # inverse ops: original client op they revert
    guard: OpId | None = None   # inverse ops: apply only if owner == guard
    alive_value: bool | None = None  # inverse liveness write
    removed_fields: list[str] = field(default_factory=list)
    ops: list["Op"] = field(default_factory=list)
    wall_ts: float = 0.0


@dataclass
class FieldOwner:
    """LWW owner of a single entity field: (winning op id, value).

    ``anchor`` is set for server generated inverse ops: it points at the
    original client op the chain belongs to, so later regular writes can be
    compared against the anchor instead of the (always-later) inverse id.
    """

    op_id: OpId
    value: object
    anchor: OpId | None = None
    prev: "FieldOwner | None" = None  # overwritten owner chain (for undo)


@dataclass
class ShapeRecord:
    """Current entity state with per-field LWW metadata."""

    shape_id: str
    kind: str  # "shape" or "annot"
    fields: dict[str, FieldOwner] = field(default_factory=dict)
    # birth op (lowest add op id)
    born_op: OpId | None = None
    alive: FieldOwner | None = None  # owner of liveness: None=alive, op=tombstoned
    z: FieldOwner | None = None


@dataclass
class UndoUnit:
    """One undoable client action (a single op or a client batch)."""

    unit_id: OpId  # the client op / batch head
    ops: list[Op]  # concrete ops belonging to the unit, in order
    inverses: list[Op] = field(default_factory=list)  # last undo inverse chain
    undone: bool = False


@dataclass
class UserState:
    """Per user undo/redo bookkeeping."""

    undo_stack: list[UndoUnit] = field(default_factory=list)
    redo_stack: list[UndoUnit] = field(default_factory=list)
    undo_counter: int = 0


@dataclass
class BoardState:
    """Mutable state of one board."""

    board_id: str
    version: int = 0
    shapes: dict[str, ShapeRecord] = field(default_factory=dict)
    # global commit order (list of committed Op in commit sequence)
    log: list[Op] = field(default_factory=list)
    # dedup index: (client_id, client_seq) -> committed Op
    seen: dict[tuple[str, int], Op] = field(default_factory=dict)
    users: dict[str, UserState] = field(default_factory=dict)
    # compacted prefix: log entries with seq <= compacted_seq are folded
    compacted_seq: int = 0
    snapshot: "Snapshot | None" = None


@dataclass
class Snapshot:
    """Compacted representation replacing a prefix of the op log."""

    board_id: str
    seq: int  # global seq through which the snapshot folds operations
    version: int
    shapes: dict[str, ShapeRecord] = field(default_factory=dict)
    seen: dict[tuple[str, int], Op] = field(default_factory=dict)
    users: dict[str, UserState] = field(default_factory=dict)


class Role(str, Enum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


@dataclass
class CommitResult:
    accepted: bool
    version: int
    op: Op | None = None
    duplicate: bool = False
    reason: str = ""


@dataclass
class DeltaSync:
    """Result of a reconnect sync."""

    board_id: str
    from_version: int
    version: int
    ops: list[Op] = field(default_factory=list)
    full_state: dict | None = None  # set when client is before/across snapshot
    already_acked: set[int] = field(default_factory=set)
