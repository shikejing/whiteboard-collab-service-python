"""Per-board / per-user access control with a denial audit trail.

Roles: ``owner`` / ``editor`` may mutate; ``viewer`` is strictly read-only.
Unknown users have no access at all. Every decision (allow and deny) is
appended to an in-memory audit trail; denials raise :class:`PermissionDenied`
with an explanatory reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import OpType, Role

WRITE_OPS = {
    OpType.ADD_SHAPE,
    OpType.UPDATE_SHAPE,
    OpType.DELETE_SHAPE,
    OpType.ZMOVE_SHAPE,
    OpType.ADD_ANNOT,
    OpType.UPDATE_ANNOT,
    OpType.DELETE_ANNOT,
    OpType.BATCH,
}

READ_ROLE_OK = {Role.OWNER, Role.EDITOR, Role.VIEWER}
WRITE_ROLE_OK = {Role.OWNER, Role.EDITOR}


@dataclass
class AuditEntry:
    board_id: str
    user_id: str
    action: str
    allowed: bool
    reason: str


class PermissionDenied(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AccessControl:
    def __init__(self) -> None:
        self._roles: dict[tuple[str, str], Role] = {}
        self.audit: list[AuditEntry] = []

    # ------------------------------------------------------------- provision

    def grant(self, board_id: str, user_id: str, role: Role) -> None:
        self._roles[(board_id, user_id)] = role

    def revoke(self, board_id: str, user_id: str) -> None:
        self._roles.pop((board_id, user_id), None)

    def role_of(self, board_id: str, user_id: str) -> Role | None:
        return self._roles.get((board_id, user_id))

    # --------------------------------------------------------------- checks

    def _record(self, board_id: str, user_id: str, action: str, allowed: bool, reason: str) -> None:
        self.audit.append(
            AuditEntry(board_id=board_id, user_id=user_id, action=action, allowed=allowed, reason=reason)
        )

    def check_read(self, board_id: str, user_id: str) -> Role:
        role = self.role_of(board_id, user_id)
        if role is None:
            self._record(board_id, user_id, "read", False, "user has no access to board")
            raise PermissionDenied("user has no access to board")
        self._record(board_id, user_id, "read", True, f"role={role.value}")
        return role

    def check_write(self, board_id: str, user_id: str, op_type: OpType) -> Role:
        role = self.role_of(board_id, user_id)
        if role is None:
            self._record(board_id, user_id, op_type.value, False, "user has no access to board")
            raise PermissionDenied("user has no access to board")
        if op_type not in WRITE_OPS:
            self._record(board_id, user_id, op_type.value, False, f"non-mutating op check misuse: {op_type.value}")
            raise PermissionDenied(f"op {op_type.value} is not a write op")
        if role not in WRITE_ROLE_OK:
            reason = f"read-only role {role.value} cannot {op_type.value}"
            self._record(board_id, user_id, op_type.value, False, reason)
            raise PermissionDenied(reason)
        self._record(board_id, user_id, op_type.value, True, f"role={role.value}")
        return role

    def check_undo(self, board_id: str, user_id: str) -> None:
        role = self.role_of(board_id, user_id)
        if role is None:
            self._record(board_id, user_id, "undo", False, "user has no access to board")
            raise PermissionDenied("user has no access to board")
        if role not in WRITE_ROLE_OK:
            reason = f"read-only role {role.value} cannot undo"
            self._record(board_id, user_id, "undo", False, reason)
            raise PermissionDenied(reason)
        self._record(board_id, user_id, "undo", True, f"role={role.value}")

    # ---------------------------------------------------------------- audit

    def audit_trail(self, board_id: str | None = None) -> list[AuditEntry]:
        if board_id is None:
            return list(self.audit)
        return [e for e in self.audit if e.board_id == board_id]
