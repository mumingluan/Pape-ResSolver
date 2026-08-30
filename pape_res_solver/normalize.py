from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .lua_static import LuaCall, LuaExpr, LuaRef, LuaTable


@dataclass(slots=True)
class NormalizedTable:
    rows: list[tuple[str, dict[str, Any] | Any]]
    schema: dict[str, Any] | None
    schema_fingerprint: str | None
    unresolved_values: int
    raw_keys: int


def _sorted_keys(keys: Iterable[Any]) -> list[Any]:
    return sorted(keys, key=lambda value: (type(value).__name__, str(value)))


def to_jsonable(value: Any, stack: set[int] | None = None) -> Any:
    stack = set() if stack is None else stack
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"$float": str(value)}
    if isinstance(value, LuaRef):
        return {"$ref": value.name}
    if isinstance(value, LuaCall):
        return {
            "$call": to_jsonable(value.function, stack),
            "args": [to_jsonable(item, stack) for item in value.arguments],
        }
    if isinstance(value, LuaExpr):
        return {
            "$expr": value.operator,
            "operands": [to_jsonable(item, stack) for item in value.operands],
        }
    if isinstance(value, LuaTable):
        identity = id(value)
        if identity in stack:
            return {"$cycle": True}
        stack.add(identity)
        keys = set(value.values)
        integer_keys = {key for key in keys if isinstance(key, int) and key > 0}
        other_keys = keys - integer_keys
        if not other_keys and (integer_keys or value.array_extent):
            extent = max(value.array_extent, max(integer_keys, default=0))
            result: Any = [to_jsonable(value.get(index), stack) for index in range(1, extent + 1)]
        elif not keys:
            result = [] if value.array_extent else {}
        else:
            result = {str(key): to_jsonable(value.values[key], stack) for key in _sorted_keys(keys)}
        stack.remove(identity)
        return result
    if isinstance(value, dict):
        return {str(key): to_jsonable(child, stack) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(child, stack) for child in value]
    return {"$python": repr(value)}


def _schema_json(schema: LuaTable) -> dict[str, Any]:
    return {str(key): to_jsonable(schema.values[key]) for key in sorted(schema.values, key=str)}


def _schema_position(spec: Any) -> int | None:
    if isinstance(spec, int):
        return spec
    if isinstance(spec, LuaTable) and isinstance(spec.get(1), int):
        return spec.get(1)
    return None


def _decode_nested(value: Any, schema: LuaTable) -> Any:
    if not isinstance(value, LuaTable):
        return to_jsonable(value)
    # Runtime-produced grouped tables can report array_extent=0 or use sparse,
    # very large integer keys. Iterate actual keys rather than allocating a
    # range up to the largest ID.
    integer_keys = sorted(key for key in value.values if isinstance(key, int) and key > 0)
    non_nil = [value.get(key) for key in integer_keys if value.get(key) is not None]
    if non_nil and all(isinstance(item, LuaTable) for item in non_nil):
        return [_decode_row(item, schema) for item in non_nil]
    return _decode_row(value, schema)


def _decode_field(row: LuaTable, spec: Any) -> Any:
    if isinstance(spec, int):
        return to_jsonable(row.get(spec))
    if isinstance(spec, LuaTable):
        position = spec.get(1)
        nested_schema = spec.get(2)
        if not isinstance(position, int):
            return to_jsonable(spec)
        value = row.get(position)
        if isinstance(nested_schema, LuaTable):
            return _decode_nested(value, nested_schema)
        return to_jsonable(value)
    return to_jsonable(spec)


def _decode_row(row: LuaTable, schema: LuaTable) -> dict[str, Any]:
    fields = sorted(
        schema.values.items(), key=lambda item: (_schema_position(item[1]) or 1 << 30, str(item[0]))
    )
    return {
        str(field): _decode_field(row, spec) for field, spec in fields if _schema_position(spec) is not None
    }


def count_unresolved(value: Any) -> int:
    if isinstance(value, (LuaRef, LuaExpr, LuaCall)):
        return 1
    if isinstance(value, LuaTable):
        return sum(count_unresolved(key) + count_unresolved(child) for key, child in value.values.items())
    if isinstance(value, (list, tuple)):
        return sum(count_unresolved(child) for child in value)
    if isinstance(value, dict):
        return sum(count_unresolved(child) for child in value.values())
    return 0


def normalize_config_table(value: Any) -> NormalizedTable:
    if not isinstance(value, LuaTable):
        return NormalizedTable(
            rows=[("value", to_jsonable(value))],
            schema=None,
            schema_fingerprint=None,
            unresolved_values=count_unresolved(value),
            raw_keys=0,
        )
    schema = value.get("_k")
    schema_json = _schema_json(schema) if isinstance(schema, LuaTable) else None
    fingerprint = None
    if schema_json is not None:
        canonical = json.dumps(schema_json, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    rows: list[tuple[str, dict[str, Any] | Any]] = []
    for key in _sorted_keys(key for key in value.values if key != "_k"):
        row = value.get(key)
        normalized = (
            # Some LuaCfg tables group multiple schema-shaped rows under one
            # primary key (for example, month -> daily sign rows).  Decode the
            # value through the same nested-row discriminator used by nested
            # schema fields so dense and sparse row arrays are preserved.
            _decode_nested(row, schema)
            if isinstance(row, LuaTable) and isinstance(schema, LuaTable)
            else to_jsonable(row)
        )
        rows.append((str(key), normalized))
    return NormalizedTable(
        rows=rows,
        schema=schema_json,
        schema_fingerprint=fingerprint,
        unresolved_values=count_unresolved(value),
        raw_keys=len(value.values) - (1 if "_k" in value.values else 0),
    )


_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_table_filename(name: str) -> str:
    cleaned = _UNSAFE_FILENAME.sub("_", name).strip("._")
    return cleaned or "unnamed"
