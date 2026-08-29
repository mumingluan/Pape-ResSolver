from __future__ import annotations

from dataclasses import dataclass


class LuaBytecodeError(ValueError):
    pass


LUA_SIGNATURE = b"\x1bLua"
LUAC_DATA = b"\x19\x93\r\n\x1a\n"
LUAC_INT = 0x5678


@dataclass(frozen=True, slots=True)
class LuaChunkHeader:
    version: int
    format: int
    cint_size: int
    size_t_size: int
    instruction_size: int
    integer_size: int
    number_size: int
    endian: str


class _ChunkConverter:
    def __init__(self, data: bytes, target_size_t: int) -> None:
        self.data = data
        self.offset = 0
        self.output = bytearray()
        self.target_size_t = target_size_t
        self.header = self._header()

    def _read(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.data):
            raise LuaBytecodeError(f"truncated chunk at offset {self.offset}")
        value = self.data[self.offset : end]
        self.offset = end
        return value

    def _copy(self, size: int) -> bytes:
        value = self._read(size)
        self.output.extend(value)
        return value

    def _uint(self, size: int) -> int:
        return int.from_bytes(self._read(size), self.header.endian, signed=False)

    def _write_uint(self, value: int, size: int) -> None:
        self.output.extend(value.to_bytes(size, self.header.endian, signed=False))

    def _header(self) -> LuaChunkHeader:
        if self._copy(4) != LUA_SIGNATURE:
            raise LuaBytecodeError("not a Lua binary chunk")
        version = self._copy(1)[0]
        format_number = self._copy(1)[0]
        if version != 0x53:
            raise LuaBytecodeError(f"unsupported Lua bytecode version 0x{version:02x}")
        if self._copy(6) != LUAC_DATA:
            raise LuaBytecodeError("invalid Lua bytecode signature data")
        cint_size = self._copy(1)[0]
        old_size_t = self._read(1)[0]
        self.output.append(self.target_size_t)
        instruction_size = self._copy(1)[0]
        integer_size = self._copy(1)[0]
        number_size = self._copy(1)[0]
        integer_raw = self._read(integer_size)
        little = int.from_bytes(integer_raw, "little", signed=False)
        big = int.from_bytes(integer_raw, "big", signed=False)
        if little == LUAC_INT:
            endian = "little"
        elif big == LUAC_INT:
            endian = "big"
        else:
            raise LuaBytecodeError("cannot determine Lua chunk endianness")
        self.output.extend(integer_raw)
        self.output.extend(self._read(number_size))
        return LuaChunkHeader(
            version=version,
            format=format_number,
            cint_size=cint_size,
            size_t_size=old_size_t,
            instruction_size=instruction_size,
            integer_size=integer_size,
            number_size=number_size,
            endian=endian,
        )

    def convert(self) -> bytes:
        self._copy(1)  # main closure upvalue count
        self._function()
        if self.offset != len(self.data):
            raise LuaBytecodeError(f"unparsed trailing bytes: {len(self.data) - self.offset}")
        return bytes(self.output)

    def _count(self) -> int:
        raw = self._copy(self.header.cint_size)
        return int.from_bytes(raw, self.header.endian, signed=False)

    def _string(self) -> None:
        marker = self._copy(1)[0]
        if marker == 0:
            return
        if marker == 0xFF:
            size = self._uint(self.header.size_t_size)
            self._write_uint(size, self.target_size_t)
        else:
            size = marker
        if size == 0:
            return
        self._copy(size - 1)

    def _function(self) -> None:
        self._string()
        self._copy(self.header.cint_size * 2)
        self._copy(3)
        code_count = self._count()
        self._copy(code_count * self.header.instruction_size)
        constant_count = self._count()
        for _ in range(constant_count):
            tag = self._copy(1)[0]
            if tag == 0:  # nil
                continue
            if tag == 1:  # boolean
                self._copy(1)
            elif tag == 3:  # float
                self._copy(self.header.number_size)
            elif tag == 19:  # integer
                self._copy(self.header.integer_size)
            elif tag in (4, 20):  # short/long string
                self._string()
            else:
                raise LuaBytecodeError(f"unknown constant tag {tag} at offset {self.offset - 1}")
        upvalue_count = self._count()
        self._copy(upvalue_count * 2)
        prototype_count = self._count()
        for _ in range(prototype_count):
            self._function()
        line_count = self._count()
        self._copy(line_count * self.header.cint_size)
        local_count = self._count()
        for _ in range(local_count):
            self._string()
            self._copy(self.header.cint_size * 2)
        upvalue_name_count = self._count()
        for _ in range(upvalue_name_count):
            self._string()


def convert_lua53_size_t(data: bytes, target_size_t: int = 8) -> tuple[bytes, LuaChunkHeader]:
    if target_size_t not in (4, 8):
        raise ValueError("target_size_t must be 4 or 8")
    converter = _ChunkConverter(data, target_size_t)
    return converter.convert(), converter.header
