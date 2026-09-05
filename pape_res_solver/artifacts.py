from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
from pathlib import Path
from typing import Any

from .manifest import ResourceManifest

_PACKAGE_FROM_NAME = re.compile(r"^(\d+)")
_ENTRY_FROM_NAME = re.compile(r"^(\d+)")
_DIRTYWORDS_TRANSFORM = bytes(c if c in (0x00, 0xCC) else c ^ 0xCC for c in range(256))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonl_records(path: Path) -> int:
    count = 0
    last = b""
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            count += chunk.count(b"\n")
            last = chunk[-1:]
    return count + (1 if path.stat().st_size and last != b"\n" else 0)


def _materialize(source: Path, destination: Path, mode: str) -> str | None:
    if mode == "none":
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        try:
            if os.path.samefile(source, destination):
                return str(destination)
        except OSError:
            pass
        destination.unlink()
    if mode == "hardlink":
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
    else:
        shutil.copy2(source, destination)
    return str(destination)


def _package_id(relative: Path) -> int | None:
    for part in reversed(relative.parts):
        match = _PACKAGE_FROM_NAME.match(part)
        if match:
            return int(match.group(1))
    return None


def _entry_id(relative: Path) -> int | None:
    match = _ENTRY_FROM_NAME.match(relative.name)
    return int(match.group(1)) if match else None


def _format(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".jsonl"):
        return "jsonl"
    if name.endswith(".json"):
        return "json"
    if name.endswith(".db"):
        return "sqlite"
    if name.endswith((".binary", ".bin")):
        return "binary"
    return path.suffix.lower().lstrip(".") or "unknown"


