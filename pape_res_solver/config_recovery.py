from __future__ import annotations

import binascii
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .lua_index import inspect_script
from .lua_runtime import Lua53ConfigRuntime
from .manifest import LuaSourceEntry, ResourceManifest
from .normalize import normalize_config_table

_FUNCTION_START = re.compile(r"(?m)^\s*function\s+([A-Za-z_][A-Za-z0-9_.:]*)")
_CFG_CALL = re.compile(
    r"LuaCfgMgr\.(?:Get|GetAll|GetListByCondition|GetByCondition)\s*\(\s*['\"]([^'\"]+)['\"]"
)
_DIRECT_ASSIGN = re.compile(
    r"(?:local\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"LuaCfgMgr\.(?:Get|GetAll|GetListByCondition|GetByCondition)\s*\(\s*['\"]([^'\"]+)['\"]"
)
_METHOD_ASSIGN = re.compile(
    r"(?:local\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*|self)[:.]([A-Za-z_][A-Za-z0-9_]*)\s*\("
)
_RETURN_CFG = re.compile(
    r"\breturn\s+LuaCfgMgr\.(?:Get|GetAll|GetListByCondition|GetByCondition)"
    r"\s*\(\s*['\"]([^'\"]+)['\"]"
)
_GENERIC_FIELDS = {"ID", "Id", "Name", "Type", "Icon", "Order", "Group", "Desc"}


def _candidate_source_keys(
    manifest: ResourceManifest, entries: list[LuaSourceEntry]
) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    rg = shutil.which("rg")
    source_root = manifest.root / "lua_source" / "by_package"
    package_roots = sorted(
        {
            source_root / str(entry.package_id)
            for entry in entries
            if (source_root / str(entry.package_id)).is_dir()
        }
    )
    if not entries:
        return set(), set()
    if rg is None or not package_roots:
        keys = {(entry.package_id, entry.index) for entry in entries}
        return keys, keys
    relative_to_key = {
        entry.source_path.replace("\\", "/").lower(): (entry.package_id, entry.index)
        for entry in entries
    }

    def search(pattern: str) -> set[tuple[int, int]]:
        process = subprocess.run(
            [rg, "-l", "--text", "--no-messages", "-e", pattern, *(str(path) for path in package_roots)],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode not in (0, 1):
            keys = {(entry.package_id, entry.index) for entry in entries}
            return keys
        result = set()
        for line in process.stdout.splitlines():
            try:
                relative = Path(line).resolve().relative_to(manifest.root).as_posix().lower()
            except (OSError, ValueError):
                continue
            key = relative_to_key.get(relative)
            if key is not None:
                result.add(key)
        return result

    return search(r"LuaCfgMgr\."), search(r"(?:\._k|\[['\"]_k['\"]\]|\b_k)\s*=")


def xlua_crc32(value: str) -> int:
    return ~binascii.crc32(value.encode("utf-8"), 0xFFFFFFFF) & 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class ConfigShape:
    entry: LuaSourceEntry
    fields: frozenset[str]
    rows: int


def _function_blocks(source: str) -> list[tuple[str | None, str]]:
    matches = list(_FUNCTION_START.finditer(source))
    if not matches:
        return [(None, source)]
    blocks: list[tuple[str | None, str]] = []
    if matches[0].start() > 0:
        blocks.append((None, source[: matches[0].start()]))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        blocks.append((match.group(1), source[match.start() : end]))
    return blocks


def _field_accesses(block: str, variable: str) -> set[str]:
    return set(re.findall(rf"\b{re.escape(variable)}\.([A-Za-z_][A-Za-z0-9_]*)", block))


def _collect_usage(sources: list[str]) -> tuple[set[str], dict[str, set[str]]]:
    referenced: set[str] = set()
    wrapper_targets: dict[str, set[str]] = defaultdict(set)
    blocks_by_source: list[list[tuple[str | None, str]]] = []
    for source in sources:
        blocks = _function_blocks(source)
        blocks_by_source.append(blocks)
        referenced.update(inspect_script(source).config_tables)
        for function_name, block in blocks:
            if function_name is None:
                continue
            targets = set(_RETURN_CFG.findall(block))
            if len(targets) == 1:
                wrapper_targets[function_name.split(":")[-1].split(".")[-1]].update(targets)
    wrappers = {name: next(iter(targets)) for name, targets in wrapper_targets.items() if len(targets) == 1}
    usage: dict[str, set[str]] = defaultdict(set)
    for blocks in blocks_by_source:
        for _, block in blocks:
            assignments = list(_DIRECT_ASSIGN.findall(block))
            for variable, method in _METHOD_ASSIGN.findall(block):
                table = wrappers.get(method)
                if table is not None:
                    assignments.append((variable, table))
            for variable, table in assignments:
                fields = _field_accesses(block, variable)
                usage[table].update(fields)
                for item in re.findall(
                    rf"\bfor\s+[^,]+,\s*([A-Za-z_][A-Za-z0-9_]*)\s+in\s+pairs\s*\(\s*{re.escape(variable)}\s*\)",
                    block,
                ):
                    usage[table].update(_field_accesses(block, item))
    return referenced, dict(usage)


def _looks_like_config(source: str) -> bool:
    tail = source[-4096:]
    has_schema = bool(
        re.search(r"(?:\._k|\[['\"]_k['\"]\]|\b_k)\s*=", tail)
    )
    return bool(has_schema and re.search(r"\breturn\s+", tail))


def _shape(entry: LuaSourceEntry, manifest: ResourceManifest, runtime: Lua53ConfigRuntime) -> ConfigShape | None:
    try:
        value, _ = runtime.execute(manifest.resolve(entry.path).read_bytes())
        normalized = normalize_config_table(value)
    except Exception:  # noqa: BLE001 - a logic chunk is simply not a config candidate
        return None
    if not normalized.schema:
        return None
    return ConfigShape(entry=entry, fields=frozenset(normalized.schema), rows=len(normalized.rows))


def apply_config_name_resolutions(
    manifest: ResourceManifest, report: dict[str, Any]
) -> list[dict[str, Any]]:
    current = {
        (entry.package_id, entry.index): entry for entry in manifest.all_entries()
    }
    resolutions = {}
    records = []
    for record in report.get("resolutions", []):
        key = (int(record["package_id"]), int(record["index"]))
        entry = current.get(key)
        if entry is None:
            continue
        expected_hash = record.get("name_hash")
        if expected_hash is not None and entry.name_hash != int(expected_hash):
            continue
        resolutions[key] = str(record["source_name"])
        records.append(record)
    manifest.add_inferred_sources(resolutions)
    return records


def recover_config_names(
    manifest: ResourceManifest,
    runtime: Lua53ConfigRuntime,
    scan_package_ids: set[int] | None = None,
) -> dict[str, Any]:
    """Resolve stripped LuaCfg names from runtime references and schema evidence.

    Direct XLua CRC matches are exact. Alias/hotfix chunks whose physical name
    differs from the LuaCfg registry key are accepted only when field usage has
    one reciprocal, high-confidence schema match.
    """
    entries = list(manifest.all_entries())
    unresolved = [entry for entry in entries if not entry.resolved]
    scan_entries = (
        entries
        if scan_package_ids is None
        else [entry for entry in entries if entry.package_id in scan_package_ids]
    )
    logic_keys, shape_keys = _candidate_source_keys(manifest, scan_entries)
    source_text: dict[tuple[int, int], str] = {}
    logic_sources: list[str] = []
    for entry in entries:
        key = (entry.package_id, entry.index)
        if key not in logic_keys and key not in shape_keys:
            continue
        path = manifest.resolve(entry.source_path)
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if key in shape_keys:
            source_text[key] = source
        if (
            key in logic_keys
            and
            "LuaCfgMgr." in source
            and not entry.source_name.startswith("LuaCfg.")
            and not _looks_like_config(source)
        ):
            logic_sources.append(source)
    referenced, usage = _collect_usage(logic_sources)
    existing_tables = {entry.table_name for entry in manifest.config_entries()}
    by_hash: dict[int, list[LuaSourceEntry]] = defaultdict(list)
    for entry in unresolved:
        if entry.name_hash is not None:
            by_hash[entry.name_hash].append(entry)
    resolutions: dict[tuple[int, int], str] = {}
    records: list[dict[str, Any]] = []
    for table in sorted(referenced - existing_tables):
        matches = by_hash.get(xlua_crc32(f"LuaCfg.{table}"), [])
        if len(matches) != 1:
            continue
        entry = matches[0]
        key = (entry.package_id, entry.index)
        resolutions[key] = f"LuaCfg.{table}"
        records.append(
            {
                "table": table,
                "source_name": f"LuaCfg.{table}",
                "package_id": entry.package_id,
                "index": entry.index,
                "name_hash": entry.name_hash,
                "method": "xlua-crc32-reference",
                "confidence": "exact",
                "evidence_fields": [],
            }
        )
    remaining_entries = [
        entry
        for entry in unresolved
        if (entry.package_id, entry.index) not in resolutions
        and _looks_like_config(source_text.get((entry.package_id, entry.index), ""))
    ]
    shapes = [shape for entry in remaining_entries if (shape := _shape(entry, manifest, runtime))]
    remaining_tables = sorted(referenced - existing_tables - {record["table"] for record in records})
    proposals: dict[str, list[tuple[tuple[float, ...], ConfigShape, set[str]]]] = defaultdict(list)
    for table in remaining_tables:
        evidence = usage.get(table, set())
        if len(evidence) < 2:
            continue
        for shape in shapes:
            matched = evidence & shape.fields
            distinctive = matched - _GENERIC_FIELDS
            coverage = len(matched) / len(evidence)
            if len(distinctive) < 2 or coverage < 0.6:
                continue
            score = (float(len(distinctive)), float(len(matched)), coverage, float(-len(evidence - matched)))
            proposals[table].append((score, shape, matched))
    best_by_shape: dict[tuple[int, int], list[tuple[tuple[float, ...], str]]] = defaultdict(list)
    unique_table_best: dict[str, tuple[tuple[float, ...], ConfigShape, set[str]]] = {}
    for table, candidates in proposals.items():
        candidates.sort(key=lambda item: item[0], reverse=True)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            continue
        unique_table_best[table] = candidates[0]
        shape = candidates[0][1]
        best_by_shape[(shape.entry.package_id, shape.entry.index)].append((candidates[0][0], table))
    for table, (score, shape, matched) in sorted(unique_table_best.items()):
        key = (shape.entry.package_id, shape.entry.index)
        competing = sorted(best_by_shape[key], reverse=True)
        if len(competing) > 1 and competing[0][0] == competing[1][0]:
            continue
        if competing[0][1] != table:
            continue
        resolutions[key] = f"LuaCfg.{table}"
        records.append(
            {
                "table": table,
                "source_name": f"LuaCfg.{table}",
                "package_id": shape.entry.package_id,
                "index": shape.entry.index,
                "name_hash": shape.entry.name_hash,
                "method": "lua-usage-schema",
                "confidence": "unique",
                "evidence_fields": sorted(matched),
                "schema_fields": sorted(shape.fields),
                "row_count": shape.rows,
            }
        )
    manifest.add_inferred_sources(resolutions)
    return {
        "schema": "pape-res-config-name-resolution-v1",
        "referenced_tables": len(referenced),
        "existing_tables": len(existing_tables),
        "config_shaped_unresolved_chunks": len(shapes),
        "resolved": len(records),
        "resolutions": sorted(records, key=lambda record: str(record["table"])),
        "unresolved_referenced_tables": sorted(
            referenced - existing_tables - {str(record["table"]) for record in records}
        ),
    }
