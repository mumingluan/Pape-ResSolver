import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pape_res_solver.sqlite_trim import RUNTIME_SCHEMA, trim_sqlite


class SQLiteTrimTests(unittest.TestCase):
    def test_trims_analysis_tables_and_preserves_config_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "resources.sqlite"
            output = root / "runtime.sqlite"
            self._create_fixture(source)

            report = trim_sqlite(source, output, resource_version="1.7.test")

            self.assertEqual(report["schema"], RUNTIME_SCHEMA)
            self.assertEqual(report["config_tables"], 2)
            self.assertEqual(report["config_rows"], 3)
            self.assertEqual(report["config_references"], 3)
            database = sqlite3.connect(output)
            try:
                names = {
                    row[0]
                    for row in database.execute(
                        "select name from sqlite_master where type = 'table'"
                    )
                }
                self.assertEqual(
                    names,
                    {"resource_metadata", "config_tables", "config_rows", "config_references"},
                )
                row = database.execute(
                    "select data_json from config_rows where table_name = ? and row_key = ?",
                    ("Task", "100"),
                ).fetchone()
                self.assertEqual(json.loads(row[0]), {"ID": 100, "AddReward": []})
                metadata = dict(database.execute("select key, value from resource_metadata"))
                self.assertEqual(metadata["resource_version"], "1.7.test")
                self.assertEqual(metadata["schema"], RUNTIME_SCHEMA)
                self.assertEqual(database.execute("pragma integrity_check").fetchone()[0], "ok")
            finally:
                database.close()

    def test_table_selection_removes_dangling_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "resources.sqlite"
            output = root / "task.sqlite"
            self._create_fixture(source)

            report = trim_sqlite(source, output, tables=["Task"])

            self.assertEqual(report["config_tables"], 1)
            self.assertEqual(report["config_rows"], 2)
            self.assertEqual(report["config_references"], 1)
            database = sqlite3.connect(output)
            try:
                self.assertEqual(
                    database.execute("select distinct table_name from config_rows").fetchall(),
                    [("Task",)],
                )
                reference = database.execute(
                    "select source_table, target_table from config_references"
                ).fetchone()
                self.assertEqual(reference, ("Task", "Task"))
            finally:
                database.close()

    def test_refuses_missing_table_and_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "resources.sqlite"
            output = root / "runtime.sqlite"
            self._create_fixture(source)
            output.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                trim_sqlite(source, output)
            self.assertEqual(output.read_bytes(), b"keep")
            output.unlink()
            with self.assertRaisesRegex(ValueError, "Missing"):
                trim_sqlite(source, output, tables=["Missing"])
            self.assertFalse(output.exists())

    @staticmethod
    def _create_fixture(path: Path) -> None:
        database = sqlite3.connect(path)
        try:
            database.executescript(
                """
                create table config_tables (
                    table_name text primary key,
                    source_name text not null,
                    package_id integer not null,
                    entry_index integer not null,
                    source_path text not null,
                    sha256 text not null,
                    row_count integer not null,
                    schema_fingerprint text,
                    unresolved_values integer not null
                );
                create table config_rows (
                    table_name text not null,
                    row_key text not null,
                    data_json text not null,
                    primary key(table_name, row_key)
                );
                create table config_references (
                    source_table text not null,
                    source_key text not null,
                    field text not null,
                    target_table text not null,
                    target_key text not null,
                    valid integer not null
                );
                create table lua_scripts (source_name text);
                create table decoded_table_rows (data_json text);
                """
            )
            database.executemany(
                "insert into config_tables values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("Task", "LuaCfg.Task", 1, 1, "task.lua", "a", 2, "s1", 0),
                    ("Item", "LuaCfg.Item", 1, 2, "item.lua", "b", 1, "s2", 0),
                ],
            )
            database.executemany(
                "insert into config_rows values (?, ?, ?)",
                [
                    ("Task", "100", '{"ID":100,"AddReward":[]}'),
                    ("Task", "101", '{"ID":101,"AddReward":[{"Type":1,"ID":2,"Num":3}]}'),
                    ("Item", "2", '{"ID":2,"Name":42}'),
                ],
            )
            database.executemany(
                "insert into config_references values (?, ?, ?, ?, ?, ?)",
                [
                    ("Task", "100", "PreID", "Task", "101", 1),
                    ("Task", "101", "AddReward", "Item", "2", 1),
                    ("Task", "101", "LogicalTarget", "VirtualTable", "7", 1),
                ],
            )
            database.execute("insert into lua_scripts values ('Runtime.NotNeeded')")
            database.execute("insert into decoded_table_rows values ('{}')")
            database.commit()
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()
