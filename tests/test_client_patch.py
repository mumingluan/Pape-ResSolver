import binascii
import lzma
import tempfile
import unittest
import zipfile
from pathlib import Path

from pape_res_solver.client_patch import (
    _Member,
    _read_members,
    _write_game_zip,
    auto_patch_xfilezip_app_key,
    find_app_key_archive,
    patch_nx_app_key,
    patch_xfilezip_app_key,
)


def nx_fixture(app_key: str) -> bytes:
    plain = b"prefix" + bytes(
        byte ^ (0xAC if index % 2 == 0 else 0xCF)
        for index, byte in enumerate(app_key.encode("ascii"))
    ) + b"suffix"
    return bytes(byte ^ 0x52 for byte in plain)


def member(name: str, data: bytes) -> _Member:
    info = zipfile.ZipInfo(name, (2026, 9, 3, 12, 0, 0))
    info.compress_type = 14
    info.extract_version = 63
    info.create_version = 63
    info.CRC = binascii.crc32(data) & 0xFFFFFFFF
    info.file_size = len(data)
    compressed = lzma.compress(data, format=lzma.FORMAT_ALONE)
    info.compress_size = len(compressed)
    return _Member(info, data, compressed)


class ClientPatchTests(unittest.TestCase):
    def test_patches_container_encoding_without_changing_size(self) -> None:
        original = nx_fixture("old-app-key-0001")
        patched, matches = patch_nx_app_key(original, "old-app-key-0001", "new-app-key-0002")
        self.assertEqual(matches, 1)
        self.assertEqual(len(patched), len(original))
        self.assertEqual(patch_nx_app_key(patched, "old-app-key-0001", "new-app-key-0002")[1], 0)
        self.assertEqual(patch_nx_app_key(patched, "new-app-key-0002", "old-app-key-0001")[1], 1)

    def test_rebuilds_game_lzma_zip_and_emits_runtime_nx(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "123.zip"
            output = root / "patched" / "123.zip"
            nx_output = root / "patched" / "123.nx"
            original_nx = nx_fixture("old-app-key-0001")
            nxf = b"index-data"
            _write_game_zip(source, [member("123.nx", original_nx), member("123.nxf", nxf)])
            source_before = source.read_bytes()

            report = patch_xfilezip_app_key(
                source, output, "old-app-key-0001", "new-app-key-0002", nx_output=nx_output
            )

            self.assertEqual(report["matches"], 1)
            self.assertEqual(source.read_bytes(), source_before)
            extracted = _read_members(output)
            self.assertEqual(extracted[0].data, nx_output.read_bytes())
            self.assertEqual(extracted[1].data, nxf)
            self.assertEqual(
                patch_nx_app_key(extracted[0].data, "new-app-key-0002", "old-app-key-0001")[1], 1
            )

    def test_rejects_unsafe_or_ambiguous_patch(self) -> None:
        with self.assertRaisesRegex(ValueError, "lengths differ"):
            patch_nx_app_key(nx_fixture("same-length-key1"), "same-length-key1", "short")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "123.zip"
            _write_game_zip(source, [member("123.nx", nx_fixture("old-app-key-0001"))])
            with self.assertRaisesRegex(ValueError, "expected 2.*found 1"):
                patch_xfilezip_app_key(
                    source, root / "patched.zip", "old-app-key-0001", "new-app-key-0002",
                    expected_matches=2,
                )

    def test_auto_finds_renamed_package_and_derives_output_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            xfilezip = root / "resources" / "XFileZip"
            xfilezip.mkdir(parents=True)
            _write_game_zip(xfilezip / "111.zip", [member("111.nx", nx_fixture("other-app-key-01"))])
            _write_game_zip(
                xfilezip / "987654321.zip",
                [
                    member("987654321.nx", nx_fixture("old-app-key-0001")),
                    member("987654321.nxf", b"index"),
                ],
            )

            archive, nx_name, matches, inspected = find_app_key_archive(
                root / "resources", "old-app-key-0001", "new-app-key-0002"
            )
            self.assertEqual(archive.name, "987654321.zip")
            self.assertEqual(nx_name, "987654321.nx")
            self.assertEqual(matches, 1)
            self.assertEqual(inspected, 2)

            output = root / "patched"
            report = auto_patch_xfilezip_app_key(
                root / "resources", output, "old-app-key-0001", "new-app-key-0002"
            )
            self.assertEqual(report["mode"], "auto")
            self.assertTrue((output / "987654321.zip").is_file())
            self.assertTrue((output / "987654321.nx").is_file())
            self.assertFalse((output / "111.zip").exists())


if __name__ == "__main__":
    unittest.main()