def _table_content_fingerprint(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    packages = []
    for row in document.get("packages", []):
        fingerprint = row.get("fingerprint")
        if not fingerprint:
            return None
        packages.append((int(row["package_id"]), str(fingerprint)))
    encoded = json.dumps(sorted(packages), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decode_dirtywords(source: Path, destination: Path, jsonl: Path) -> dict[str, Any]:
    raw = source.read_bytes()
    decoded = bytearray(raw.translate(_DIRTYWORDS_TRANSFORM))
    if decoded[:16] != b"SQLite format 3\x00":
        raise ValueError("decoded DirtyWords resource has no SQLite header")
    db_pages = struct.unpack_from(">I", decoded, 28)[0]
    if not db_pages or len(decoded) % db_pages:
        raise ValueError(f"invalid DirtyWords page count: {db_pages}")
    page_size = len(decoded) // db_pages
    if page_size < 512 or page_size > 32768 or page_size & (page_size - 1):
        raise ValueError(f"invalid computed DirtyWords page size: {page_size}")
    struct.pack_into(">H", decoded, 16, page_size)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(decoded)
    rows = 0
    words = 0
    jsonl.unlink(missing_ok=True)
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    output = jsonl.open("w", encoding="utf-8", newline="\n")
    connection = sqlite3.connect(destination)
    try:
        integrity = connection.execute("pragma integrity_check").fetchone()[0]
        for row_id, blob in connection.execute("select id, value from DirtyWords order by id"):
            values: list[str] = []
            if isinstance(blob, bytes) and blob[:1] == b"\x1a":
                body = blob[1:]
                if body.startswith(b"[") and body.endswith(b"]"):
                    for _, encoded_word in re.findall(rb'(\d+),"(.*?)"', body, re.S):
                        values.append(encoded_word.decode("utf-8", "replace"))
            output.write(json.dumps({"id": row_id, "count": len(values), "words": values}, ensure_ascii=False, separators=(",", ":")) + "\n")
            rows += 1
            words += len(values)
    finally:
        connection.close()
        output.close()
    if integrity != "ok":
        raise ValueError(f"DirtyWords integrity check failed: {integrity}")
    return {
        "name": "DirtyWords",
        "source": str(source),
        "sqlite_path": str(destination),
        "jsonl_path": str(jsonl),
        "rows": rows,
        "words": words,
        "page_size": page_size,
        "db_pages": db_pages,
        "integrity": integrity,
    }


def _decode_xfilezip_index(source: Path, destination: Path) -> dict[str, Any]:
    data = bytes(byte ^ 0x0C for byte in source.read_bytes())
    records: list[bytes] = []
    offset = 0
    while offset + 4 <= len(data):
        size = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        if size > len(data) - offset:
            raise ValueError(f"truncated XFileZip record at {offset - 4}")
        records.append(data[offset:offset + size])
        offset += size
    if offset != len(data):
        raise ValueError(f"{len(data) - offset} trailing XFileZip bytes")
    if len(records) < 7:
        raise ValueError("XFileZip index has too few records")

    def u32(record: bytes) -> int:
        if len(record) != 4:
            raise ValueError("XFileZip integer record has invalid length")
        return struct.unpack("<I", record)[0]

    resource_count = u32(records[5])
    resource_end = 6 + resource_count * 4
    if resource_end >= len(records):
        raise ValueError("XFileZip resource table exceeds record stream")
    resources = []
    for index in range(6, resource_end, 4):
        resources.append({
            "resource_id": u32(records[index]),
            "meta_1": u32(records[index + 1]),
            "meta_2": u32(records[index + 2]),
            "meta_3": u32(records[index + 3]),
        })
    path_count = u32(records[resource_end])
    path_records = records[resource_end + 1:]
    if len(path_records) != path_count * 2:
        raise ValueError(f"XFileZip path count mismatch: declared {path_count}, records {len(path_records) // 2}")
    paths = []
    resource_ids = {item["resource_id"] for item in resources}
    for index in range(0, len(path_records), 2):
        resource_id = u32(path_records[index])
        path = path_records[index + 1].decode("utf-8")
        if resource_id not in resource_ids:
            raise ValueError(f"XFileZip path references unknown resource {resource_id}")
        paths.append({"resource_id": resource_id, "path": path})
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as output:
        for row in paths:
            output.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {
        "name": "XFileZip/210201614",
        "source": str(source),
        "jsonl_path": str(destination),
        "records": len(records),
        "resource_count": resource_count,
        "path_count": path_count,
        "version": records[0].decode("utf-8"),
        "blob_name": records[1].decode("utf-8"),
        "checksum": u32(records[4]),
    }


def process_artifacts(
    manifest: ResourceManifest,
    output: Path,
    database: sqlite3.Connection,
    materialize_mode: str = "hardlink",
    incremental: bool = True,
) -> dict[str, Any]:
    current_tables_manifest = manifest.root / "tables" / "manifest.json"
    previous_tables_manifest = output / "artifacts" / "tables" / "manifest.json"
    table_fingerprint = _table_content_fingerprint(current_tables_manifest)
    reuse_tables = bool(
        incremental
        and table_fingerprint
        and table_fingerprint == _table_content_fingerprint(previous_tables_manifest)
    )
    previous_files = {
        str(row[0]): {
            "size": int(row[1]),
            "sha256": row[2],
            "records": row[3],
            "output_path": row[4],
            "materialized": bool(row[5]),
        }
        for row in database.execute(
            "select source_path, size, sha256, records, output_path, materialized from resource_files"
        )
    } if incremental else {}
    if reuse_tables:
        database.execute("delete from resource_files where category not like 'tables/%'")
        database.execute("delete from resource_files where category = 'tables/manifest'")
    else:
        database.execute("delete from resource_files")
    database.execute("delete from resource_packages")
    if reuse_tables:
        database.execute("delete from decoded_table_rows where package_id < 0")
    else:
        database.execute("delete from decoded_table_rows")
        database.execute("delete from config_rows where table_name in (select table_name from config_tables where source_name like 'X3MsgPack.%')")
        database.execute("delete from config_tables where source_name like 'X3MsgPack.%'")
    artifacts_root = output / "artifacts"
    counts: dict[str, dict[str, int]] = {}
    total_files = 0
    total_bytes = 0
    materialized_files = 0

    def add_file(
        source: Path, relative: Path, category: str, materialize: bool, hash_file: bool = True
    ) -> None:
        nonlocal total_files, total_bytes, materialized_files
        stat = source.stat()
        source_relative = str(source.relative_to(manifest.root)).replace("\\", "/")
        destination = artifacts_root / relative
        previous = previous_files.get(source_relative)
        same_materialized_file = False
        if (
            materialize
            and previous
            and previous["materialized"]
            and previous["size"] == stat.st_size
            and destination.is_file()
        ):
            try:
                same_materialized_file = os.path.samefile(source, destination)
            except OSError:
                pass
        if same_materialized_file:
            digest = previous["sha256"] if hash_file else None
            records = previous["records"]
            materialized = str(destination)
        else:
            digest = _sha256(source) if hash_file else None
            records = _jsonl_records(source) if source.name.lower().endswith(".jsonl") else None
            materialized = _materialize(source, destination, materialize_mode) if materialize else None
        if materialized:
            materialized_files += 1
        database.execute(
            """insert into resource_files(
                category, source_path, output_path, package_id, entry_id,
                format, size, sha256, records, materialized
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                category,
                source_relative,
                str(destination.relative_to(output)).replace("\\", "/") if materialized else None,
                _package_id(relative),
                _entry_id(relative),
                _format(source),
                stat.st_size,
                digest,
                records,
                1 if materialized else 0,
            ),
        )
        group = counts.setdefault(category, {"files": 0, "bytes": 0, "records": 0})
        group["files"] += 1
        group["bytes"] += stat.st_size
        group["records"] += records or 0
        total_files += 1
        total_bytes += stat.st_size

    tables_root = manifest.root / "tables"
    if tables_root.is_dir() and not reuse_tables:
        for source in sorted(path for path in tables_root.rglob("*") if path.is_file()):
            relative = source.relative_to(tables_root)
            category = "tables/" + (relative.parts[0] if len(relative.parts) > 1 else "manifest")
            add_file(source, Path("tables") / relative, category, materialize=True)
    elif current_tables_manifest.is_file():
        add_file(
            current_tables_manifest,
            Path("tables/manifest.json"),
            "tables/manifest",
            materialize=True,
        )

    for source in sorted(path for path in (manifest.root / "config").rglob("*") if path.is_file()):
        add_file(
            source, Path("config") / source.relative_to(manifest.root / "config"), "config", materialize=True
        )
    for source in sorted((manifest.root / "packages").glob("*.json")):
        add_file(source, Path("packages") / source.name, "package_manifest", materialize=True)

    for category in ("containers", "indexes"):
        source_root = manifest.root / category
        if not source_root.is_dir():
            continue
        for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
            add_file(
                source,
                Path(category) / source.relative_to(source_root),
                category,
                materialize=False,
                hash_file=False,
            )

    root_manifest = manifest.root / "manifest.json"
    if root_manifest.is_file():
        data = json.loads(root_manifest.read_text(encoding="utf-8"))
        for package in data.get("packages", []):
            database.execute(
                """insert or replace into resource_packages(
                    package_id, kind, entries, failures, decoded_size,
                    unresolved_tail_size, manifest_path
                ) values (?, ?, ?, ?, ?, ?, ?)""",
                (
                    package.get("package_id"),
                    package.get("kind"),
                    package.get("entries"),
                    package.get("failures"),
                    package.get("decoded_size"),
                    package.get("unresolved_tail_size"),
                    package.get("manifest"),
                ),
            )
        add_file(root_manifest, Path("manifest.json"), "root_manifest", materialize=True)

    if reuse_tables and (output / "server_tables" / "catalog.json").is_file():
        consolidated = json.loads(
            (output / "server_tables" / "catalog.json").read_text(encoding="utf-8")
        )
    else:
        consolidated = _consolidate_decoded_tables(manifest, output, database)
    config_outputs = _normalize_config_artifacts(manifest, output, database)

    counts = {
        str(category): {"files": int(files), "bytes": int(size), "records": int(records)}
        for category, files, size, records in database.execute(
            """select category, count(*), coalesce(sum(size), 0), coalesce(sum(records), 0)
               from resource_files group by category"""
        )
    }
    total_files, total_bytes, materialized_files = database.execute(
        "select count(*), coalesce(sum(size), 0), coalesce(sum(materialized), 0) from resource_files"
    ).fetchone()

    return {
        "schema": "pape-res-artifact-report-v1",
        "materialize_mode": materialize_mode,
        "incremental": {
            "table_fingerprint": table_fingerprint,
            "reused_tables": reuse_tables,
        },
        "totals": {
            "files": total_files,
            "bytes": total_bytes,
            "materialized_files": materialized_files,
            "packages": database.execute("select count(*) from resource_packages").fetchone()[0],
        },
        "categories": counts,
        "consolidated": consolidated,
        "config_outputs": config_outputs,
    }


def _consolidate_decoded_tables(
    manifest: ResourceManifest,
    output: Path,
    database: sqlite3.Connection,
) -> dict[str, Any]:
    source_root = manifest.root / "tables"
    destination_root = output / "server_tables"
    msgpack_output = destination_root / "msgpack"
    msgpack_output.mkdir(parents=True, exist_ok=True)
    packages = 0
    msgpack_rows = 0
    x3_rows = 0
    runtime_tables: list[dict[str, Any]] = []
    parse_failures: list[dict[str, str]] = []

    msgpack_root = source_root / "msgpack"
    if msgpack_root.is_dir():
        for package_dir in sorted(path for path in msgpack_root.iterdir() if path.is_dir()):
            if not package_dir.name.isdigit():
                continue
            package_id = int(package_dir.name)
            destination = msgpack_output / f"{package_id}.jsonl"
            wrote = 0
            with destination.open("w", encoding="utf-8", newline="\n") as output_file:
                for source in sorted(package_dir.glob("*.json")):
                    match = _ENTRY_FROM_NAME.match(source.name)
                    if not match:
                        continue
                    entry = int(match.group(1))
                    try:
                        value = json.loads(source.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError) as error:
                        parse_failures.append(
                            {"source_path": str(source), "error": f"{type(error).__name__}: {error}"}
                        )
                        continue
                    compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    output_file.write(
                        json.dumps(
                            {"entry": entry, "value": value}, ensure_ascii=False, separators=(",", ":")
                        )
                        + "\n"
                    )
                    database.execute(
                        """insert or replace into decoded_table_rows(
                            package_id, entry_key, table_name, format, data_json, source_path
                        ) values (?, ?, null, 'msgpack', ?, ?)""",
                        (
                            package_id,
                            str(entry),
                            compact,
                            str(source.relative_to(manifest.root)).replace("\\", "/"),
                        ),
                    )
                    wrote += 1
            if wrote:
                packages += 1
                msgpack_rows += wrote
            else:
                destination.unlink(missing_ok=True)

    x3_root = source_root / "x3_msgpack"
    if x3_root.is_dir():
        for source in sorted(x3_root.rglob("*.json")):
            relative = source.relative_to(x3_root)
            package_id = int(relative.parts[0]) if relative.parts and relative.parts[0].isdigit() else None
            match = re.match(r"^(\d+)_([^.]*)", source.name)
            entry_key = match.group(1) if match else source.stem
            table_name = match.group(2) if match else source.stem
            try:
                value = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                parse_failures.append(
                    {"source_path": str(source), "error": f"{type(error).__name__}: {error}"}
                )
                continue
            database.execute(
                """insert or replace into decoded_table_rows(
                    package_id, entry_key, table_name, format, data_json, source_path
                ) values (?, ?, ?, 'x3_msgpack', ?, ?)""",
                (
                    package_id,
                    str(int(entry_key)) if str(entry_key).isdigit() else str(entry_key),
                    table_name,
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                    str(source.relative_to(manifest.root)).replace("\\", "/"),
                ),
            )
            promoted = _promote_x3_runtime_table(
                source=source,
                source_relative=source.relative_to(manifest.root),
                value=value,
                output=output,
                database=database,
                package_id=package_id,
                entry_index=int(entry_key) if str(entry_key).isdigit() else 0,
                table_name=table_name,
            )
            if promoted is not None:
                runtime_tables.append(promoted)
            x3_rows += 1

    report_path = destination_root / "catalog.json"
    report = {
        "schema": "pape-res-server-tables-v1",
        "msgpack_packages": packages,
        "msgpack_rows": msgpack_rows,
        "x3_rows": x3_rows,
        "runtime_tables": runtime_tables,
        "parse_failures": parse_failures,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _promote_x3_runtime_table(
    *,
    source: Path,
    source_relative: Path,
    value: Any,
    output: Path,
    database: sqlite3.Connection,
    package_id: int | None,
    entry_index: int,
    table_name: str,
) -> dict[str, Any] | None:
    """Expose decoded X3 records through the same runtime API as Lua configs.

    X3 files contain client battle configuration that is semantic server data,
    not an opaque artifact.  Keeping it in config_rows also means sqlite-trim
    preserves it without carrying the raw package or analysis indexes.
    """

    if not isinstance(value, dict):
        return None
    decoded = value.get("decoded")
    if not isinstance(decoded, dict):
        return None
    records = decoded.get("records")
    if not isinstance(records, dict) or not records:
        return None

    runtime_name = f"X3{table_name}"
    rows: list[tuple[str, Any]] = []
    for key, row in records.items():
        if not isinstance(row, (dict, list)):
            continue
        rows.append((str(key), row))
    if not rows:
        return None
    rows.sort(key=lambda item: item[0])

    table_path = output / "tables" / f"{runtime_name}.jsonl"
    table_path.parent.mkdir(parents=True, exist_ok=True)
    with table_path.open("w", encoding="utf-8", newline="\n") as output_file:
        for row_key, row in rows:
            compact = json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            output_file.write(compact + "\n")
            database.execute(
                "insert or replace into config_rows(table_name, row_key, data_json) values (?, ?, ?)",
                (runtime_name, row_key, compact),
            )

    digest = _sha256(source)
    relative_text = str(source_relative).replace("\\", "/")
    record_type = str(decoded.get("record_type") or table_name)
    source_name = f"X3MsgPack.{record_type}"
    database.execute(
        """insert or replace into config_tables(
            table_name, source_name, package_id, entry_index, source_path,
            sha256, row_count, schema_fingerprint, unresolved_values
        ) values (?, ?, ?, ?, ?, ?, ?, null, 0)""",
        (
            runtime_name,
            source_name,
            package_id if package_id is not None else -1,
            entry_index,
            relative_text,
            digest,
            len(rows),
        ),
    )
    return {
        "table": runtime_name,
        "source_name": source_name,
        "package_id": package_id if package_id is not None else -1,
        "index": entry_index,
        "source_path": relative_text,
        "sha256": digest,
        "size": source.stat().st_size,
        "name_hash": None,
        "row_count": len(rows),
        "schema_fingerprint": None,
        "schema_path": None,
        "jsonl_path": str(table_path.relative_to(output)).replace("\\", "/"),
        "unresolved_values": 0,
        "parser_mode": "x3-msgpack-decoded",
        "bytecode_header": None,
    }


def _normalize_config_artifacts(
    manifest: ResourceManifest,
    output: Path,
    database: sqlite3.Connection,
) -> dict[str, Any]:
    destination = output / "server_tables" / "config"
    destination.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"decoded": [], "unresolved": []}

    dirtywords = manifest.root / "config" / "DBCfg" / "DirtyWords.db"
    if dirtywords.is_file():
        try:
            result["decoded"].append(
                _decode_dirtywords(
                    dirtywords,
                    destination / "DirtyWords.sqlite",
                    destination / "DirtyWords.jsonl",
                )
            )
        except (OSError, UnicodeError, sqlite3.Error, struct.error, ValueError) as error:
            result["unresolved"].append(
                {
                    "path": str(dirtywords.relative_to(manifest.root)).replace("\\", "/"),
                    "error": f"{type(error).__name__}: {error}",
                }
            )

    xfilezip = manifest.root / "config" / "XFileZip" / "210201614.bin"
    if xfilezip.is_file():
        try:
            result["decoded"].append(
                _decode_xfilezip_index(xfilezip, destination / "XFileZip_210201614.jsonl")
            )
        except (OSError, UnicodeError, struct.error, ValueError) as error:
            result["unresolved"].append(
                {
                    "path": str(xfilezip.relative_to(manifest.root)).replace("\\", "/"),
                    "error": f"{type(error).__name__}: {error}",
                }
            )

    dynamic = manifest.root / "config" / "DynamicVectorCfg.bin"
    if dynamic.is_file():
        try:
            document = json.loads(dynamic.read_text(encoding="utf-8"))
            rows = document.get("Data", []) if isinstance(document, dict) else []
            path = destination / "DynamicVectorCfg.jsonl"
            with path.open("w", encoding="utf-8", newline="\n") as output_file:
                for index, row in enumerate(rows, 1):
                    output_file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                    database.execute(
                        """insert or replace into decoded_table_rows(
                            package_id, entry_key, table_name, format, data_json, source_path
                        ) values (-1, ?, 'DynamicVectorCfg', 'json', ?, ?)""",
                        (
                            str(index),
                            json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                            str(dynamic.relative_to(manifest.root)).replace("\\", "/"),
                        ),
                    )
            result["decoded"].append(
                {
                    "name": "DynamicVectorCfg",
                    "rows": len(rows),
                    "path": str(path.relative_to(output)).replace("\\", "/"),
                }
            )
        except (OSError, UnicodeError, json.JSONDecodeError, sqlite3.Error, TypeError, ValueError) as error:
            result["unresolved"].append(
                {"path": str(dynamic.relative_to(manifest.root)), "error": f"{type(error).__name__}: {error}"}
            )

    language_manifest = manifest.root / "config" / "MultiLanguagePackageManiFest.bin"
    if language_manifest.is_file():
        result["unresolved"].append(
            {
                "path": str(language_manifest.relative_to(manifest.root)).replace("\\", "/"),
                "size": language_manifest.stat().st_size,
                "sha256": _sha256(language_manifest),
                "reason": (
                    "AES-encrypted JSON used by the client; language extraction uses the "
                    "catalog resource-set manifest instead"
                ),
            }
        )

    for relative in (Path("config/DBCfg/DirtyWords.db"), Path("config/XFileZip/210201614.bin")):
        path = manifest.root / relative
        if path.is_file():
            result["unresolved"].append(
                {
                    "path": str(relative).replace("\\", "/"),
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                    "reason": "proprietary or encrypted binary format",
                }
            )
    return result
