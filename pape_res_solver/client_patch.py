from __future__ import annotations

import binascii
import hashlib
import lzma
import os
import struct
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

XOR_CONTAINER = 0x52
XOR_STRING = (0xAC, 0xCF)
ZIP_LZMA_METHOD = 14


@dataclass(frozen=True)
class _Member:
    info: zipfile.ZipInfo
    data: bytes
    compressed: bytes


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _raw_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    archive.fp.seek(info.header_offset)
    header = archive.fp.read(30)
    if len(header) != 30 or struct.unpack_from("<I", header)[0] != 0x04034B50:
        raise ValueError(f"invalid local ZIP header for {info.filename}")
    name_length, extra_length = struct.unpack_from("<HH", header, 26)
    archive.fp.seek(name_length + extra_length, os.SEEK_CUR)
    compressed = archive.fp.read(info.compress_size)
    if len(compressed) != info.compress_size:
        raise ValueError(f"truncated ZIP member: {info.filename}")
    return compressed


def _read_members(path: Path) -> list[_Member]:
    members: list[_Member] = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir() or Path(info.filename).name != info.filename:
                raise ValueError(f"unexpected XFileZip member: {info.filename}")
            compressed = _raw_member(archive, info)
            if info.compress_type == ZIP_LZMA_METHOD:
                data = lzma.decompress(compressed, format=lzma.FORMAT_AUTO)
            elif info.compress_type == zipfile.ZIP_STORED:
                data = compressed
            else:
                raise ValueError(
                    f"unsupported compression method {info.compress_type}: {info.filename}"
                )
            if len(data) != info.file_size:
                raise ValueError(f"uncompressed size mismatch: {info.filename}")
            if (binascii.crc32(data) & 0xFFFFFFFF) != info.CRC:
                raise ValueError(f"CRC mismatch: {info.filename}")
            members.append(_Member(info=info, data=data, compressed=compressed))
    if not members:
        raise ValueError(f"empty XFileZip archive: {path}")
    return members


def _encoded_lua_string(value: bytes) -> bytes:
    return bytes(byte ^ XOR_STRING[index & 1] for index, byte in enumerate(value))


def patch_nx_app_key(data: bytes, old_app_key: str, new_app_key: str) -> tuple[bytes, int]:
    try:
        old = old_app_key.encode("ascii")
        new = new_app_key.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("AppKeys must contain ASCII characters only") from error
    if not old:
        raise ValueError("old AppKey must not be empty")
    if len(old) != len(new):
        raise ValueError(
            f"AppKey byte lengths differ ({len(old)} != {len(new)}); in-place NX patch is unsafe"
        )
    decoded = bytearray(byte ^ XOR_CONTAINER for byte in data)
    encoded_old = _encoded_lua_string(old)
    encoded_new = _encoded_lua_string(new)
    offsets: list[int] = []
    position = 0
    while True:
        position = decoded.find(encoded_old, position)
        if position < 0:
            break
        offsets.append(position)
        position += len(encoded_old)
    for offset in offsets:
        decoded[offset : offset + len(encoded_old)] = encoded_new
    return bytes(byte ^ XOR_CONTAINER for byte in decoded), len(offsets)


