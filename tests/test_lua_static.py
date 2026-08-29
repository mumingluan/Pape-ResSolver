import unittest

from pape_res_solver.lua_static import LuaTable, parse_static_lua
from pape_res_solver.normalize import normalize_config_table


class StaticLuaParserTests(unittest.TestCase):
    def test_decompiler_aliases_and_dynamic_keys(self) -> None:
        source = """
        local L1, L2
        L0 = {}
        L1 = {51, 121130, 1}
        L0.A1 = L1
        L1 = "A2"
        L0[L1] = {51, 121119, 1}
        L1 = {}
        L1.ID = 1
        L1.Reward = {2, {Type = 1, ID = 2, Num = 3}}
        return {
          [101] = {101, {L0.A1, L0.A2}},
          ["_k"] = L1
        }
        """
        result = parse_static_lua(source)
        self.assertIsInstance(result, LuaTable)
        normalized = normalize_config_table(result)
        self.assertEqual(normalized.rows[0][0], "101")
        self.assertEqual(normalized.rows[0][1]["ID"], 101)
        self.assertEqual(
            normalized.rows[0][1]["Reward"],
            [
                {"Type": 51, "ID": 121130, "Num": 1},
                {"Type": 51, "ID": 121119, "Num": 1},
            ],
        )

    def test_integer_and_nil_positions_are_preserved(self) -> None:
        source = """
        local K
        K = {ID = 1, Missing = 2, Value = 3}
        return {[9007199254740993] = {9007199254740993, nil, -4}, ["_k"] = K}
        """
        normalized = normalize_config_table(parse_static_lua(source))
        row = normalized.rows[0][1]
        self.assertEqual(row["ID"], 9007199254740993)
        self.assertIsNone(row["Missing"])
        self.assertEqual(row["Value"], -4)


if __name__ == "__main__":
    unittest.main()
