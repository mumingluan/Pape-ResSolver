from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def _line_count(path: Path) -> int:
    count = 0
    last = b""
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            count += chunk.count(b"\n")
            last = chunk[-1:]
    return count + (1 if path.stat().st_size and last != b"\n" else 0)


def verify_output(output: Path, write_report: bool = True) -> dict[str, Any]:
    output = output.resolve()
    catalog = json.loads((output / "catalog.json").read_text(encoding="utf-8"))
    failures: list[str] = []
    actual_rows = 0
    for table in catalog.get("tables", []):
        path = output / table["jsonl_path"]
        if not path.is_file():
            failures.append(f"missing table output: {table['jsonl_path']}")
            continue
        lines = _line_count(path)
        actual_rows += lines
        if lines != table["row_count"]:
            failures.append(f"row count mismatch {table['table']}: catalog={table['row_count']} file={lines}")
        schema_path = table.get("schema_path")
        if schema_path and not (output / schema_path).is_file():
            failures.append(f"missing schema output: {schema_path}")

    database = sqlite3.connect(output / "resources.sqlite")
    try:
        integrity = database.execute("pragma integrity_check").fetchone()[0]
        counts = {
            table: database.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "config_tables",
                "config_rows",
                "config_references",
                "lua_scripts",
                "lua_dependencies",
                "resource_files",
                "resource_packages",
                "decoded_table_rows",
            )
        }
    finally:
        database.close()
    if integrity != "ok":
        failures.append(f"SQLite integrity check: {integrity}")
    if counts["config_tables"] != catalog["totals"]["succeeded"]:
        failures.append("SQLite config table count does not match catalog")
    if counts["config_rows"] != catalog["totals"]["rows"] or actual_rows != catalog["totals"]["rows"]:
        failures.append("normalized config row totals do not agree")

    parse_failures = json.loads((output / "reports" / "parse_failures.json").read_text(encoding="utf-8"))
    references = json.loads((output / "reports" / "references.json").read_text(encoding="utf-8"))
    lua_index = json.loads((output / "reports" / "lua_index.json").read_text(encoding="utf-8"))
    artifacts = json.loads((output / "reports" / "artifacts.json").read_text(encoding="utf-8"))
    if parse_failures:
        failures.append(f"configuration parse failures: {len(parse_failures)}")
    if references["totals"]["broken"]:
        failures.append(f"broken known references: {references['totals']['broken']}")
    if lua_index["totals"]["all_entries"] != (lua_index["totals"]["config_entries"] + counts["lua_scripts"]):
        failures.append("Lua config + logic entry counts do not cover the manifest")
    if artifacts["consolidated"]["parse_failures"]:
        failures.append("decoded table consolidation has parse failures")
    if (
        artifacts["consolidated"]["msgpack_rows"] != 53728
        and catalog["resource"].get("source_scaffold") == "my\\scaffold\\1.7.1546"
    ):
        failures.append("current fixture MessagePack row count changed unexpectedly")

    report = {
        "schema": "pape-res-verification-v1",
        "valid": not failures,
        "failures": failures,
        "catalog_tables": len(catalog.get("tables", [])),
        "catalog_rows": catalog["totals"]["rows"],
        "file_rows": actual_rows,
        "sqlite_integrity": integrity,
        "sqlite_counts": counts,
        "known_references": references["totals"],
        "lua_entries": lua_index["totals"],
        "artifact_totals": artifacts["totals"],
        "consolidated": artifacts["consolidated"],
    }
    if write_report:
        path = output / "reports" / "verification.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
