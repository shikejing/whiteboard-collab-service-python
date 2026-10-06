"""History compaction: fold old operations into snapshots.

A snapshot freezes, at global version ``through_version``:

* every entity record (per-field LWW owners + history chains + tombstones),
* the ``(client_id, client_seq)`` dedup index -- this is what lets a
  reconnecting client safely resend ops from before compaction (they are
  still recognized as duplicates),
* every user's undo/redo stacks (undo targets older than the snapshot stay
  valid because registers keep their prev chains).

The snapshot is produced by **replaying the log prefix** onto an empty board,
so its content exactly equals the state at that version -- never newer.

Compaction only truncates the in-memory log prefix; a reconnecting client
whose last version is at or before ``snapshot.seq`` receives the full
snapshot content plus every op after it, so compaction never breaks offline
incremental sync.
"""

from __future__ import annotations

import copy

from .engine import BoardEngine
from .models import BoardState, Snapshot


class SnapshotManager:
    def __init__(self, engine: BoardEngine | None = None) -> None:
        self.engine = engine or BoardEngine()

    def _render_entities(self, shapes: dict) -> list[dict]:
        entities: list[dict] = []
        for shape_id, rec in shapes.items():
            if rec.born_op is None:
                continue
            if rec.alive is not None and rec.alive.value is False:
                continue
            entities.append(
                {
                    "id": shape_id,
                    "kind": rec.kind,
                    "z": rec.z.value if rec.z is not None else 0,
                    "props": {n: o.value for n, o in sorted(rec.fields.items())},
                }
            )
        entities.sort(
            key=lambda e: (e["z"], shapes[e["id"]].born_op.key(), e["id"])
        )
        return entities

    def compact(self, state: BoardState, through_version: int) -> Snapshot:
        through_version = max(0, min(through_version, state.version))
        offset = state.compacted_seq
        prefix_ops = state.log[: through_version - offset]

        # replay prefix on a pristine board: applies ops + records seen
        replay = self.engine.init_board(state.board_id)
        replay.compacted_seq = offset
        for op in prefix_ops:
            self.engine.commit(replay, op)
        # undo/redo stacks are user metadata, carried over as of the cut
        replay.users = copy.deepcopy(state.users)

        snapshot = Snapshot(
            board_id=state.board_id,
            seq=through_version,
            version=through_version,
            shapes=replay.shapes,
            seen=replay.seen,
            users=replay.users,
        )
        drop = through_version - state.compacted_seq
        if drop > 0:
            del state.log[:drop]
        state.compacted_seq = through_version
        state.snapshot = snapshot
        # fold folded history: current state must keep snapshot-derived
        # dedup index and liveness history even though the prefix is gone
        state.seen = dict(state.seen)
        return snapshot

    def snapshot_view(self, snapshot: Snapshot) -> dict:
        return {
            "board_id": snapshot.board_id,
            "version": snapshot.seq,
            "entities": self._render_entities(snapshot.shapes),
        }

    def restore_view(self, state: BoardState, snapshot: Snapshot) -> dict:
        return self.snapshot_view(snapshot)
