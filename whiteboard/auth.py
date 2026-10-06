"""Per-(user, board) role-based access control.

Roles: owner > editor > viewer. Viewers are read-only: any write
attempt is rejected and recorded in an audit log so abuse is visible.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .models import Role


class AccessDenied(Exception):
    """Raised when a user attempts an action their role forbids."""

    def __init__(self, board_id: str, user_id: str, action: str) -> None:
        super().__init__(
            f"user {user_id!r} may not {action} on board {board_id!r}")
        self.board_id = board_id
        self.user_id = user_id
        self.action = action


@dataclass
class AuditEntry:
    board_id: str
    user_id: str
    action: str
    reason: str
    ts: float = field(default_factory=time.time)


class AccessControl:
    """Stores role grants and enforces read/write permissions."""

    def __init__(self) -> None:
        self._grants: dict[tuple[str, str], Role] = {}
        self.audit_log: list[AuditEntry] = []

    def grant(self, board_id: str, user_id: str, role: Role) -> None:
        self._grants[(board_id, user_id)] = role

    def revoke(self, board_id: str, user_id: str) -> None:
        self._grants.pop((board_id, user_id), None)

    def role_of(self, board_id: str, user_id: str) -> Role | None:
        return self._grants.get((board_id, user_id))

    # -- enforcement ---------------------------------------------------------

    def _require(self, board_id: str, user_id: str, action: str,
                 allowed: set[Role]) -> None:
        role = self.role_of(board_id, user_id)
        if role is None or role not in allowed:
            reason = "no grant" if role is None else f"role={role.value}"
            self.audit_log.append(AuditEntry(
                board_id=board_id, user_id=user_id, action=action,
                reason=reason))
            raise AccessDenied(board_id, user_id, action)

    def require_read(self, board_id: str, user_id: str) -> None:
        self._require(board_id, user_id, "read",
                      {Role.OWNER, Role.EDITOR, Role.VIEWER})

    def require_write(self, board_id: str, user_id: str) -> None:
        self._require(board_id, user_id, "write", {Role.OWNER, Role.EDITOR})

    def require_owner(self, board_id: str, user_id: str) -> None:
        self._require(board_id, user_id, "admin", {Role.OWNER})
