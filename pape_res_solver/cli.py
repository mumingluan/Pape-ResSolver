from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

from .client_patch import auto_patch_xfilezip_app_key, patch_xfilezip_app_key
from .pipeline import ExtractionPipeline
from .runtime_presets import RUNTIME_PRESETS, runtime_tables
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
    extract.add_argument(
        "--no-multilanguage",
        action="store_true",
        help="skip independent languages.sqlite generation",
    )
    extract.add_argument(
        "--no-incremental",
        action="store_true",
        help="rebuild every Solver table even when the previous output hashes match",
    )
    query = commands.add_parser("query", help="query one named config row")
    query.add_argument("output", type=Path, help="output directory or resources.sqlite")
    query.add_argument("table", help="normalized table name")
    query.add_argument("key", help="row key")
    find_id = commands.add_parser("find-id", help="find an ID across normalized config tables")
    find_id.add_argument("output", type=Path, help="output directory or resources.sqlite")
    find_id.add_argument("key", help="row key")
    find_id.add_argument("--limit", type=int, default=100)
    text = commands.add_parser("text", help="query localized text by numeric ID")
    text.add_argument("output", type=Path, help="output directory or languages.sqlite")
    text.add_argument("text_id", type=int)
    text.add_argument("--resource-set", type=int)
    verify = commands.add_parser("verify", help="verify a generated output directory")
    verify.add_argument("output", type=Path)
    trim = commands.add_parser("trim", help="create a compact runtime SQLite from resources.sqlite")
    trim.add_argument("input", type=Path, help="full resources.sqlite")
    trim.add_argument("output", type=Path, help="compact output SQLite")
    trim.add_argument("--table", action="append", default=[], help="config table to retain; repeatable")
    trim.add_argument(
        "--preset",
        choices=tuple(sorted(RUNTIME_PRESETS)),
        help="retain the reviewed table set for a supported runtime; --table adds extra tables",
    )
    trim.add_argument("--resource-version", help="resource/hotfix version stored in output metadata")
    trim.add_argument("--no-references", action="store_true", help="omit validated config references")
    trim.add_argument("--force", action="store_true", help="atomically replace an existing output")
    patch_app_key = commands.add_parser(
        "patch-app-key", help="generate a client-compatible XFileZip AppKey patch"
    )
    patch_app_key.add_argument(
        "input", type=Path, help="source ZIP, XFileZip directory, or resource root"
    )
    patch_app_key.add_argument(
        "output",
        type=Path,
        help="output ZIP for file input, or output directory for directory input",
    )
    patch_app_key.add_argument("--old-app-key", required=True, help="AppKey currently embedded in Lua")
    patch_app_key.add_argument("--new-app-key", required=True, help="replacement AppKey of equal length")
    patch_app_key.add_argument(
        "--nx-output", type=Path, help="also write the patched runtime NX cache file"
    )
    patch_app_key.add_argument(
        "--expected-matches", type=int, default=1, help="required exact match count (default: 1)"
    )
    patch_app_key.add_argument("--force", action="store_true", help="atomically replace outputs")
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
            extract_multilanguage=not args.no_multilanguage,
            incremental=not args.no_incremental,
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
    if args.command == "text":
        database_path = args.output if args.output.suffix == ".sqlite" else args.output / "languages.sqlite"
        database = sqlite3.connect(database_path)
        try:
            if args.resource_set is None:
                rows = database.execute(
                    """select resource_set_id, text from localized_text
                       where text_id = ? order by resource_set_id""",
                    (args.text_id,),
                ).fetchall()
            else:
                rows = database.execute(
                    """select resource_set_id, text from localized_text
                       where resource_set_id = ? and text_id = ?""",
                    (args.resource_set, args.text_id),
                ).fetchall()
            print(json.dumps(
                [{"resource_set_id": row[0], "text_id": args.text_id, "text": row[1]} for row in rows],
                ensure_ascii=False,
                indent=2,
            ))
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
                tables=runtime_tables(args.preset, args.table),
                resource_version=args.resource_version,
                include_references=not args.no_references,
                force=args.force,
            )
        except (FileExistsError, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            print(f"trim failed: {error}", file=sys.stderr)
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "patch-app-key":
        try:
            if args.input.is_dir():
                if args.nx_output is not None:
                    raise ValueError("--nx-output is automatic when input is a directory")
                report = auto_patch_xfilezip_app_key(
                    args.input,
                    args.output,
                    args.old_app_key,
                    args.new_app_key,
                    expected_matches=args.expected_matches,
                    force=args.force,
                )
            else:
                report = patch_xfilezip_app_key(
                    args.input,
                    args.output,
                    args.old_app_key,
                    args.new_app_key,
                    nx_output=args.nx_output,
                    expected_matches=args.expected_matches,
                    force=args.force,
                )
        except (FileExistsError, OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
            print(f"AppKey patch failed: {error}", file=sys.stderr)
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
