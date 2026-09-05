import binascii
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pape_res_solver.lua_static import parse_static_lua
from pape_res_solver.lua_runtime import Lua53ConfigRuntime
from pape_res_solver.manifest import ResourceManifest
from pape_res_solver.normalize import normalize_config_table
from pape_res_solver.split_tables import SplitTableResolver


class SplitTableTests(unittest.TestCase):
    def resolver(self, root, rows):
        hashes = {}
        for index, (key, source) in enumerate(rows, 1):
            name = f'LuaCfg.LuaSplitFils.Example.cfg_Example_{key}'
            digest = ~binascii.crc32(name.encode(), 0xFFFFFFFF) & 0xFFFFFFFF
            path = root / f'{index}.lua'
            path.write_text(source, encoding='utf-8')
            hashes.setdefault(str(digest), []).append(dict(package_id=1, index=index,
                path=f'{index}.luac', source_path=path.name, name_hash=digest,
                size=len(source), sha256=hashlib.sha256(source.encode()).hexdigest()))
        (root / 'lua_source').mkdir(exist_ok=True)
        (root / 'lua_source/manifest.json').write_text(json.dumps({'hashes': hashes}))
        return SplitTableResolver(ResourceManifest(root), Lua53ConfigRuntime())

    def test_restore_sparse_rows_and_nested_rewards(self):
        with tempfile.TemporaryDirectory() as directory:
            resolver = self.resolver(Path(directory), [(91, 'return {91, {{2, 2, 100}}}'), (7, 'return {7}')])
            table = parse_static_lua('return {_useBinary=true, _k={ID=1, Reward={2, {Type=1, ID=2, Num=3}}}, [7]=0, [91]=0}')
            report = resolver.restore('Example', table)
            rows = dict(normalize_config_table(table).rows)
            self.assertEqual(report['fragments'], 2)
            self.assertEqual(set(rows), {'7', '91'})
            self.assertEqual(rows['91']['Reward'], [{'Type': 2, 'ID': 2, 'Num': 100}])

    def test_missing_conflicting_and_wrong_id_are_rejected(self):
        for rows, expected in [([], 'missing'), ([(7, 'return {8}')], 'ID mismatch'),
                               ([(7, 'return {7}'), (7, 'return {7, 2}')], 'conflicting')]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                resolver = self.resolver(Path(directory), rows)
                with self.assertRaisesRegex(ValueError, expected):
                    resolver.restore('Example', parse_static_lua('return {_useBinary=true, _k={ID=1}, [7]=0}'))

    def test_fragment_changes_invalidate_dependency_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = self.resolver(root, [(7, 'return {7, 100}')]).fingerprint
            changed = self.resolver(root, [(7, 'return {7, 200}')]).fingerprint
            self.assertNotEqual(original, changed)
