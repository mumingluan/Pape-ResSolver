from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LANGUAGE_SCHEMA = "pape-res-languages-sqlite-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_fingerprint(manifest: dict[str, Any]) -> str:
    resource_sets = []
    for resource_set in manifest.get("resource_sets", []):
        packages = []
        for package in resource_set.get("packages", []):
            packages.append(
                {
                    "package_id": int(package["package_id"]),
                    "kind": package.get("kind"),
                    "source_nx_sha256": package.get("source_nx_sha256"),
                    "source_nxf_sha256": package.get("source_nxf_sha256"),
                }
            )
        resource_sets.append(
            {
                "resource_set_id": int(resource_set["resource_set_id"]),
                "packages": sorted(packages, key=lambda item: item["package_id"]),
            }
        )
    encoded = json.dumps(
        sorted(resource_sets, key=lambda item: item["resource_set_id"]),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reuse_unchanged_database(
    output: Path, manifest: dict[str, Any], manifest_path: Path, fingerprint: str
) -> dict[str, Any] | None:
    if not output.is_file():
        return None
    database = sqlite3.connect(output)
    try:
        metadata = dict(database.execute("select key, value from language_metadata"))
        if metadata.get("content_fingerprint") != fingerprint:
            return None
        counts = {
            "resource_sets": database.execute(
                "select count(*) from language_resource_sets"
            ).fetchone()[0],
            "packages": database.execute("select count(*) from language_packages").fetchone()[0],
            "texts": database.execute("select count(*) from localized_text").fetchone()[0],
        }
        database.executemany(
            "insert or replace into language_metadata(key, value) values (?, ?)",
            {
                "resource_version": str(manifest.get("version") or ""),
                "platform": str(manifest.get("platform") or ""),
                "source_manifest_sha256": _sha256(manifest_path),
                "generated_at": datetime.now(UTC).isoformat(),
            }.items(),
        )
        database.commit()
    except sqlite3.Error:
        return None
    finally:
        database.close()
    return {
        "schema": LANGUAGE_SCHEMA,
        "available": True,
        "database": output.name,
        "counts": counts,
        "packages": [],
        "incremental_status": "reused",
        "content_fingerprint": fingerprint,
    }


def _fields(data: bytes) -> list[tuple[int, bytes]]:
    result: list[tuple[int, bytes]] = []
    position = 0
    while position < len(data):
        start = position
        if position + 4 > len(data):
            raise ValueError("truncated NXF field length")
        length = struct.unpack_from("<I", data, position)[0]
        position += 4
        end = position + length
        if end > len(data):
            raise ValueError("truncated NXF field payload")
        result.append((start, data[position:end]))
        position = end
    return result


def _nx_values(data: bytes) -> dict[int, str]:
    result: dict[int, str] = {}
    position = 0
    while position < len(data):
        start = position
        if position + 4 > len(data):
            raise ValueError("truncated NX value length")
        length = struct.unpack_from("<I", data, position)[0]
        position += 4
        end = position + length
        if end > len(data):
            raise ValueError("truncated NX value payload")
        result[start] = data[position:end].decode("utf-8")
        position = end
    return result


def inspect_nxf(nxf_path: Path) -> dict[str, Any]:
    index = _fields(nxf_path.read_bytes())
    if len(index) < 5:
        raise ValueError(f"NXF index is too short: {nxf_path}")
    try:
        key_type = index[2][1].decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError(f"NXF key type is not ASCII: {nxf_path}") from error
    if len(index[0][1]) != 4 or len(index[3][1]) != 4:
        raise ValueError(f"invalid NXF header: {nxf_path}")
    row_count = struct.unpack("<I", index[0][1])[0]
    dependency_count = struct.unpack("<I", index[3][1])[0]
    pointer_index = 4 + dependency_count
    if pointer_index >= len(index) or len(index[pointer_index][1]) != 4:
        raise ValueError(f"invalid NXF dependency header: {nxf_path}")
    dependencies = []
    for _, payload in index[4:pointer_index]:
        if len(payload) != 4:
            raise ValueError(f"invalid NXF dependency value: {nxf_path}")
        dependencies.append(struct.unpack("<I", payload)[0])
    metadata_pointer = struct.unpack("<I", index[pointer_index][1])[0]
    try:
        metadata_index = next(
            position for position, (field_start, _) in enumerate(index)
            if field_start == metadata_pointer
        )
    except StopIteration as error:
        raise ValueError(f"invalid NXF metadata pointer: {nxf_path}") from error
    return {
        "fields": index,
        "key_type": key_type,
        "row_count": row_count,
        "dependencies": dependencies,
        "pointer_index": pointer_index,
        "metadata_index": metadata_index,
    }


def extract_int32_texts(nx_path: Path, nxf_path: Path) -> tuple[list[tuple[int, str]], dict[str, Any]]:
    header = inspect_nxf(nxf_path)
    if header["key_type"] != "Int32":
        raise ValueError(f"expected Int32 NXF keys, got {header['key_type']}: {nxf_path}")
    index = header["fields"]
    values = _nx_values(nx_path.read_bytes())
    offsets = index[header["pointer_index"] + 1:header["metadata_index"]]
    if len(offsets) != len(values):
        raise ValueError(
            f"NX/NXF unique-value count mismatch ({len(values)} != {len(offsets)}): {nxf_path}"
        )
    pointer_to_nx: dict[int, int] = {}
    for field_start, payload in offsets:
        if len(payload) != 4:
            raise ValueError(f"invalid NXF value pointer: {nxf_path}")
        pointer_to_nx[field_start] = struct.unpack("<I", payload)[0]
    metadata = index[header["metadata_index"]:]
    if len(metadata) != header["row_count"] * 4:
        raise ValueError(f"unrecognized Int32 NXF record layout: {nxf_path}")

    rows: list[tuple[int, str]] = []
    for row_index in range(header["row_count"]):
        record = metadata[row_index * 4:row_index * 4 + 4]
        if any(len(payload) != 4 for _, payload in record):
            raise ValueError(f"invalid Int32 NXF record {row_index}: {nxf_path}")
        if struct.unpack("<I", record[3][1])[0] != record[0][0]:
            raise ValueError(f"invalid Int32 NXF self pointer at row {row_index}: {nxf_path}")
        text_id = struct.unpack("<i", record[0][1])[0]
        value_pointer = struct.unpack("<I", record[1][1])[0]
        try:
            nx_offset = pointer_to_nx[value_pointer]
            text = values[nx_offset]
        except KeyError as error:
            raise ValueError(f"invalid Int32 NXF value reference at row {row_index}: {nxf_path}") from error
        rows.append((text_id, text))
    if len({text_id for text_id, _ in rows}) != len(rows):
        raise ValueError(f"duplicate Int32 keys inside package: {nxf_path}")
    return rows, {
        "key_type": header["key_type"],
        "row_count": len(rows),
        "unique_values": len(values),
        "dependencies": header["dependencies"],
    }


def _create_schema(database: sqlite3.Connection) -> None:
    database.executescript(
        """
        create table language_metadata (
            key text primary key,
            value text not null
        ) without rowid;
        create table language_resource_sets (
            resource_set_id integer primary key,
            language_key text not null unique,
            package_count integer not null,
            text_count integer not null
        ) without rowid;
        create table language_packages (
            resource_set_id integer not null,
            package_id integer not null,
            kind text not null,
            key_type text,
            row_count integer not null,
            unique_value_count integer not null,
            dependencies_json text not null,
            source_path text not null,
            nx_sha256 text,
            nxf_sha256 text,
            primary key(resource_set_id, package_id)
        ) without rowid;
        create table localized_text (
            resource_set_id integer not null,
            text_id integer not null,
            text text not null,
            package_id integer not null,
            primary key(resource_set_id, text_id)
        ) without rowid;
        create index idx_localized_text_id on localized_text(text_id, resource_set_id);
        """
    )


def export_multilanguage_sqlite(
    resource_root: Path, output: Path, incremental: bool = True
) -> dict[str, Any]:
    manifest_path = resource_root / "multilanguage" / "manifest.json"
    if not manifest_path.is_file():
        if output.is_file():
            output.unlink()
        return {
            "schema": LANGUAGE_SCHEMA,
            "available": False,
            "reason": "multilanguage/manifest.json is absent",
        }
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "pape-res-multilanguage-input-v1":
        raise ValueError(f"unsupported multilanguage manifest schema: {manifest.get('schema')}")
    if manifest.get("failures"):
        raise ValueError("Get multilanguage manifest contains missing normalized packages")

    fingerprint = _content_fingerprint(manifest)
    reused = (
        _reuse_unchanged_database(output, manifest, manifest_path, fingerprint)
        if incremental
        else None
    )
    if reused is not None:
        return reused

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + f".tmp.{os.getpid()}")
    if temporary.exists():
        temporary.unlink()
    database = sqlite3.connect(temporary)
    package_reports: list[dict[str, Any]] = []
    try:
        database.execute("pragma journal_mode = delete")
        database.execute("pragma synchronous = normal")
        _create_schema(database)
        metadata = {
            "schema": LANGUAGE_SCHEMA,
            "schema_version": "1",
            "resource_version": str(manifest.get("version") or ""),
            "platform": str(manifest.get("platform") or ""),
            "source_manifest_sha256": _sha256(manifest_path),
            "content_fingerprint": fingerprint,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        database.executemany(
            "insert into language_metadata(key, value) values (?, ?)", metadata.items()
        )
        for resource_set in manifest.get("resource_sets", []):
            resource_set_id = int(resource_set["resource_set_id"])
            language_key = str(resource_set.get("key") or resource_set_id)
            database.execute(
                "insert into language_resource_sets values (?, ?, ?, 0)",
                (resource_set_id, language_key, len(resource_set.get("packages", []))),
            )
            for package in resource_set.get("packages", []):
                package_id = int(package["package_id"])
                kind = str(package.get("kind") or "unknown")
                report = {
                    "resource_set_id": resource_set_id,
                    "package_id": package_id,
                    "kind": kind,
                    "source_path": str(package.get("source_path") or ""),
                    "key_type": None,
                    "row_count": 0,
                    "unique_values": 0,
                    "dependencies": [],
                }
                if kind == "data":
                    nx_path = resource_root.joinpath(*Path(package["decoded_nx_path"]).parts)
                    nxf_path = resource_root.joinpath(*Path(package["decoded_nxf_path"]).parts)
                    header = inspect_nxf(nxf_path)
                    report["key_type"] = header["key_type"]
                    report["dependencies"] = header["dependencies"]
                    if header["key_type"] == "Int32":
                        rows, details = extract_int32_texts(nx_path, nxf_path)
                        report.update(details)
                        database.executemany(
                            "insert into localized_text values (?, ?, ?, ?)",
                            ((resource_set_id, text_id, text, package_id) for text_id, text in rows),
                        )
                    report["nx_sha256"] = _sha256(nx_path)
                    report["nxf_sha256"] = _sha256(nxf_path)
                database.execute(
                    """insert into language_packages values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        resource_set_id,
                        package_id,
                        kind,
                        report["key_type"],
                        report["row_count"],
                        report["unique_values"],
                        json.dumps(report["dependencies"], separators=(",", ":")),
                        report["source_path"],
                        report.get("nx_sha256"),
                        report.get("nxf_sha256"),
                    ),
                )
                package_reports.append(report)
            text_count = database.execute(
                "select count(*) from localized_text where resource_set_id = ?",
                (resource_set_id,),
            ).fetchone()[0]
            database.execute(
                "update language_resource_sets set text_count = ? where resource_set_id = ?",
                (text_count, resource_set_id),
            )
        database.commit()
        integrity = database.execute("pragma integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"language SQLite integrity check failed: {integrity}")
        counts = {
            "resource_sets": database.execute(
                "select count(*) from language_resource_sets"
            ).fetchone()[0],
            "packages": database.execute("select count(*) from language_packages").fetchone()[0],
            "texts": database.execute("select count(*) from localized_text").fetchone()[0],
        }
    except Exception:
        database.close()
        if temporary.exists():
            temporary.unlink()
        raise
    else:
        database.close()
    os.replace(temporary, output)
    return {
        "schema": LANGUAGE_SCHEMA,
        "available": True,
        "database": output.name,
        "counts": counts,
        "packages": package_reports,
        "incremental_status": "rebuilt",
        "content_fingerprint": fingerprint,
    }
