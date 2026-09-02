import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from lupa.lua53 import LuaRuntime

from pape_res_solver.lua_bytecode import convert_lua53_size_t
from pape_res_solver.pipeline import ExtractionPipeline


class VersionedPipelineTests(unittest.TestCase):
    def test_package_ids_and_paths_come_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "resources"
            source_dir = root / "lua_source" / "by_package" / "987654321"
            bytecode_dir = root / "lua" / "987654321"
            source_dir.mkdir(parents=True)
            bytecode_dir.mkdir(parents=True)
            lua_source = "return {[77] = {77, 'portable'}, ['_k'] = {ID = 1, Name = 2}}"
            (source_dir / "000007.lua").write_text(lua_source, encoding="utf-8")

            runtime = LuaRuntime(encoding=None, register_eval=False, register_builtins=False)
            compiler = runtime.eval(
                b"function(source) local fn, err = load(source, '@fixture', 't', {}); if not fn then error(err) end; return string.dump(fn, true) end"
            )
            native_chunk = compiler(lua_source.encode("utf-8"))
            size_t4, _ = convert_lua53_size_t(native_chunk, 4)
            (bytecode_dir / "000007.luac").write_bytes(size_t4)

            sha = "fixture-sha"
            manifest = {
                "schema": "fixture-lua-manifest-v1",
                "totals": {},
                "packages": [],
                "package_failures": 0,
                "sources": {
                    "LuaCfg.PortableExample": [
                        {
                            "package_id": 987654321,
                            "index": 7,
                            "path": "lua/987654321/000007.luac",
                            "source_path": "lua_source/by_package/987654321/000007.lua",
                            "sha256": sha,
                            "size": len(size_t4),
                            "name_hash": 123,
                        }
                    ]
                },
                "hashes": {},
            }
            (root / "lua_source" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (root / "manifest.json").write_text(
                json.dumps({"schema": "fixture-resource-v1", "packages": []}), encoding="utf-8"
            )
            output = Path(temporary) / "output"
            result = ExtractionPipeline(root, output).extract(
                selected={"PortableExample"},
                index_lua=False,
                materialize_tables="none",
            )
            self.assertEqual(result["totals"]["succeeded"], 1)
            self.assertEqual(result["tables"][0]["package_id"], 987654321)
            self.assertTrue(
                ExtractionPipeline(root, output)
                .manifest.resolve("lua_source\\by_package\\987654321\\000007.lua")
                .samefile(source_dir / "000007.lua")
            )
            row = json.loads((output / "tables" / "PortableExample.jsonl").read_text())
            self.assertEqual(row, {"ID": 77, "Name": "portable"})

            repeated = ExtractionPipeline(root, output).extract(
                selected=None,
                index_lua=False,
                materialize_tables="none",
            )
            self.assertEqual(repeated["incremental"]["reused_tables"], 1)
            self.assertEqual(repeated["incremental"]["rebuilt_tables"], 0)

            changed_source = "return {[88] = {88, 'changed'}, ['_k'] = {ID = 1, Name = 2}}"
            (source_dir / "000007.lua").write_text(changed_source, encoding="utf-8")
            changed_chunk = compiler(changed_source.encode("utf-8"))
            changed_size_t4, _ = convert_lua53_size_t(changed_chunk, 4)
            (bytecode_dir / "000007.luac").write_bytes(changed_size_t4)
            manifest["sources"]["LuaCfg.PortableExample"][0]["sha256"] = "fixture-sha-next"
            manifest["sources"]["LuaCfg.PortableExample"][0]["size"] = len(changed_size_t4)
            (root / "lua_source/manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            changed = ExtractionPipeline(root, output).extract(
                index_lua=False,
                materialize_tables="none",
            )
            self.assertEqual(changed["incremental"]["rebuilt_tables"], 1)
            self.assertEqual(changed["incremental"]["reused_tables"], 0)
            database = sqlite3.connect(output / "resources.sqlite")
            try:
                keys = database.execute(
                    "select row_key from config_rows where table_name = 'PortableExample'"
                ).fetchall()
                self.assertEqual(keys, [("88",)])
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
