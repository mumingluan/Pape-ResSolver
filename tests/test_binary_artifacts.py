import json
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

from pape_res_solver.artifacts import (
    _DIRTYWORDS_TRANSFORM,
    _decode_dirtywords,
    _decode_xfilezip_index,
)


class BinaryArtifactTests(unittest.TestCase):
    def test_dirtywords_decode_restores_sqlite_and_words(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plain = root / "plain.sqlite"
            connection = sqlite3.connect(plain)
            try:
                connection.execute("create table DirtyWords(id integer primary key, value blob)")
                connection.execute("insert into DirtyWords values (?, ?)", (1, b'\x1a[3,"abc"]'))
                connection.commit()
            finally:
                connection.close()
            raw = plain.read_bytes()
            encrypted = root / "DirtyWords.db"
            encrypted.write_bytes(raw.translate(_DIRTYWORDS_TRANSFORM))

            result = _decode_dirtywords(encrypted, root / "decoded.sqlite", root / "words.jsonl")

            self.assertEqual(result["integrity"], "ok")
            self.assertEqual(result["rows"], 1)
            self.assertEqual(result["words"], 1)
            self.assertEqual(json.loads((root / "words.jsonl").read_text(encoding="utf-8"))["words"], ["abc"])

    def test_xfilezip_index_decode_restores_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plain_records = [
                b"1.0.0", b"blob.nx.nxf", b".nx", b".nxf", struct.pack("<I", 7),
                struct.pack("<I", 1),
                struct.pack("<I", 123), struct.pack("<I", 1), struct.pack("<I", 2), struct.pack("<I", 3),
                struct.pack("<I", 1), struct.pack("<I", 123), b"/Locale/zh-CN/123",
            ]
            data = b"".join(struct.pack("<I", len(record)) + record for record in plain_records)
            encrypted = root / "210201614.bin"
            encrypted.write_bytes(bytes(byte ^ 0x0C for byte in data))

            result = _decode_xfilezip_index(encrypted, root / "paths.jsonl")

            self.assertEqual(result["records"], len(plain_records))
            self.assertEqual(result["path_count"], 1)
            self.assertEqual(json.loads((root / "paths.jsonl").read_text(encoding="utf-8"))["path"], "/Locale/zh-CN/123")


if __name__ == "__main__":
    unittest.main()
