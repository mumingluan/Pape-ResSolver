from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .artifacts import process_artifacts
from .config_recovery import recover_config_names
from .lua_index import index_lua_sources
from .lua_runtime import Lua53ConfigRuntime
from .lua_static import parse_static_lua
from .manifest import LuaSourceEntry, ResourceManifest
from .multilanguage import export_multilanguage_sqlite
from .normalize import normalize_config_table, safe_table_filename
from .validate import validate_references


def _json_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=False, separators=(",", ":"), allow_nan=False)


class ExtractionPipeline:
    def __init__(self, resource_root: Path, output: Path) -> None:
        self.manifest = ResourceManifest(resource_root)
        self.output = output.resolve()
        self.tables_dir = self.output / "tables"
        self.schemas_dir = self.output / "schemas"
        self.reports_dir = self.output / "reports"
        self.runtime = Lua53ConfigRuntime()

    def extract(
        self,
        selected: set[str] | None = None,
        clean: bool = False,
        index_lua: bool = True,
        export_scripts: str = "useful",
        materialize_tables: str = "hardlink",
        extract_multilanguage: bool = True,
    ) -> dict[str, Any]:
        if clean and self.output.exists():
            marker = self.output / ".pape-res-output"
            if marker.is_file() or not any(self.output.iterdir()):
                shutil.rmtree(self.output)
            else:
                raise ValueError(f"refusing to clean unmarked output directory: {self.output}")
        self.tables_dir.mkdir(parents=True, exist_ok=True)
        self.schemas_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        (self.output / ".pape-res-output").write_text("pape-res-solver-output-v1\n", encoding="ascii")
        name_resolution_report = recover_config_names(self.manifest, self.runtime)
        entries = self.manifest.config_entries(selected)
        catalog: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        database = self._open_database()
        try:
            for entry in entries:
                try:
                    item = self._extract_entry(entry, database)
                    catalog.append(item)
                    if item["unresolved_values"]:
                        unresolved.append(
                            {
                                "table": item["table"],
                                "source_path": item["source_path"],
                                "count": item["unresolved_values"],
                            }
                        )
                except Exception as error:  # noqa: BLE001 - isolate failures by resource table
                    failures.append(
                        {
                            "table": entry.table_name,
                            "source_name": entry.source_name,
                            "package_id": entry.package_id,
                            "index": entry.index,
                            "source_path": entry.source_path,
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
            reference_report = validate_references(database)
            lua_index_report = (
                index_lua_sources(self.manifest, self.output, database, export_scripts) if index_lua else None
            )
            artifact_report = process_artifacts(
                self.manifest,
                self.output,
                database,
                materialize_mode=materialize_tables,
            )
            database.commit()
        finally:
            database.close()
        language_report = (
            export_multilanguage_sqlite(self.manifest.root, self.output / "languages.sqlite")
            if extract_multilanguage
            else {"available": False, "reason": "disabled by command line"}
        )
        catalog_document = {
            "schema": "pape-res-solver-catalog-v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "resource_root": str(self.manifest.root),
            "resource": self.manifest.version_metadata(),
            "totals": {
                "selected_entries": len(entries),
                "succeeded": len(catalog),
                "failed": len(failures),
                "rows": sum(int(item["row_count"]) for item in catalog),
                "unresolved_values": sum(int(item["unresolved_values"]) for item in catalog),
                "language_resource_sets": language_report.get("counts", {}).get("resource_sets", 0),
                "localized_texts": language_report.get("counts", {}).get("texts", 0),
            },
            "tables": catalog,
            "multilanguage": language_report,
        }
        self._write_json(self.output / "catalog.json", catalog_document)
        self._write_json(self.reports_dir / "parse_failures.json", failures)
        self._write_json(self.reports_dir / "unresolved_values.json", unresolved)
        self._write_json(self.reports_dir / "references.json", reference_report)
        self._write_json(self.reports_dir / "config_name_resolution.json", name_resolution_report)
        if lua_index_report is not None:
            self._write_json(self.reports_dir / "lua_index.json", lua_index_report)
        self._write_json(self.reports_dir / "artifacts.json", artifact_report)
        self._write_json(self.reports_dir / "multilanguage.json", language_report)
        return catalog_document

    def _extract_entry(self, entry: LuaSourceEntry, database: sqlite3.Connection) -> dict[str, Any]:
        source_path = self.manifest.resolve(entry.source_path)
        bytecode_path = self.manifest.resolve(entry.path)
        parser_mode = "lua53-bytecode"
        bytecode_header: dict[str, Any] | None = None
        try:
            parsed, header = self.runtime.execute(bytecode_path.read_bytes())
            bytecode_header = asdict(header)
        except Exception as bytecode_error:  # noqa: BLE001 - static parser is the compatibility fallback
            parser_mode = "static-source-fallback"
            source = source_path.read_text(encoding="utf-8")
            parsed = parse_static_lua(source, str(source_path))
            bytecode_header = {"fallback_reason": f"{type(bytecode_error).__name__}: {bytecode_error}"}
        normalized = normalize_config_table(parsed)
        filename = safe_table_filename(entry.table_name)
        table_path = self.tables_dir / f"{filename}.jsonl"
        with table_path.open("w", encoding="utf-8", newline="\n") as output:
            for row_key, row in normalized.rows:
                output.write(_json_line(row))
                output.write("\n")
                database.execute(
                    "insert or replace into config_rows(table_name, row_key, data_json) values (?, ?, ?)",
                    (entry.table_name, row_key, _json_line(row)),
                )
        schema_path: str | None = None
        if normalized.schema is not None:
            schema_file = self.schemas_dir / f"{filename}.json"
            self._write_json(schema_file, normalized.schema)
            schema_path = str(schema_file.relative_to(self.output)).replace("\\", "/")
        catalog_item = {
            "table": entry.table_name,
            "source_name": entry.source_name,
            "package_id": entry.package_id,
            "index": entry.index,
            "source_path": entry.source_path,
            "sha256": entry.sha256,
            "size": entry.size,
            "name_hash": entry.name_hash,
            "row_count": len(normalized.rows),
            "schema_fingerprint": normalized.schema_fingerprint,
            "schema_path": schema_path,
            "jsonl_path": str(table_path.relative_to(self.output)).replace("\\", "/"),
            "unresolved_values": normalized.unresolved_values,
            "parser_mode": parser_mode,
            "bytecode_header": bytecode_header,
        }
        database.execute(
            """insert or replace into config_tables(
                table_name, source_name, package_id, entry_index, source_path,
                sha256, row_count, schema_fingerprint, unresolved_values
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.table_name,
                entry.source_name,
                entry.package_id,
                entry.index,
                entry.source_path,
                entry.sha256,
                len(normalized.rows),
                normalized.schema_fingerprint,
                normalized.unresolved_values,
            ),
        )
        return catalog_item

    def _open_database(self) -> sqlite3.Connection:
        path = self.output / "resources.sqlite"
        database = sqlite3.connect(path)
        database.execute("pragma journal_mode = wal")
        database.execute("pragma synchronous = normal")
        database.executescript(
            """
            create table if not exists config_tables (
                table_name text primary key,
                source_name text not null,
                package_id integer not null,
                entry_index integer not null,
                source_path text not null,
                sha256 text not null,
                row_count integer not null,
                schema_fingerprint text,
                unresolved_values integer not null
            );
            create table if not exists config_rows (
                table_name text not null,
                row_key text not null,
                data_json text not null,
                primary key(table_name, row_key)
            );
            create index if not exists idx_config_rows_key on config_rows(row_key);
            create table if not exists config_references (
                source_table text not null,
                source_key text not null,
                field text not null,
                target_table text not null,
                target_key text not null,
                valid integer not null
            );
            create index if not exists idx_config_references_source
                on config_references(source_table, source_key);
            create index if not exists idx_config_references_target
                on config_references(target_table, target_key);
            create table if not exists lua_scripts (
                source_name text not null,
                package_id integer not null,
                entry_index integer not null,
                category text not null,
                resolved integer not null,
                source_path text not null,
                sha256 text not null,
                name_hash integer,
                size integer not null,
                exported_path text,
                primary key(source_name, package_id, entry_index)
            );
            create table if not exists lua_dependencies (
                source_name text not null,
                kind text not null,
                target text not null
            );
            create index if not exists idx_lua_dependencies_source
                on lua_dependencies(source_name, kind);
            create index if not exists idx_lua_dependencies_target
                on lua_dependencies(kind, target);
            create table if not exists resource_files (
                id integer primary key autoincrement,
                category text not null,
                source_path text not null,
                output_path text,
                package_id integer,
                entry_id integer,
                format text not null,
                size integer not null,
                sha256 text,
                records integer,
                materialized integer not null
            );
            create index if not exists idx_resource_files_package
                on resource_files(package_id, entry_id);
            create index if not exists idx_resource_files_category
                on resource_files(category);
            create table if not exists resource_packages (
                package_id integer primary key,
                kind text,
                entries integer,
                failures integer,
                decoded_size integer,
                unresolved_tail_size integer,
                manifest_path text
            );
            create table if not exists decoded_table_rows (
                package_id integer,
                entry_key text not null,
                table_name text,
                format text not null,
                data_json text not null,
                source_path text not null,
                primary key(package_id, entry_key, format)
            );
            create index if not exists idx_decoded_table_rows_name
                on decoded_table_rows(table_name);
            """
        )
        return database

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )
