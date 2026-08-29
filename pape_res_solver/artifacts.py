from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .manifest import ResourceManifest

_PACKAGE_FROM_NAME = re.compile(r"^(\d+)")
_ENTRY_FROM_NAME = re.compile(r"^(\d+)")


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


def process_artifacts(
    manifest: ResourceManifest,
    output: Path,
    database: sqlite3.Connection,
    materialize_mode: str = "hardlink",
) -> dict[str, Any]:
    database.execute("delete from resource_files")
    database.execute("delete from resource_packages")
    database.execute("delete from decoded_table_rows")
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
        digest = _sha256(source) if hash_file else None
        records = _jsonl_records(source) if source.name.lower().endswith(".jsonl") else None
        destination = artifacts_root / relative
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
                str(source.relative_to(manifest.root)).replace("\\", "/"),
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
    if tables_root.is_dir():
        for source in sorted(path for path in tables_root.rglob("*") if path.is_file()):
            relative = source.relative_to(tables_root)
            category = "tables/" + (relative.parts[0] if len(relative.parts) > 1 else "manifest")
            add_file(source, Path("tables") / relative, category, materialize=True)

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

    consolidated = _consolidate_decoded_tables(manifest, output, database)
    config_outputs = _normalize_config_artifacts(manifest, output, database)

    return {
        "schema": "pape-res-artifact-report-v1",
        "materialize_mode": materialize_mode,
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
            x3_rows += 1

    report_path = destination_root / "catalog.json"
    report = {
        "schema": "pape-res-server-tables-v1",
        "msgpack_packages": packages,
        "msgpack_rows": msgpack_rows,
        "x3_rows": x3_rows,
        "parse_failures": parse_failures,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _normalize_config_artifacts(
    manifest: ResourceManifest,
    output: Path,
    database: sqlite3.Connection,
) -> dict[str, Any]:
    destination = output / "server_tables" / "config"
    destination.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"decoded": [], "unresolved": []}

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
        raw = language_manifest.read_bytes()
        try:
            text = raw.decode("ascii")
            if len(text) % 64 or not re.fullmatch(r"[0-9a-fA-F]+", text):
                raise ValueError("content is not a concatenated SHA-256 list")
            hashes = [text[index : index + 64].lower() for index in range(0, len(text), 64)]
            path = destination / "MultiLanguagePackageManifest.jsonl"
            with path.open("w", encoding="ascii", newline="\n") as output_file:
                for index, digest in enumerate(hashes, 1):
                    row = {"index": index, "sha256": digest}
                    raw_row = json.dumps(row, separators=(",", ":"))
                    output_file.write(raw_row + "\n")
                    database.execute(
                        """insert or replace into decoded_table_rows(
                            package_id, entry_key, table_name, format, data_json, source_path
                        ) values (-2, ?, 'MultiLanguagePackageManifest', 'sha256-list', ?, ?)""",
                        (
                            str(index),
                            raw_row,
                            str(language_manifest.relative_to(manifest.root)).replace("\\", "/"),
                        ),
                    )
            result["decoded"].append(
                {
                    "name": "MultiLanguagePackageManifest",
                    "rows": len(hashes),
                    "path": str(path.relative_to(output)).replace("\\", "/"),
                }
            )
        except (OSError, UnicodeError, sqlite3.Error, TypeError, ValueError) as error:
            result["unresolved"].append(
                {
                    "path": str(language_manifest.relative_to(manifest.root)),
                    "error": f"{type(error).__name__}: {error}",
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
