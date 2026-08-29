from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class ReferenceResult:
    source_table: str
    source_key: str
    field: str
    target_table: str
    target_key: str
    valid: bool


def _rows(database: sqlite3.Connection, table: str) -> Iterable[tuple[str, dict[str, Any]]]:
    for key, raw in database.execute(
        "select row_key, data_json from config_rows where table_name = ? order by row_key", (table,)
    ):
        value = json.loads(raw)
        if isinstance(value, dict):
            yield str(key), value


def _keys(database: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[0])
        for row in database.execute("select row_key from config_rows where table_name = ?", (table,))
    }


def _add(
    output: list[ReferenceResult],
    source_table: str,
    source_key: str,
    field: str,
    target_table: str,
    target_key: Any,
    target_keys: set[str],
) -> None:
    if target_key in (None, 0, ""):
        return
    key = str(target_key)
    output.append(
        ReferenceResult(
            source_table=source_table,
            source_key=source_key,
            field=field,
            target_table=target_table,
            target_key=key,
            valid=key in target_keys,
        )
    )


def validate_references(database: sqlite3.Connection) -> dict[str, Any]:
    card_keys = _keys(database, "CardBaseInfo")
    item_keys = _keys(database, "Item")
    gacha_group_keys = _keys(database, "GachaGroup")
    gacha_rule_keys = _keys(database, "GachaRule")
    gacha_drop_groups: set[str] = set()
    for _, row in _rows(database, "GachaDrop"):
        if row.get("GroupID") not in (None, 0):
            gacha_drop_groups.add(str(row["GroupID"]))

    results: list[ReferenceResult] = []
    for key, row in _rows(database, "CardBaseInfo"):
        _add(results, "CardBaseInfo", key, "FragmentID", "Item", row.get("FragmentID"), item_keys)
    for key, row in _rows(database, "Item"):
        if row.get("Type") == 203:
            _add(results, "Item", key, "ConnectID", "CardBaseInfo", row.get("ConnectID"), card_keys)
    for key, row in _rows(database, "GachaAll"):
        _add(results, "GachaAll", key, "GachaGroup", "GachaGroup", row.get("GachaGroup"), gacha_group_keys)
        _add(results, "GachaAll", key, "CostTicket", "Item", row.get("CostTicket"), item_keys)
        _add(results, "GachaAll", key, "CostBase", "Item", row.get("CostBase"), item_keys)
        for index, rule in enumerate(row.get("Rule") or []):
            _add(results, "GachaAll", key, f"Rule[{index}]", "GachaRule", rule, gacha_rule_keys)
    for key, row in _rows(database, "GachaRule"):
        for index, drop in enumerate(row.get("Drop") or []):
            if isinstance(drop, dict):
                _add(
                    results,
                    "GachaRule",
                    key,
                    f"Drop[{index}].ID",
                    "GachaDrop.GroupID",
                    drop.get("ID"),
                    gacha_drop_groups,
                )
    for key, row in _rows(database, "GachaDrop"):
        for index, item in enumerate(row.get("ItemID") or []):
            if not isinstance(item, dict):
                continue
            item_type = item.get("Type")
            target_table = "CardBaseInfo" if item_type == 51 else "Item"
            target_keys = card_keys if item_type == 51 else item_keys
            _add(results, "GachaDrop", key, f"ItemID[{index}].ID", target_table, item.get("ID"), target_keys)

    database.execute("delete from config_references")
    database.executemany(
        """insert into config_references(
            source_table, source_key, field, target_table, target_key, valid
        ) values (?, ?, ?, ?, ?, ?)""",
        [
            (
                item.source_table,
                item.source_key,
                item.field,
                item.target_table,
                item.target_key,
                1 if item.valid else 0,
            )
            for item in results
        ],
    )
    broken = [asdict(item) for item in results if not item.valid]
    by_relation: dict[str, dict[str, int]] = {}
    for item in results:
        relation = f"{item.source_table}.{item.field.split('[')[0]}->{item.target_table}"
        counts = by_relation.setdefault(relation, {"total": 0, "valid": 0, "broken": 0})
        counts["total"] += 1
        counts["valid" if item.valid else "broken"] += 1
    return {
        "schema": "pape-res-reference-report-v1",
        "totals": {
            "references": len(results),
            "valid": sum(1 for item in results if item.valid),
            "broken": len(broken),
        },
        "relations": by_relation,
        "broken": broken,
    }
