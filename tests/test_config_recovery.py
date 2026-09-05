import binascii
import json
import tempfile
import unittest
from pathlib import Path

from lupa.lua53 import LuaRuntime

from pape_res_solver.config_recovery import recover_config_names
from pape_res_solver.lua_bytecode import convert_lua53_size_t
from pape_res_solver.lua_runtime import Lua53ConfigRuntime
from pape_res_solver.manifest import ResourceManifest


def _crc(value: str) -> int:
    return ~binascii.crc32(value.encode("utf-8"), 0xFFFFFFFF) & 0xFFFFFFFF


class ConfigNameRecoveryTests(unittest.TestCase):
    def test_register_style_schema_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = 'local t={[7]={7,1,2}}; local k="_k"; t[k]={ID=1,EffectCDType=2,TriggerCount=3}; return t'
            logic = 'local c=LuaCfgMgr.Get("EasterEgg",7); return c.EffectCDType,c.TriggerCount'
            manifest = self._fixture(Path(temporary), [(1,config,123),(2,logic,456)])
            report = recover_config_names(manifest,Lua53ConfigRuntime())
            self.assertEqual(report['resolutions'][0]['table'],'EasterEgg')

    def _fixture(self, root: Path, chunks: list[tuple[int, str, int]]) -> ResourceManifest:
        source_dir = root / "lua_source" / "by_package" / "7"
        bytecode_dir = root / "lua" / "7"
        source_dir.mkdir(parents=True)
        bytecode_dir.mkdir(parents=True)
        compiler_runtime = LuaRuntime(encoding=None, register_eval=False, register_builtins=False)
        compiler = compiler_runtime.eval(
            b"function(source) local fn, err = load(source, '@fixture', 't', {}); "
            b"if not fn then error(err) end; return string.dump(fn, true) end"
        )
        hashes: dict[str, list[dict[str, object]]] = {}
        for index, source, name_hash in chunks:
            source_path = source_dir / f"{index:06d}.lua"
            bytecode_path = bytecode_dir / f"{index:06d}.luac"
            source_path.write_text(source, encoding="utf-8")
            native = compiler(source.encode("utf-8"))
            chunk, _ = convert_lua53_size_t(native, 4)
            bytecode_path.write_bytes(chunk)
            hashes.setdefault(str(name_hash), []).append(
                {
                    "package_id": 7,
                    "index": index,
                    "path": f"lua/7/{index:06d}.luac",
                    "source_path": f"lua_source/by_package/7/{index:06d}.lua",
                    "sha256": f"sha-{index}",
                    "size": len(chunk),
                    "name_hash": name_hash,
                    "source": None,
                }
            )
        manifest = {"schema": "fixture-v1", "sources": {}, "hashes": hashes}
        (root / "lua_source" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return ResourceManifest(root)

    def test_exact_hash_resolves_name_referenced_by_logic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = "return {[1] = {1, 'x'}, _k = {ID = 1, Value = 2}}"
            logic = 'local cfg = LuaCfgMgr.Get("ExactTable", 1); return cfg.Value'
            manifest = self._fixture(
                root,
                [(1, config, _crc("LuaCfg.ExactTable")), (2, logic, 99)],
            )
            report = recover_config_names(manifest, Lua53ConfigRuntime())
            self.assertEqual(report["resolved"], 1)
            entry = manifest.config_entries()[0]
            self.assertEqual(entry.source_name, "LuaCfg.ExactTable")
            self.assertEqual(report["resolutions"][0]["method"], "xlua-crc32-reference")

    def test_hotfix_alias_uses_unique_runtime_field_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monthly = (
                "return {[1] = {1, 30, 9, {{2, 2, 100}}}, "
                "_k = {ID = 1, Duration = 2, PayID = 3, DailyReward = {4, {Type=1, ID=2, Num=3}}}}"
            )
            decoy = "return {[1] = {1, 2, 'n'}, _k = {ID = 1, Type = 2, Name = 3}}"
            logic = """
            function B:GetCfgMonthlyCard(key)
              return LuaCfgMgr.Get("MonthlyCard", key) or {}
            end
            function B:Read(key)
              local cfg = self:GetCfgMonthlyCard(key)
              return cfg.Duration, cfg.PayID, cfg.DailyReward
            end
            """
            manifest = self._fixture(root, [(1, monthly, 123), (2, decoy, 456), (3, logic, 789)])
            report = recover_config_names(manifest, Lua53ConfigRuntime())
            resolution = next(row for row in report["resolutions"] if row["table"] == "MonthlyCard")
            self.assertEqual(resolution["method"], "lua-usage-schema")
            self.assertEqual(resolution["index"], 1)
            self.assertEqual(
                resolution["evidence_fields"], ["DailyReward", "Duration", "PayID"]
            )
            self.assertEqual(manifest.config_entries()[0].source_name, "LuaCfg.MonthlyCard")

    def test_ambiguous_schema_is_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = "return {[1] = {1, 30, 9}, _k = {ID = 1, Duration = 2, PayID = 3}}"
            logic = (
                'local cfg = LuaCfgMgr.Get("MonthlyCard", 1); '
                "return cfg.Duration, cfg.PayID"
            )
            manifest = self._fixture(root, [(1, config, 1), (2, config, 2), (3, logic, 3)])
            report = recover_config_names(manifest, Lua53ConfigRuntime())
            self.assertEqual(report["resolved"], 0)
            self.assertEqual(manifest.config_entries(), [])


if __name__ == "__main__":
    unittest.main()
