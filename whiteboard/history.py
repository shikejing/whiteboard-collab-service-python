"""Operation history: append-only commit log and range queries.

The log stores committed ops in global commit order. Global version numbers
are dense and start at 1 (version == number of committed ops). After a
snapshot compaction the log's first entry corresponds to global version
``state.compacted_seq + 1``; queries at or below that boundary are answered
from the snapshot by the service layer.
"""

from __future__ import annotations

from .models import BoardState, Op


class HistoryLog:
    def append(self, state: BoardState, op: Op) -> int:
        state.log.append(op)
        state.version += 1
        return state.version

    def since(self, state: BoardState, version: int) -> list[Op]:
        """Ops with global version strictly greater than ``version``.

        Only entries still held in the live log are returned; callers must
        check :meth:`has_prefix` and fall back to snapshot state otherwise.
        """
        if version < state.compacted_seq:
            version = state.compacted_seq
        offset = state.compacted_seq
        return state.log[version - offset:]

    def has_prefix(self, state: BoardState, version: int) -> bool:
        """Whether all ops after ``version`` are still present in the log."""
        return version >= state.compacted_seq

    def iter_ops(self, state: BoardState) -> list[Op]:
        return list(state.log)
