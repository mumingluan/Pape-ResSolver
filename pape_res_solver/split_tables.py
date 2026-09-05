"""Restore CfgHelper.get_byPKG lazy Lua tables using their exact XLua hashes."""
from __future__ import annotations

import binascii
import hashlib
import json

from .lua_static import LuaTable, parse_static_lua


class SplitTableResolver:
    def __init__(self, manifest, runtime):
        self.manifest = manifest
        self.runtime = runtime
        self.by_hash = {}
        self.by_name = {}
        entries = {(e.package_id, e.index): e for e in manifest.all_entries()}
        entries.update({(e.package_id, e.index): e for e in manifest.source_entries()})
        for entry in entries.values():
            if entry.name_hash is not None:
                self.by_hash.setdefault(entry.name_hash, []).append(entry)
            self.by_name.setdefault(entry.source_name, []).append(entry)
        # Include all fragment dependencies even when only the master is unchanged.
        self.fingerprint = hashlib.sha256(json.dumps(sorted(
            (e.package_id, e.index, e.sha256, e.name_hash) for e in entries.values()
        )).encode()).hexdigest()

    def restore(self, name, table):
        if not isinstance(table, LuaTable) or not table.get('_useBinary'):
            return None
        if not isinstance(table.get('_k'), LuaTable):
            raise ValueError(f'{name}: split table has no schema')
        restored = 0
        for key, value in list(table.values.items()):
            if key in ('_k', '_useBinary') or isinstance(value, LuaTable):
                continue
            if value != 0:
                raise ValueError(f'{name}[{key}]: invalid split placeholder {value!r}')
            source = f'LuaCfg.LuaSplitFils.{name}.cfg_{name}_{key}'
            digest = ~binascii.crc32(source.encode(), 0xFFFFFFFF) & 0xFFFFFFFF
            candidates = self.by_name.get(source) or self.by_hash.get(digest, [])
            if not candidates:
                raise ValueError(f'{name}[{key}]: missing split fragment {source}')
            if len({e.sha256 for e in candidates}) != 1:
                raise ValueError(f'{name}[{key}]: conflicting split fragments {source}')
            entry = candidates[0]
            if entry.resolved and entry.source_name != source:
                raise ValueError(f'{name}[{key}]: fragment hash collision with {entry.source_name}')
            try:
                row, _ = self.runtime.execute(self.manifest.resolve(entry.path).read_bytes())
            except Exception:
                path = self.manifest.resolve(entry.source_path)
                row = parse_static_lua(path.read_text(encoding='utf-8'), str(path))
            if not isinstance(row, LuaTable):
                raise ValueError(f'{name}[{key}]: fragment did not return a table')
            id_position = table.get('_k').get('ID')
            if isinstance(id_position, int) and row.get(id_position) != key:
                raise ValueError(f'{name}[{key}]: fragment ID mismatch')
            table.set(key, row)
            restored += 1
        table.values.pop('_useBinary', None)
        return {'fragments': restored, 'dependency_fingerprint': self.fingerprint,
                'naming': 'CfgHelper.get_byPKG/LuaSplitFils'}
