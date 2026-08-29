from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LuaSourceEntry:
    source_name: str
    package_id: int
    index: int
    path: str
    source_path: str
    sha256: str
    size: int
    name_hash: int | None
    resolved: bool = True

    @property
    def table_name(self) -> str:
        return self.source_name.removeprefix("LuaCfg.")


class ResourceManifest:
    def __init__(self, resource_root: Path) -> None:
        self.root = resource_root.resolve()
        manifest_path = self.root / "lua_source" / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Lua source manifest not found: {manifest_path}")
        self.manifest_path = manifest_path
        self.data = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.schema = str(self.data.get("schema", ""))

    def source_entries(self, prefix: str | None = None) -> Iterable[LuaSourceEntry]:
        sources = self.data.get("sources", {})
        for source_name in sorted(sources):
            if prefix is not None and not source_name.startswith(prefix):
                continue
            for raw in sources[source_name]:
                source_path = raw.get("source_path")
                if not source_path:
                    continue
                yield LuaSourceEntry(
                    source_name=source_name,
                    package_id=int(raw["package_id"]),
                    index=int(raw["index"]),
                    path=str(raw["path"]),
                    source_path=str(source_path),
                    sha256=str(raw["sha256"]),
                    size=int(raw["size"]),
                    name_hash=int(raw["name_hash"]) if raw.get("name_hash") is not None else None,
                    resolved=True,
                )

    def all_entries(self) -> Iterable[LuaSourceEntry]:
        for name_hash, variants in self.data.get("hashes", {}).items():
            for raw in variants:
                source_name = raw.get("resolved_source") or raw.get("source")
                resolved = bool(source_name)
                if not source_name:
                    digest = str(raw["sha256"])
                    source_name = f"Unresolved/{digest[:2]}/{digest}"
                source_path = raw.get("source_path")
                if not source_path:
                    source_path = (
                        f"lua_source/by_package/{int(raw['package_id'])}/{int(raw['index']):06d}.lua"
                    )
                yield LuaSourceEntry(
                    source_name=str(source_name),
                    package_id=int(raw["package_id"]),
                    index=int(raw["index"]),
                    path=str(raw["path"]),
                    source_path=str(source_path),
                    sha256=str(raw["sha256"]),
                    size=int(raw["size"]),
                    name_hash=int(raw.get("name_hash", name_hash)),
                    resolved=resolved,
                )

    def config_entries(self, selected: set[str] | None = None) -> list[LuaSourceEntry]:
        entries: list[LuaSourceEntry] = []
        for entry in self.source_entries("LuaCfg."):
            if selected and entry.table_name not in selected and entry.source_name not in selected:
                continue
            entries.append(entry)
        return entries

    def resolve(self, relative_path: str) -> Path:
        normalized = relative_path.replace("/", "\\")
        return self.root / Path(normalized)

    def version_metadata(self) -> dict[str, object]:
        root_manifest = self.root / "manifest.json"
        if not root_manifest.is_file():
            return {"lua_source_schema": self.schema}
        data = json.loads(root_manifest.read_text(encoding="utf-8"))
        return {
            "schema": data.get("schema"),
            "scope": data.get("scope"),
            "source_scaffold": data.get("source_scaffold"),
            "lua_format": data.get("lua_format"),
            "counts": data.get("counts"),
            "lua_source_schema": self.schema,
        }
