from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .pipeline import ExtractionPipeline
from .sqlite_trim import trim_sqlite
from .verify import verify_output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pape-res", description="Normalize Pape resource dumps")
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract", help="extract LuaCfg tables to JSONL and SQLite")
    extract.add_argument("resources", type=Path, help="normalized resources root")
    extract.add_argument("--output", "-o", type=Path, required=True, help="output directory")
    extract.add_argument("--table", action="append", default=[], help="table name to extract; repeatable")
    extract.add_argument("--clean", action="store_true", help="remove output directory before extraction")
    extract.add_argument("--no-lua-index", action="store_true", help="skip resolved logic Lua indexing")
    extract.add_argument(
        "--export-scripts",
        choices=("none", "useful", "all"),
        default="useful",
        help="which resolved logic Lua scripts to copy into the output",
    )
    extract.add_argument(
        "--materialize-tables",
        choices=("none", "hardlink", "copy"),
        default="hardlink",
        help="how to materialize decoded table/config/package artifacts",
    )
    query = commands.add_parser("query", help="query one named config row")
    query.add_argument("output", type=Path, help="output directory or resources.sqlite")
    query.add_argument("table", help="normalized table name")
    query.add_argument("key", help="row key")
    find_id = commands.add_parser("find-id", help="find an ID across normalized config tables")
    find_id.add_argument("output", type=Path, help="output directory or resources.sqlite")
    find_id.add_argument("key", help="row key")
    find_id.add_argument("--limit", type=int, default=100)
    verify = commands.add_parser("verify", help="verify a generated output directory")
    verify.add_argument("output", type=Path)
    trim = commands.add_parser("trim", help="create a compact runtime SQLite from resources.sqlite")
    trim.add_argument("input", type=Path, help="full resources.sqlite")
    trim.add_argument("output", type=Path, help="compact output SQLite")
    trim.add_argument("--table", action="append", default=[], help="config table to retain; repeatable")
    trim.add_argument("--resource-version", help="resource/hotfix version stored in output metadata")
    trim.add_argument("--no-references", action="store_true", help="omit validated config references")
    trim.add_argument("--force", action="store_true", help="atomically replace an existing output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "extract":
        selected = set(args.table) or None
        result = ExtractionPipeline(args.resources, args.output).extract(
            selected=selected,
            clean=args.clean,
            index_lua=not args.no_lua_index,
            export_scripts=args.export_scripts,
            materialize_tables=args.materialize_tables,
        )
        print(json.dumps(result["totals"], ensure_ascii=False, indent=2))
        return 0 if result["totals"]["failed"] == 0 else 2
    if args.command in {"query", "find-id"}:
        database_path = args.output if args.output.suffix == ".sqlite" else args.output / "resources.sqlite"
        database = sqlite3.connect(database_path)
        try:
            if args.command == "query":
                row = database.execute(
                    "select data_json from config_rows where table_name = ? and row_key = ?",
                    (args.table, args.key),
                ).fetchone()
                if row is None:
                    print(f"not found: {args.table}[{args.key}]", file=sys.stderr)
                    return 1
                print(json.dumps(json.loads(row[0]), ensure_ascii=False, indent=2))
                return 0
            rows = database.execute(
                "select table_name, data_json from config_rows where row_key = ? order by table_name limit ?",
                (args.key, max(1, args.limit)),
            ).fetchall()
            print(
                json.dumps(
                    [{"table": table, "row": json.loads(raw)} for table, raw in rows],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if rows else 1
        finally:
            database.close()
    if args.command == "verify":
        report = verify_output(args.output)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["valid"] else 2
    if args.command == "trim":
        try:
            report = trim_sqlite(
                args.input,
                args.output,
                tables=args.table,
                resource_version=args.resource_version,
                include_references=not args.no_references,
                force=args.force,
            )
        except (FileExistsError, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            print(f"trim failed: {error}", file=sys.stderr)
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
