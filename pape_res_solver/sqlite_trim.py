from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

RUNTIME_SCHEMA = "pape-res-runtime-sqlite-v1"
APPLICATION_ID = 0x50525331  # "PRS1"

_CONFIG_TABLE_COLUMNS = (
    "table_name",
    "source_name",
    "package_id",
    "entry_index",
    "source_path",
    "sha256",
    "row_count",
    "schema_fingerprint",
    "unresolved_values",
)
_CONFIG_ROW_COLUMNS = ("table_name", "row_key", "data_json")
_REFERENCE_COLUMNS = (
    "source_table",
    "source_key",
    "field",
    "target_table",
    "target_key",
    "valid",
)


def trim_sqlite(
    source: Path,
    output: Path,
    *,
    tables: Iterable[str] | None = None,
    resource_version: str | None = None,
    include_references: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Create a compact, read-only-runtime-oriented resource database.

    The output intentionally contains only named configuration rows, their
    provenance, optional validated references, and self-describing metadata.
    Analysis indexes, recovered Lua catalogs, artifact inventories, and
    non-semantic decoded package rows are omitted.
    """

    source = source.expanduser().resolve(strict=True)
    output = output.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"input is not a file: {source}")
    if source == output:
        raise ValueError("input and output SQLite paths must differ")
    if output.exists() and not force:
        raise FileExistsError(f"output already exists (use --force): {output}")
    _require_checkpointed_source(source)

    selected_names = sorted({name.strip() for name in tables or () if name.strip()})
    source_stat = source.stat()
    source_sha256 = _sha256_file(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")

    report: dict[str, Any]
    try:
        report = _build_database(
            source,
            temporary,
            selected_names=selected_names,
            resource_version=resource_version,
            include_references=include_references,
            source_size=source_stat.st_size,
            source_sha256=source_sha256,
        )
        current_stat = source.stat()
        if (current_stat.st_size, current_stat.st_mtime_ns) != (
            source_stat.st_size,
            source_stat.st_mtime_ns,
        ):
            raise RuntimeError("input SQLite changed while it was being trimmed")
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    output_size = output.stat().st_size
    report.update(
        {
            "input": str(source),
            "output": str(output),
            "input_bytes": source_stat.st_size,
            "output_bytes": output_size,
            "saved_bytes": source_stat.st_size - output_size,
            "reduction_percent": round((1 - output_size / source_stat.st_size) * 100, 2),
        }
    )
    return report


def _build_database(
    source: Path,
    output: Path,
    *,
    selected_names: list[str],
    resource_version: str | None,
    include_references: bool,
    source_size: int,
    source_sha256: str,
) -> dict[str, Any]:
    database = sqlite3.connect(output, uri=True)
    try:
        database.execute("pragma page_size = 8192")
        database.execute("pragma journal_mode = off")
        database.execute("pragma synchronous = off")
        database.execute("pragma temp_store = memory")
        database.execute(f"pragma application_id = {APPLICATION_ID}")
        database.execute("pragma user_version = 1")
        database.executescript(
            """
            create table resource_metadata (
                key text primary key,
                value text not null
            ) without rowid;
            create table config_tables (
                table_name text primary key,
                source_name text not null,
                package_id integer not null,
                entry_index integer not null,
                source_path text not null,
                sha256 text not null,
                row_count integer not null,
                schema_fingerprint text,
                unresolved_values integer not null
            ) without rowid;
            create table config_rows (
                table_name text not null,
                row_key text not null,
                data_json text not null,
                primary key(table_name, row_key)
            ) without rowid;
            create temp table selected_tables (
                table_name text primary key
            ) without rowid;
            """
        )
        source_uri = f"file:{quote(source.as_posix(), safe='/:')}?mode=ro&immutable=1"
        database.execute("attach database ? as source", (source_uri,))
        _validate_source_schema(database)

        available = {
            row[0] for row in database.execute("select table_name from source.config_tables")
        }
        missing = sorted(set(selected_names) - available)
        if missing:
            raise ValueError(f"config tables not found in input: {', '.join(missing)}")
        effective_names = selected_names or sorted(available)
        database.executemany(
            "insert into selected_tables(table_name) values (?)",
            ((name,) for name in effective_names),
        )

        table_columns = ", ".join(_CONFIG_TABLE_COLUMNS)
        row_columns = ", ".join(_CONFIG_ROW_COLUMNS)
        database.execute(
            f"""insert into config_tables({table_columns})
                select {', '.join(f't.{name}' for name in _CONFIG_TABLE_COLUMNS)}
                from source.config_tables t
                join selected_tables s on s.table_name = t.table_name"""
        )
        database.execute(
            f"""insert into config_rows({row_columns})
                select {', '.join(f'r.{name}' for name in _CONFIG_ROW_COLUMNS)}
                from source.config_rows r
                join selected_tables s on s.table_name = r.table_name"""
        )

        reference_count = 0
        if include_references and _source_table_exists(database, "config_references"):
            database.executescript(
                """
                create table config_references (
                    source_table text not null,
                    source_key text not null,
                    field text not null,
                    target_table text not null,
                    target_key text not null,
                    valid integer not null
                );
                create index idx_config_references_source
                    on config_references(source_table, source_key);
                create index idx_config_references_target
                    on config_references(target_table, target_key);
                """
            )
            reference_columns = ", ".join(_REFERENCE_COLUMNS)
            if selected_names:
                database.execute(
                    f"""insert into config_references({reference_columns})
                        select {', '.join(f'r.{name}' for name in _REFERENCE_COLUMNS)}
                        from source.config_references r
                        join selected_tables source_selection
                          on source_selection.table_name = r.source_table
                        join selected_tables target_selection
                          on target_selection.table_name = r.target_table"""
                )
            else:
                database.execute(
                    f"""insert into config_references({reference_columns})
                        select {reference_columns} from source.config_references"""
                )
            reference_count = database.execute("select count(*) from config_references").fetchone()[0]

        table_count = database.execute("select count(*) from config_tables").fetchone()[0]
        row_count = database.execute("select count(*) from config_rows").fetchone()[0]
        inferred_version = _source_metadata(database, "resource_version")
        metadata = {
            "schema": RUNTIME_SCHEMA,
            "schema_version": "1",
            "resource_version": resource_version or inferred_version or "",
            "generated_at": datetime.now(UTC).isoformat(),
            "source_filename": source.name,
            "source_size": str(source_size),
            "source_sha256": source_sha256,
            "config_table_count": str(table_count),
            "config_row_count": str(row_count),
            "config_reference_count": str(reference_count),
            "selected_tables": json.dumps(effective_names, ensure_ascii=False, separators=(",", ":")),
        }
        database.executemany(
            "insert into resource_metadata(key, value) values (?, ?)", metadata.items()
        )
        database.execute("drop table selected_tables")
        database.commit()
        integrity = database.execute("pragma integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"trimmed SQLite integrity check failed: {integrity}")
        database.execute("detach database source")
        database.execute("pragma optimize")
        database.commit()
        return {
            "schema": RUNTIME_SCHEMA,
            "resource_version": metadata["resource_version"],
            "config_tables": table_count,
            "config_rows": row_count,
            "config_references": reference_count,
            "integrity": integrity,
        }
    finally:
        database.close()


def _validate_source_schema(database: sqlite3.Connection) -> None:
    required = {
        "config_tables": set(_CONFIG_TABLE_COLUMNS),
        "config_rows": set(_CONFIG_ROW_COLUMNS),
    }
    for table, expected_columns in required.items():
        if not _source_table_exists(database, table):
            raise ValueError(f"input is not a Pape resource SQLite: missing {table}")
        actual_columns = {
            row[1] for row in database.execute(f"pragma source.table_info({table})")
        }
        missing = expected_columns - actual_columns
        if missing:
            raise ValueError(f"input table {table} is missing columns: {', '.join(sorted(missing))}")


def _source_table_exists(database: sqlite3.Connection, table: str) -> bool:
    return (
        database.execute(
            "select 1 from source.sqlite_master where type = 'table' and name = ?", (table,)
        ).fetchone()
        is not None
    )


def _source_metadata(database: sqlite3.Connection, key: str) -> str | None:
    if not _source_table_exists(database, "resource_metadata"):
        return None
    row = database.execute(
        "select value from source.resource_metadata where key = ?", (key,)
    ).fetchone()
    return None if row is None else str(row[0])


def _require_checkpointed_source(source: Path) -> None:
    wal = Path(f"{source}-wal")
    if wal.is_file() and wal.stat().st_size > 0:
        raise ValueError(f"input has a non-empty WAL; checkpoint it before trimming: {wal}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
