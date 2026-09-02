import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pape_res_solver.artifacts import _promote_x3_runtime_table
from pape_res_solver.sqlite_trim import trim_sqlite


class X3RuntimeTableTests(unittest.TestCase):
    def test_promoted_x3_records_survive_runtime_trim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_json = root / "tables" / "x3_msgpack" / "1" / "000045_WeaponLogicConfigs.json"
            source_json.parent.mkdir(parents=True)
            document = {
                "decoded": {
                    "record_type": "WeaponLogicConfig",
                    "records": {
                        "1901": {"ID": 1901, "AttackIDs": [190101, 190102]},
                    },
                }
            }
            source_json.write_text(json.dumps(document), encoding="utf-8")
            full = root / "resources.sqlite"
            database = sqlite3.connect(full)
            try:
                self._create_schema(database)
                item = _promote_x3_runtime_table(
                    source=source_json,
                    source_relative=source_json.relative_to(root),
                    value=document,
                    output=root,
                    database=database,
                    package_id=1,
                    entry_index=45,
                    table_name="WeaponLogicConfigs",
                )
                database.commit()
            finally:
                database.close()

            self.assertEqual(item["table"], "X3WeaponLogicConfigs")
            runtime = root / "runtime.sqlite"
            trim_sqlite(full, runtime)
            database = sqlite3.connect(runtime)
            try:
                row = database.execute(
                    "select data_json from config_rows where table_name=? and row_key=?",
                    ("X3WeaponLogicConfigs", "1901"),
                ).fetchone()
                self.assertEqual(json.loads(row[0])["AttackIDs"], [190101, 190102])
            finally:
                database.close()

    @staticmethod
    def _create_schema(database: sqlite3.Connection) -> None:
        database.executescript(
            """
            create table config_tables (
                table_name text primary key, source_name text not null,
                package_id integer not null, entry_index integer not null,
                source_path text not null, sha256 text not null,
                row_count integer not null, schema_fingerprint text,
                unresolved_values integer not null
            );
            create table config_rows (
                table_name text not null, row_key text not null, data_json text not null,
                primary key(table_name, row_key)
            );
            """
        )


if __name__ == "__main__":
    unittest.main()
