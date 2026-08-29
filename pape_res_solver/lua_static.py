from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


class LuaParseError(ValueError):
    pass


@dataclass(slots=True)
class LuaRef:
    name: str


@dataclass(slots=True)
class LuaExpr:
    operator: str
    operands: tuple[Any, ...]


@dataclass(slots=True)
class LuaCall:
    function: Any
    arguments: list[Any]


@dataclass
class LuaTable:
    values: dict[Any, Any] = field(default_factory=dict)
    array_extent: int = 0

    def set(self, key: Any, value: Any) -> None:
        if isinstance(key, int) and key > 0:
            self.array_extent = max(self.array_extent, key)
        if value is None:
            self.values.pop(key, None)
        else:
            self.values[key] = value

    def get(self, key: Any, default: Any = None) -> Any:
        return self.values.get(key, default)


@dataclass(slots=True)
class Token:
    kind: str
    value: Any
    offset: int
    line: int
    column: int


_NUMBER = re.compile(
    r"(?:0[xX][0-9a-fA-F]+(?:\.[0-9a-fA-F]*)?(?:[pP][+-]?\d+)?|"
    r"(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)"
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Lexer:
    def __init__(self, source: str, source_name: str = "<lua>") -> None:
        self.source = source.lstrip("\ufeff")
        self.source_name = source_name
        self.length = len(self.source)
        self.offset = 0
        self.line = 1
        self.column = 1

    def _advance(self, count: int = 1) -> str:
        text = self.source[self.offset : self.offset + count]
        self.offset += count
        lines = text.count("\n")
        if lines:
            self.line += lines
            self.column = len(text.rsplit("\n", 1)[-1]) + 1
        else:
            self.column += count
        return text

    def _long_bracket(self, offset: int) -> tuple[int, int] | None:
        if offset >= self.length or self.source[offset] != "[":
            return None
        cursor = offset + 1
        while cursor < self.length and self.source[cursor] == "=":
            cursor += 1
        if cursor < self.length and self.source[cursor] == "[":
            return cursor - offset - 1, cursor + 1
        return None

    def _read_long_string(self) -> str:
        opening = self._long_bracket(self.offset)
        if opening is None:
            raise self.error("invalid long string")
        equals, content_start = opening
        opening_size = content_start - self.offset
        self._advance(opening_size)
        close = "]" + ("=" * equals) + "]"
        end = self.source.find(close, self.offset)
        if end < 0:
            raise self.error("unterminated long string")
        value = self.source[self.offset : end]
        self._advance(end - self.offset + len(close))
        value = value.removeprefix("\n")
        return value

    def _read_quoted_string(self) -> str:
        quote = self.source[self.offset]
        self._advance()
        output: list[str] = []
        escapes = {
            "a": "\a",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "v": "\v",
            "\\": "\\",
            '"': '"',
            "'": "'",
        }
        while self.offset < self.length:
            char = self.source[self.offset]
            if char == quote:
                self._advance()
                return "".join(output)
            if char != "\\":
                output.append(self._advance())
                continue
            self._advance()
            if self.offset >= self.length:
                break
            escaped = self.source[self.offset]
            if escaped in escapes:
                output.append(escapes[escaped])
                self._advance()
            elif escaped == "z":
                self._advance()
                while self.offset < self.length and self.source[self.offset].isspace():
                    self._advance()
            elif escaped == "x":
                self._advance()
                digits = self._advance(2)
                output.append(chr(int(digits, 16)))
            elif escaped == "u" and self.source[self.offset : self.offset + 2] == "u{":
                self._advance(2)
                end = self.source.find("}", self.offset)
                if end < 0:
                    raise self.error("unterminated unicode escape")
                output.append(chr(int(self.source[self.offset : end], 16)))
                self._advance(end - self.offset + 1)
            elif escaped.isdigit():
                match = re.match(r"\d{1,3}", self.source[self.offset :])
                assert match is not None
                output.append(chr(int(match.group(0), 10)))
                self._advance(len(match.group(0)))
            elif escaped in "\r\n":
                if escaped == "\r" and self.source[self.offset + 1 : self.offset + 2] == "\n":
                    self._advance(2)
                else:
                    self._advance()
                output.append("\n")
            else:
                output.append(self._advance())
        raise self.error("unterminated quoted string")

    def _skip_space_and_comments(self) -> None:
        while self.offset < self.length:
            if self.source[self.offset].isspace():
                self._advance()
                continue
            if self.source.startswith("--", self.offset):
                self._advance(2)
                if self._long_bracket(self.offset) is not None:
                    self._read_long_string()
                else:
                    end = self.source.find("\n", self.offset)
                    self._advance((self.length if end < 0 else end) - self.offset)
                continue
            break

    def next_token(self) -> Token:
        self._skip_space_and_comments()
        start, line, column = self.offset, self.line, self.column
        if start >= self.length:
            return Token("EOF", None, start, line, column)
        char = self.source[start]
        if char in "'\"":
            return Token("STRING", self._read_quoted_string(), start, line, column)
        if self._long_bracket(start) is not None:
            return Token("STRING", self._read_long_string(), start, line, column)
        number = _NUMBER.match(self.source, start)
        if number:
            raw = number.group(0)
            self._advance(len(raw))
            if raw.lower().startswith("0x"):
                value = float.fromhex(raw) if ("." in raw or "p" in raw.lower()) else int(raw, 16)
            else:
                value = float(raw) if any(marker in raw for marker in ".eE") else int(raw, 10)
            return Token("NUMBER", value, start, line, column)
        name = _NAME.match(self.source, start)
        if name:
            raw = name.group(0)
            self._advance(len(raw))
            return Token(
                raw if raw in {"local", "return", "nil", "true", "false", "and", "or", "not"} else "NAME",
                raw,
                start,
                line,
                column,
            )
        for operator in ("...", "..", "//", "<<", ">>", "<=", ">=", "==", "~=", "::"):
            if self.source.startswith(operator, start):
                self._advance(len(operator))
                return Token(operator, operator, start, line, column)
        if char in "{}[](),;=.+-*/%^#<>:&|~":
            self._advance()
            return Token(char, char, start, line, column)
        raise self.error(f"unexpected character {char!r}")

    def error(self, message: str) -> LuaParseError:
        return LuaParseError(f"{self.source_name}:{self.line}:{self.column}: {message}")


_BINDING_POWER = {
    "or": (1, 2),
    "and": (2, 3),
    "<": (3, 4),
    ">": (3, 4),
    "<=": (3, 4),
    ">=": (3, 4),
    "~=": (3, 4),
    "==": (3, 4),
    "|": (4, 5),
    "~": (5, 6),
    "&": (6, 7),
    "<<": (7, 8),
    ">>": (7, 8),
    "..": (8, 8),
    "+": (9, 10),
    "-": (9, 10),
    "*": (10, 11),
    "/": (10, 11),
    "//": (10, 11),
    "%": (10, 11),
    "^": (12, 12),
}


class StaticLuaParser:
    def __init__(self, source: str, source_name: str = "<lua>") -> None:
        self.lexer = Lexer(source, source_name)
        self.current = self.lexer.next_token()
        self.env: dict[str, Any] = {}
        self.unresolved: list[str] = []

    def advance(self) -> Token:
        previous = self.current
        self.current = self.lexer.next_token()
        return previous

    def accept(self, kind: str) -> Token | None:
        if self.current.kind == kind:
            return self.advance()
        return None

    def expect(self, kind: str) -> Token:
        if self.current.kind != kind:
            raise self.error(f"expected {kind!r}, got {self.current.kind!r}")
        return self.advance()

    def error(self, message: str) -> LuaParseError:
        token = self.current
        return LuaParseError(f"{self.lexer.source_name}:{token.line}:{token.column}: {message}")

    def parse(self) -> Any:
        result: Any = None
        while self.current.kind != "EOF":
            if self.accept(";"):
                continue
            if self.accept("local"):
                self._parse_local()
                continue
            if self.accept("return"):
                result = self.parse_expression()
                while self.accept(","):
                    self.parse_expression()
                return result
            if self.current.kind == "NAME":
                self._parse_assignment()
                continue
            raise self.error("unsupported statement in static data chunk")
        return result

    def _parse_local(self) -> None:
        names = [self.expect("NAME").value]
        while self.accept(","):
            names.append(self.expect("NAME").value)
        values: list[Any] = []
        if self.accept("="):
            values.append(self.parse_expression())
            while self.accept(","):
                values.append(self.parse_expression())
        for index, name in enumerate(names):
            self.env[name] = values[index] if index < len(values) else None

    def _parse_assignment(self) -> None:
        targets = [self._parse_target()]
        while self.accept(","):
            targets.append(self._parse_target())
        self.expect("=")
        values = [self.parse_expression()]
        while self.accept(","):
            values.append(self.parse_expression())
        for index, target in enumerate(targets):
            self._assign(target, values[index] if index < len(values) else None)

    def _parse_target(self) -> tuple[str, list[Any]]:
        name = self.expect("NAME").value
        accessors: list[Any] = []
        while True:
            if self.accept("."):
                accessors.append(self.expect("NAME").value)
            elif self.accept("["):
                accessors.append(self.parse_expression())
                self.expect("]")
            else:
                break
        return name, accessors

    def _assign(self, target: tuple[str, list[Any]], value: Any) -> None:
        name, accessors = target
        if not accessors:
            self.env[name] = value
            return
        current = self.env.get(name)
        if not isinstance(current, LuaTable):
            current = LuaTable()
            self.env[name] = current
        for key in accessors[:-1]:
            child = current.get(key)
            if not isinstance(child, LuaTable):
                child = LuaTable()
                current.set(key, child)
            current = child
        current.set(accessors[-1], value)

    def parse_expression(self, minimum_bp: int = 0) -> Any:
        token = self.advance()
        if token.kind == "NUMBER" or token.kind == "STRING":
            left: Any = token.value
        elif token.kind == "nil":
            left = None
        elif token.kind == "true":
            left = True
        elif token.kind == "false":
            left = False
        elif token.kind == "NAME":
            left = self.env.get(token.value, LuaRef(token.value))
        elif token.kind == "{":
            left = self._parse_table_body()
        elif token.kind == "(":
            left = self.parse_expression()
            self.expect(")")
        elif token.kind in {"-", "+", "not", "#", "~"}:
            operand = self.parse_expression(11)
            left = self._unary(token.kind, operand)
        else:
            raise self.error(f"unsupported expression starting with {token.kind!r}")

        while True:
            if self.accept("."):
                key = self.expect("NAME").value
                left = self._index(left, key)
                continue
            if self.accept("["):
                key = self.parse_expression()
                self.expect("]")
                left = self._index(left, key)
                continue
            if self.accept("("):
                arguments: list[Any] = []
                if self.current.kind != ")":
                    arguments.append(self.parse_expression())
                    while self.accept(","):
                        arguments.append(self.parse_expression())
                self.expect(")")
                left = self._call(left, arguments)
                continue
            binding = _BINDING_POWER.get(self.current.kind)
            if binding is None or binding[0] < minimum_bp:
                break
            operator = self.advance().kind
            right = self.parse_expression(binding[1])
            left = self._binary(operator, left, right)
        return left

    def _parse_table_body(self) -> LuaTable:
        table = LuaTable()
        next_index = 1
        while self.current.kind != "}":
            if self.current.kind == "[":
                self.advance()
                key = self.parse_expression()
                self.expect("]")
                self.expect("=")
                table.set(key, self.parse_expression())
            elif self.current.kind == "NAME":
                saved = self.current
                self.advance()
                if self.accept("="):
                    table.set(saved.value, self.parse_expression())
                else:
                    value = self.env.get(saved.value, LuaRef(saved.value))
                    value = self._parse_postfix_and_binary(value)
                    table.set(next_index, value)
                    next_index += 1
            else:
                table.set(next_index, self.parse_expression())
                next_index += 1
            if not (self.accept(",") or self.accept(";")) and self.current.kind != "}":
                raise self.error("expected table field separator")
        self.expect("}")
        table.array_extent = max(table.array_extent, next_index - 1)
        return table

    def _parse_postfix_and_binary(self, left: Any, minimum_bp: int = 0) -> Any:
        while True:
            if self.accept("."):
                left = self._index(left, self.expect("NAME").value)
                continue
            if self.accept("["):
                key = self.parse_expression()
                self.expect("]")
                left = self._index(left, key)
                continue
            if self.accept("("):
                args: list[Any] = []
                if self.current.kind != ")":
                    args.append(self.parse_expression())
                    while self.accept(","):
                        args.append(self.parse_expression())
                self.expect(")")
                left = self._call(left, args)
                continue
            binding = _BINDING_POWER.get(self.current.kind)
            if binding is None or binding[0] < minimum_bp:
                break
            operator = self.advance().kind
            right = self.parse_expression(binding[1])
            left = self._binary(operator, left, right)
        return left

    def _index(self, value: Any, key: Any) -> Any:
        if isinstance(value, LuaTable):
            return value.get(key)
        if isinstance(value, LuaRef):
            return LuaRef(f"{value.name}.{key}")
        return LuaExpr("index", (value, key))

    def _call(self, function: Any, arguments: list[Any]) -> Any:
        name = function.name if isinstance(function, LuaRef) else ""
        if name == "setmetatable" and arguments:
            return arguments[0]
        if name == "tonumber" and arguments and isinstance(arguments[0], (str, int, float)):
            try:
                return int(arguments[0])
            except (TypeError, ValueError):
                try:
                    return float(arguments[0])
                except (TypeError, ValueError):
                    pass
        if name == "tostring" and arguments:
            return str(arguments[0])
        return LuaCall(function, arguments)

    def _unary(self, operator: str, value: Any) -> Any:
        if operator == "-" and isinstance(value, (int, float)):
            return -value
        if operator == "+" and isinstance(value, (int, float)):
            return value
        if operator == "not":
            return not bool(value)
        if operator == "~" and isinstance(value, int):
            return ~value
        if operator == "#" and isinstance(value, LuaTable):
            return value.array_extent
        return LuaExpr(operator, (value,))

    def _binary(self, operator: str, left: Any, right: Any) -> Any:
        try:
            if operator == "+":
                return left + right
            if operator == "-":
                return left - right
            if operator == "*":
                return left * right
            if operator == "/":
                return left / right
            if operator == "//":
                return left // right
            if operator == "%":
                return left % right
            if operator == "^":
                return left**right
            if operator == "..":
                return f"{left}{right}"
            if operator == "and":
                return right if left else left
            if operator == "or":
                return left if left else right
            if operator == "==":
                return left == right
            if operator == "~=":
                return left != right
            if operator == "<":
                return left < right
            if operator == ">":
                return left > right
            if operator == "<=":
                return left <= right
            if operator == ">=":
                return left >= right
            if operator == "&":
                return left & right
            if operator == "|":
                return left | right
            if operator == "~":
                return left ^ right
            if operator == "<<":
                return left << right
            if operator == ">>":
                return left >> right
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            pass
        return LuaExpr(operator, (left, right))


def parse_static_lua(source: str, source_name: str = "<lua>") -> Any:
    return StaticLuaParser(source, source_name).parse()
