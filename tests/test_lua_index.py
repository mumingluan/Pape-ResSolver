import unittest

from pape_res_solver.lua_index import inspect_script


class LuaIndexTests(unittest.TestCase):
    def test_extracts_dependencies_and_symbols(self) -> None:
        facts = inspect_script(
            """
            local Base = require("Runtime.Base")
            local C = class("GachaBLL", Base)
            function C:Draw()
              local cfg = LuaCfgMgr.Get("GachaAll", 101)
              GrpcMgr.SendRequest(RpcDefines.GachaTenRequest, {})
            end
            """
        )
        self.assertEqual(facts.dependencies, ["Runtime.Base"])
        self.assertEqual(facts.config_tables, ["GachaAll"])
        self.assertEqual(facts.rpc_names, ["GachaTenRequest"])
        self.assertEqual(facts.functions, ["C:Draw"])
        self.assertEqual(facts.classes, ["GachaBLL"])


if __name__ == "__main__":
    unittest.main()
