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

    def test_grouped_schema_rows_are_preserved_as_a_list(self) -> None:
        source = """
        local K
        K = {ID = 1, Month = 2, Day = 3, Reward = {4, {Type = 1, ID = 2, Num = 3}}}
        return {
          [202608] = {
            {20260801, 202608, 1, {{1, 1, 10000}}},
            {20260802, 202608, 2, {{201, 100002, 10}}},
            {20260803, 202608, 3, {{2, 2, 10}}},
          },
          ["_k"] = K,
        }
        """
        normalized = normalize_config_table(parse_static_lua(source))
        self.assertEqual(normalized.rows[0][0], "202608")
        self.assertEqual(
            normalized.rows[0][1],
            [
                {"ID": 20260801, "Month": 202608, "Day": 1, "Reward": [{"Type": 1, "ID": 1, "Num": 10000}]},
                {"ID": 20260802, "Month": 202608, "Day": 2, "Reward": [{"Type": 201, "ID": 100002, "Num": 10}]},
                {"ID": 20260803, "Month": 202608, "Day": 3, "Reward": [{"Type": 2, "ID": 2, "Num": 10}]},
            ],
        )

    def test_sparse_grouped_schema_rows_drop_only_nil_slots(self) -> None:
        source = """
        local K
        K = {ID = 1, Month = 2, Need = 3, Reward = {4, {Type = 1, ID = 2, Num = 3}}}
        return {
          [202512] = {
            [7] = {20251201, 202512, 7, {{1, 1, 100000}}},
            [15] = {20251202, 202512, 15, {{2, 2, 100}}},
            [25] = {20251203, 202512, 25, {{400, 400, 1}}},
          },
          ["_k"] = K,
        }
        """
        normalized = normalize_config_table(parse_static_lua(source))
        rows = normalized.rows[0][1]
        self.assertEqual([row["Need"] for row in rows], [7, 15, 25])
        self.assertEqual(rows[2]["Reward"], [{"Type": 400, "ID": 400, "Num": 1}])

    def test_large_sparse_group_keys_do_not_expand_to_largest_id(self) -> None:
        schema = LuaTable(values={"ID": 1, "Value": 2})
        grouped = LuaTable(
            values={
                20260801: LuaTable(values={1: 1, 2: "first"}),
                20260831: LuaTable(values={1: 2, 2: "last"}),
            }
        )
        normalized = normalize_config_table(LuaTable(values={"_k": schema, 202608: grouped}))
        self.assertEqual(
            normalized.rows[0][1],
            [{"ID": 1, "Value": "first"}, {"ID": 2, "Value": "last"}],
        )

    def test_runtime_grouped_rows_without_array_extent_are_not_truncated(self) -> None:
        schema = LuaTable(
            values={"Probability": 1, "ID": 2, "Num": 3, "Type": 4, "Weight": 5, "Must": 6}
        )
        grouped = LuaTable(
            values={
                index: LuaTable(
                    values={1: 1.0, 2: 1242070 + index, 3: 1, 4: 55, 5: 1000, 6: True}
                )
                for index in range(1, 16)
            }
        )
        table = LuaTable(values={"_k": schema, 5175: grouped})

        normalized = normalize_config_table(table)

        rewards = normalized.rows[0][1]
        self.assertEqual(len(rewards), 15)
        self.assertEqual(rewards[-1]["ID"], 1242085)

    def test_battle_pass_level_groups_keep_every_level_and_override_row(self) -> None:
        source = """
        local K
        K = {
          ID = 1,
          Level = 2,
          FreeReward = {3, {Type = 1, ID = 2, Num = 3}},
          PayReward = {4, {Type = 1, ID = 2, Num = 3}},
          ExtraReward = {5, {Type = 1, ID = 2, Num = 3}},
        }
        return {
          [1] = {
            {1, 1, {{1, 1, 10000}}, {{1, 1, 32000}}, nil},
            {1, 2, {{201, 100003, 2}}, {{201, 100003, 6}}, nil},
            {1, 60, {{400, 400, 3}}, {{400, 402, 3}}, nil},
          },
          [20260813] = {
            {20260813, 30, {{400, 400, 2}}, {{400, 402, 2}, {82, 5326, 1}}, {{82, 5326, 1}}},
            {20260813, 60, {{400, 400, 3}}, {{400, 402, 3}, {82, 5326, 1}}, {{82, 5326, 1}}},
          },
          ["_k"] = K,
        }
        """
        normalized = normalize_config_table(parse_static_lua(source))
        groups = dict(normalized.rows)
        self.assertEqual([row["Level"] for row in groups["1"]], [1, 2, 60])
        self.assertEqual([row["Level"] for row in groups["20260813"]], [30, 60])
        self.assertEqual(groups["20260813"][0]["ExtraReward"], [{"Type": 82, "ID": 5326, "Num": 1}])


if __name__ == "__main__":
    unittest.main()
