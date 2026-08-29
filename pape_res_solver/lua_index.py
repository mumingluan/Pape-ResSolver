from __future__ import annotations

import json
import re
import shutil
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .manifest import LuaSourceEntry, ResourceManifest
from .normalize import safe_table_filename

_REQUIRE = re.compile(r"\brequire\s*\(?\s*['\"]([^'\"]+)['\"]")
_CONFIG = re.compile(
    r"\bLuaCfgMgr\.(?:Get|GetAll|GetListByCondition|GetByCondition)\s*\(\s*['\"]([^'\"]+)['\"]"
)
_RPC = re.compile(r"\bRpcDefines\.([A-Za-z_][A-Za-z0-9_]*)")
_FUNCTION = re.compile(r"(?:^|\n)\s*(?:local\s+)?function\s+([A-Za-z_][A-Za-z0-9_.:]*)", re.MULTILINE)
_CLASS = re.compile(r"\bclass\s*\(\s*['\"]([^'\"]+)['\"]")


@dataclass(slots=True)
class ScriptFacts:
    dependencies: list[str]
    config_tables: list[str]
    rpc_names: list[str]
    functions: list[str]
    classes: list[str]


def _unique(matches: Iterable[str]) -> list[str]:
    return sorted(set(matches))


def inspect_script(source: str) -> ScriptFacts:
    return ScriptFacts(
        dependencies=_unique(_REQUIRE.findall(source)),
        config_tables=_unique(_CONFIG.findall(source)),
        rpc_names=_unique(_RPC.findall(source)),
        functions=_unique(_FUNCTION.findall(source)),
        classes=_unique(_CLASS.findall(source)),
    )


def classify_source(name: str) -> str:
    if name.startswith("LuaCfg."):
        return "config"
    if name.startswith("PureLogic/"):
        return "pure_logic"
    lowered = name.lower()
    if "dialogue" in lowered:
        return "dialogue"
    if "msgcmd" in lowered or "/command/" in lowered:
        return "protocol_handler"
    if "bll" in lowered:
        return "business_logic"
    if "util" in lowered or "helper" in lowered:
        return "utility"
    return "logic"


def is_useful_server_script(name: str, facts: ScriptFacts) -> bool:
    if name.startswith("PureLogic/"):
        return True
    lowered = name.lower()
    terms = (
        "bll",
        "server",
        "gacha",
        "item",
        "card",
        "quest",
        "stage",
        "shop",
        "login",
        "account",
        "battle",
        "store",
        "reward",
        "inventory",
    )
    return bool(facts.rpc_names or facts.config_tables or any(term in lowered for term in terms))


def _export_path(output: Path, entry: LuaSourceEntry) -> Path:
    raw_segments = entry.source_name.replace("\\", "/").split("/")
    if len(raw_segments) == 1 and "." in raw_segments[0]:
        raw_segments = raw_segments[0].split(".")
    segments = [safe_table_filename(segment) for segment in raw_segments if segment]
    if not segments:
        segments = [f"p{entry.package_id}_e{entry.index:06d}"]
    path = output / "scripts" / Path(*segments).with_suffix(".lua")
    if len(str(path)) > 235:
        path = output / "scripts" / "by_hash" / f"{entry.sha256}.lua"
    return path


def index_lua_sources(
    manifest: ResourceManifest,
    output: Path,
    database: sqlite3.Connection,
    export_mode: str = "useful",
) -> dict[str, object]:
    database.execute("delete from lua_dependencies")
    database.execute("delete from lua_scripts")
    catalog_path = output / "scripts" / "catalog.jsonl"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    totals = {
        "all_entries": 0,
        "resolved_entries": 0,
        "unresolved_entries": 0,
        "config_entries": 0,
        "logic_entries": 0,
        "exported_scripts": 0,
        "dependencies": 0,
        "config_references": 0,
        "rpc_references": 0,
        "functions": 0,
    }
    copied_hashes: dict[Path, str] = {}
    with catalog_path.open("w", encoding="utf-8", newline="\n") as catalog:
        for entry in manifest.all_entries():
            totals["all_entries"] += 1
            totals["resolved_entries" if entry.resolved else "unresolved_entries"] += 1
            category = classify_source(entry.source_name)
            if category == "config":
                totals["config_entries"] += 1
                continue
            totals["logic_entries"] += 1
            path = manifest.resolve(entry.source_path)
            source = path.read_text(encoding="utf-8", errors="replace")
            facts = inspect_script(source)
            should_export = export_mode == "all" or (
                export_mode == "useful" and is_useful_server_script(entry.source_name, facts)
            )
            exported_path: str | None = None
            if should_export:
                destination = _export_path(output, entry)
                previous_hash = copied_hashes.get(destination)
                if previous_hash is not None and previous_hash != entry.sha256:
                    destination = destination.with_name(
                        f"{destination.stem}__p{entry.package_id}_e{entry.index:06d}.lua"
                    )
                if destination not in copied_hashes:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination)
                    copied_hashes[destination] = entry.sha256
                    totals["exported_scripts"] += 1
                exported_path = str(destination.relative_to(output)).replace("\\", "/")
            record = {
                "source_name": entry.source_name,
                "resolved": entry.resolved,
                "category": category,
                "package_id": entry.package_id,
                "index": entry.index,
                "source_path": entry.source_path,
                "sha256": entry.sha256,
                "name_hash": entry.name_hash,
                "size": entry.size,
                "exported_path": exported_path,
                "dependencies": facts.dependencies,
                "config_tables": facts.config_tables,
                "rpc_names": facts.rpc_names,
                "functions": facts.functions,
                "classes": facts.classes,
            }
            catalog.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            database.execute(
                """insert or replace into lua_scripts(
                    source_name, package_id, entry_index, category, resolved,
                    source_path, sha256, name_hash, size, exported_path
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.source_name,
                    entry.package_id,
                    entry.index,
                    category,
                    1 if entry.resolved else 0,
                    entry.source_path,
                    entry.sha256,
                    entry.name_hash,
                    entry.size,
                    exported_path,
                ),
            )
            for dependency in facts.dependencies:
                database.execute(
                    "insert into lua_dependencies(source_name, kind, target) values (?, 'require', ?)",
                    (entry.source_name, dependency),
                )
            for table in facts.config_tables:
                database.execute(
                    "insert into lua_dependencies(source_name, kind, target) values (?, 'config', ?)",
                    (entry.source_name, table),
                )
            for rpc in facts.rpc_names:
                database.execute(
                    "insert into lua_dependencies(source_name, kind, target) values (?, 'rpc', ?)",
                    (entry.source_name, rpc),
                )
            totals["dependencies"] += len(facts.dependencies)
            totals["config_references"] += len(facts.config_tables)
            totals["rpc_references"] += len(facts.rpc_names)
            totals["functions"] += len(facts.functions)
    return {
        "schema": "pape-res-lua-index-report-v1",
        "export_mode": export_mode,
        "totals": totals,
        "catalog": str(catalog_path.relative_to(output)).replace("\\", "/"),
    }
