"""JSON <-> domain (de)serialization for ops, ids and views."""

from __future__ import annotations

from .models import CLIENT_TAG, REDO_TAG, UNDO_TAG, Op, OpId, OpType

_TAG_BY_NAME = {"client": CLIENT_TAG, "undo": UNDO_TAG, "redo": REDO_TAG}
_NAME_BY_TAG = {v: k for k, v in _TAG_BY_NAME.items()}


def op_id_to_json(op_id: OpId | None) -> dict | None:
    if op_id is None:
        return None
    return {"client_id": op_id.client_id, "seq": op_id.seq, "tag": _NAME_BY_TAG[op_id.tag]}


def op_id_from_json(data: dict | None) -> OpId | None:
    if data is None:
        return None
    tag = _TAG_BY_NAME.get(data.get("tag", "client"), CLIENT_TAG)
    return OpId(client_id=data["client_id"], seq=int(data["seq"]), tag=tag)


def op_to_json(op: Op) -> dict:
    data = {
        "id": op_id_to_json(op.id),
        "type": op.type.value,
        "author": op.author,
        "base_version": op.base_version,
        "version": None,  # filled by caller if desired
    }
    for name in ("shape_id", "annot_id", "z_index", "alive_value"):
        value = getattr(op, name)
        if value is not None:
            data[name] = value
    if op.props:
        data["props"] = op.props
    if op.removed_fields:
        data["removed_fields"] = op.removed_fields
    if op.target is not None:
        data["target"] = op_id_to_json(op.target)
    if op.anchor is not None:
        data["anchor"] = op_id_to_json(op.anchor)
    if op.guard is not None:
        data["guard"] = op_id_to_json(op.guard)
    if op.ops:
        data["ops"] = [op_to_json(c) for c in op.ops]
    return data


def op_from_json(data: dict) -> Op:
    children = [op_from_json(c) for c in data.get("ops", [])]
    return Op(
        id=op_id_from_json(data["id"]),
        type=OpType(data["type"]),
        author=data["author"],
        base_version=int(data.get("base_version", 0)),
        shape_id=data.get("shape_id"),
        annot_id=data.get("annot_id"),
        props=data.get("props"),
        z_index=data.get("z_index"),
        alive_value=data.get("alive_value"),
        removed_fields=list(data.get("removed_fields", [])),
        target=op_id_from_json(data.get("target")),
        anchor=op_id_from_json(data.get("anchor")),
        guard=op_id_from_json(data.get("guard")),
        ops=children,
    )