def _dos_time_date(info: zipfile.ZipInfo) -> tuple[int, int]:
    year, month, day, hour, minute, second = info.date_time
    year = min(max(year, 1980), 2107)
    dos_time = (hour << 11) | (minute << 5) | (second // 2)
    dos_date = ((year - 1980) << 9) | (month << 5) | day
    return dos_time, dos_date


def _name_bytes(info: zipfile.ZipInfo) -> tuple[bytes, int]:
    try:
        return info.filename.encode("ascii"), (info.flag_bits & ~0x0008) | 0x0800
    except UnicodeEncodeError:
        return info.filename.encode("utf-8"), (info.flag_bits & ~0x0008) | 0x0800


def _write_game_zip(path: Path, members: list[_Member]) -> None:
    central: list[bytes] = []
    with path.open("wb") as output:
        for member in members:
            info = member.info
            name, flags = _name_bytes(info)
            compressed = member.compressed
            crc = binascii.crc32(member.data) & 0xFFFFFFFF
            dos_time, dos_date = _dos_time_date(info)
            local_offset = output.tell()
            extract_version = max(info.extract_version, 20)
            output.write(
                struct.pack(
                    "<IHHHHHIIIHH", 0x04034B50, extract_version, flags,
                    info.compress_type, dos_time, dos_date, crc, len(compressed),
                    len(member.data), len(name), 0,
                )
            )
            output.write(name)
            output.write(compressed)
            create_version = extract_version
            central.append(
                struct.pack(
                    "<IHHHHHHIIIHHHHHII", 0x02014B50, create_version,
                    extract_version, flags, info.compress_type, dos_time, dos_date,
                    crc, len(compressed), len(member.data), len(name), 0, 0, 0,
                    info.internal_attr, 0, local_offset,
                ) + name
            )
        central_offset = output.tell()
        for entry in central:
            output.write(entry)
        central_size = output.tell() - central_offset
        output.write(
            struct.pack(
                "<IHHHHIIH", 0x06054B50, 0, 0, len(central), len(central),
                central_size, central_offset, 0,
            )
        )


def patch_xfilezip_app_key(
    source: Path,
    output: Path,
    old_app_key: str,
    new_app_key: str,
    *,
    nx_output: Path | None = None,
    expected_matches: int = 1,
    force: bool = False,
) -> dict[str, object]:
    if expected_matches < 1:
        raise ValueError("expected_matches must be at least 1")
    source = source.resolve()
    output = output.resolve()
    if source == output:
        raise ValueError("output archive must differ from the input archive")
    targets = [output] + ([nx_output.resolve()] if nx_output is not None else [])
    for target in targets:
        if target.exists() and not force:
            raise FileExistsError(f"output exists; use --force: {target}")

    members = _read_members(source)
    patched_members: list[_Member] = []
    matches = 0
    patched_nx: list[tuple[str, bytes]] = []
    for member in members:
        data = member.data
        compressed = member.compressed
        if Path(member.info.filename).suffix.lower() == ".nx":
            data, count = patch_nx_app_key(data, old_app_key, new_app_key)
            matches += count
            if count:
                patched_nx.append((member.info.filename, data))
        if member.info.compress_type == ZIP_LZMA_METHOD:
            # Match the method-14/XZ framing already validated by the client.
            # Recompress the untouched NXF too, making the result deterministic.
            compressed = lzma.compress(data)
        patched_members.append(_Member(info=member.info, data=data, compressed=compressed))

    if matches != expected_matches:
        raise ValueError(f"expected {expected_matches} AppKey match(es), found {matches}")
    if nx_output is not None and len(patched_nx) != 1:
        raise ValueError(f"--nx-output requires exactly one patched NX member, found {len(patched_nx)}")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_archive: Path | None = None
    temporary_nx: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
        os.close(descriptor)
        temporary_archive = Path(temporary_name)
        _write_game_zip(temporary_archive, patched_members)
        verified = _read_members(temporary_archive)
        if [item.data for item in verified] != [item.data for item in patched_members]:
            raise RuntimeError("generated XFileZip verification failed")

        if nx_output is not None:
            nx_output = nx_output.resolve()
            nx_output.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{nx_output.name}.", dir=nx_output.parent
            )
            os.close(descriptor)
            temporary_nx = Path(temporary_name)
            temporary_nx.write_bytes(patched_nx[0][1])
        if force:
            os.replace(temporary_archive, output)
        else:
            temporary_archive.rename(output)
        temporary_archive = None
        if nx_output is not None and temporary_nx is not None:
            if force:
                os.replace(temporary_nx, nx_output)
            else:
                temporary_nx.rename(nx_output)
            temporary_nx = None
    finally:
        if temporary_archive is not None:
            temporary_archive.unlink(missing_ok=True)
        if temporary_nx is not None:
            temporary_nx.unlink(missing_ok=True)

    report: dict[str, object] = {
        "input": str(source),
        "output": str(output),
        "matches": matches,
        "patched_members": [name for name, _ in patched_nx],
        "input_sha256": _sha256(source.read_bytes()),
        "output_sha256": _sha256(output.read_bytes()),
    }
    if nx_output is not None:
        report["nx_output"] = str(nx_output)
        report["nx_sha256"] = _sha256(nx_output.read_bytes())
    return report


def _archive_candidates(source: Path) -> list[Path]:
    source = source.resolve()
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise FileNotFoundError(source)
    if source.name.casefold() == "xfilezip":
        candidates = list(source.glob("*.zip"))
    elif (source / "XFileZip").is_dir():
        candidates = list((source / "XFileZip").glob("*.zip"))
    else:
        candidates = list(source.glob("*.zip"))
        if not candidates:
            candidates = [
                path
                for path in source.rglob("*.zip")
                if path.parent.name.casefold() == "xfilezip"
            ]
    return sorted(candidates, key=lambda path: path.as_posix().casefold())


def find_app_key_archive(
    source: Path, old_app_key: str, new_app_key: str
) -> tuple[Path, str, int, int]:
    matches: list[tuple[Path, str, int]] = []
    inspected = 0
    for candidate in _archive_candidates(source):
        try:
            members = _read_members(candidate)
        except (OSError, ValueError, zipfile.BadZipFile, lzma.LZMAError):
            continue
        inspected += 1
        for member in members:
            if Path(member.info.filename).suffix.casefold() != ".nx":
                continue
            _, count = patch_nx_app_key(member.data, old_app_key, new_app_key)
            if count:
                matches.append((candidate, member.info.filename, count))
    if not matches:
        raise ValueError(
            f"AppKey was not found in {inspected} readable XFileZip archive(s) "
            f"from {source.resolve()}"
        )
    if len(matches) != 1:
        locations = ", ".join(f"{path.name}:{member}" for path, member, _ in matches)
        raise ValueError(f"AppKey is ambiguous across {len(matches)} NX members: {locations}")
    archive, member_name, count = matches[0]
    return archive, member_name, count, inspected


def auto_patch_xfilezip_app_key(
    source: Path,
    output_directory: Path,
    old_app_key: str,
    new_app_key: str,
    *,
    expected_matches: int = 1,
    force: bool = False,
) -> dict[str, object]:
    archive, nx_name, discovered_matches, inspected = find_app_key_archive(
        source, old_app_key, new_app_key
    )
    if discovered_matches != expected_matches:
        raise ValueError(
            f"expected {expected_matches} AppKey match(es), found {discovered_matches}"
        )
    output_directory = output_directory.resolve()
    report = patch_xfilezip_app_key(
        archive,
        output_directory / archive.name,
        old_app_key,
        new_app_key,
        nx_output=output_directory / Path(nx_name).name,
        expected_matches=expected_matches,
        force=force,
    )
    report["mode"] = "auto"
    report["searched_from"] = str(source.resolve())
    report["archives_inspected"] = inspected
    return report
