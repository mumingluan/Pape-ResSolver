import json
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

from pape_res_solver.multilanguage import (
    LANGUAGE_SCHEMA,
    export_multilanguage_sqlite,
    extract_int32_texts,
)


def field(payload: bytes) -> bytes:
    return struct.pack("<I", len(payload)) + payload


def int32_fixture(rows: list[tuple[int, str]], dependencies: list[int] | None = None) -> tuple[bytes, bytes]:
    dependencies = dependencies or []
    unique_texts: list[str] = []
    for _, text in rows:
        if text not in unique_texts:
            unique_texts.append(text)
    nx_parts = []
    nx_offsets = {}
    nx_position = 0
    for text in unique_texts:
        encoded = text.encode("utf-8")
        nx_offsets[text] = nx_position
        block = struct.pack("<I", len(encoded)) + encoded
        nx_parts.append(block)
        nx_position += len(block)
    nx = b"".join(nx_parts)

    header_payloads = [
        struct.pack("<I", len(rows)),
        struct.pack("<I", 0),
        b"Int32",
        struct.pack("<I", len(dependencies)),
        *(struct.pack("<I", value) for value in dependencies),
    ]
    pointer_field_index = len(header_payloads)
    header_payloads.append(b"\0\0\0\0")
    header_size = sum(4 + len(payload) for payload in header_payloads)
    offset_starts = {
        text: header_size + index * 8 for index, text in enumerate(unique_texts)
    }
    metadata_start = header_size + len(unique_texts) * 8
    header_payloads[pointer_field_index] = struct.pack("<I", metadata_start)
    nxf = b"".join(field(payload) for payload in header_payloads)
    nxf += b"".join(field(struct.pack("<I", nx_offsets[text])) for text in unique_texts)
    for index, (text_id, text) in enumerate(rows):
        record_start = metadata_start + index * 32
        nxf += field(struct.pack("<i", text_id))
        nxf += field(struct.pack("<I", offset_starts[text]))
        nxf += field(struct.pack("<I", 0xFFFFFFFF))
        nxf += field(struct.pack("<I", record_start))
    return nx, nxf


class MultilanguageTests(unittest.TestCase):
    def test_extracts_int32_keys_and_shared_utf8_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nx, nxf = int32_fixture([(10, "月卡"), (11, "月卡"), (12, "Hello")], [7, 8])
            nx_path = root / "a.nx.dec"
            nxf_path = root / "a.nxf.dec"
            nx_path.write_bytes(nx)
            nxf_path.write_bytes(nxf)

            rows, details = extract_int32_texts(nx_path, nxf_path)

            self.assertEqual(rows, [(10, "月卡"), (11, "月卡"), (12, "Hello")])
            self.assertEqual(details["unique_values"], 2)
            self.assertEqual(details["dependencies"], [7, 8])

    def test_exports_multiple_resource_sets_to_independent_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "normalized"
            output = Path(temporary) / "languages.sqlite"
            (root / "multilanguage").mkdir(parents=True)
            (root / "containers").mkdir()
            (root / "indexes").mkdir()
            sets = []
            for resource_set_id, package_id, text in (
                (1000000000001, 10, "鎏金卡"),
                (1000000000012, 20, "Monthly Pass"),
            ):
                nx, nxf = int32_fixture([(377066, text)])
                (root / f"containers/{package_id}.nx.dec").write_bytes(nx)
                (root / f"indexes/{package_id}.nxf.dec").write_bytes(nxf)
                sets.append(
                    {
                        "resource_set_id": resource_set_id,
                        "key": str(resource_set_id),
                        "package_count": 1,
                        "packages": [
                            {
                                "package_id": package_id,
                                "kind": "data",
                                "source_path": f"XFileZip/{package_id}.zip",
                                "decoded_nx_path": f"containers/{package_id}.nx.dec",
                                "decoded_nxf_path": f"indexes/{package_id}.nxf.dec",
                            }
                        ],
                    }
                )
            (root / "multilanguage/manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "pape-res-multilanguage-input-v1",
                        "version": "1.7.test",
                        "platform": "android",
                        "resource_sets": sets,
                        "failures": [],
                    }
                ),
                encoding="utf-8",
            )
            output.write_bytes(b"replace me")

            report = export_multilanguage_sqlite(root, output)

            self.assertEqual(report["schema"], LANGUAGE_SCHEMA)
            self.assertEqual(report["counts"], {"resource_sets": 2, "packages": 2, "texts": 2})
            database = sqlite3.connect(output)
            try:
                self.assertEqual(
                    database.execute(
                        "select resource_set_id, text from localized_text order by resource_set_id"
                    ).fetchall(),
                    [(1000000000001, "鎏金卡"), (1000000000012, "Monthly Pass")],
                )
                metadata = dict(database.execute("select key, value from language_metadata"))
                self.assertEqual(metadata["schema"], LANGUAGE_SCHEMA)
                self.assertEqual(database.execute("pragma integrity_check").fetchone()[0], "ok")
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
