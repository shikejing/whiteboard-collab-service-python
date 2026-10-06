"""Operation log with snapshot compaction.

Keeps the authoritative, totally ordered history of a board. Old
operations can be compacted into a snapshot; clients that were offline
past the compaction point are served the snapshot plus the remaining
tail of the log, which yields exactly the same final state as replaying
the full history.
"""
from __future__ import annotations

from .models import SequencedOp, Snapshot


class HistoryCompactedError(Exception):
    """Requested version is older than what the log still holds."""


class OpLog:
    """Append-only log of sequenced operations plus periodic snapshots.

    `server_seq` numbers are global and never reused: compaction only
    drops old entries, it does not renumber. The first retained op after
    compaction at version V has server_seq V+1.
    """

    def __init__(self) -> None:
        self._ops: list[SequencedOp] = []   # only ops after the snapshot
        self._snapshot: Snapshot | None = None

    @property
    def version(self) -> int:
        """Current board version (server_seq of the last applied op)."""
        if self._ops:
            return self._ops[-1].server_seq
        return self._snapshot.version if self._snapshot else 0

    @property
    def base_version(self) -> int:
        """Oldest version from which incremental sync is still possible."""
        return self._snapshot.version if self._snapshot else 0

    @property
    def snapshot(self) -> Snapshot | None:
        return self._snapshot

    def append(self, op: SequencedOp) -> None:
        expected = self.version + 1
        if op.server_seq != expected:
            raise ValueError(
                f"non-contiguous seq: got {op.server_seq}, expected {expected}")
        self._ops.append(op)

    def ops_since(self, version: int) -> list[SequencedOp]:
        """Return ops with server_seq > version.

        Raises HistoryCompactedError if `version` is older than the
        compaction point (caller must fall back to a snapshot).
        """
        if version < self.base_version:
            raise HistoryCompactedError(
                f"version {version} compacted away (base={self.base_version})")
        start = version - self.base_version
        return list(self._ops[start:])

    def compact(self, snapshot: Snapshot) -> None:
        """Discard all ops covered by `snapshot` (server_seq <= version)."""
        if self._snapshot is not None and snapshot.version <= self._snapshot.version:
            raise ValueError("snapshot does not advance the compaction point")
        if snapshot.version > self.version:
            raise ValueError("cannot compact beyond the current version")
        drop = snapshot.version - self.base_version
        self._ops = self._ops[drop:]
        self._snapshot = snapshot

    def find(self, ref_key: str) -> SequencedOp | None:
        """Find a retained op by its idempotency key, or None if it was
        compacted away (undo history is bounded by the compaction point)."""
        for sop in self._ops:
            if sop.op.ref.key() == ref_key:
                return sop
        return None

    def ops_upto(self, version: int) -> list[SequencedOp]:
        """Retained ops with server_seq <= version (for snapshot rebuilds)."""
        return [o for o in self._ops if o.server_seq <= version]

    def sync_for(self, version: int) -> tuple[Snapshot | None, list[SequencedOp]]:
        """Incremental sync payload for a client at `version`.

        Returns (snapshot_or_None, ops). If the client's version is older
        than the compaction point, the current snapshot is included so
        the client can reset and then apply the tail — the result is
        equivalent to replaying the full history.
        """
        if version >= self.base_version:
            return None, self.ops_since(version)
        return self._snapshot, list(self._ops)
