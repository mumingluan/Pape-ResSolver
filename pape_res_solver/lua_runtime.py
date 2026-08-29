from __future__ import annotations

from typing import Any

from lupa.lua53 import LuaRuntime, lua_type

from .lua_bytecode import LuaChunkHeader, convert_lua53_size_t
from .lua_static import LuaTable


class Lua53ConfigRuntime:
    def __init__(self, max_memory: int = 512 * 1024 * 1024) -> None:
        self.runtime = LuaRuntime(
            encoding=None,
            register_eval=False,
            register_builtins=False,
            max_memory=max_memory,
        )
        self.loader = self.runtime.eval(
            b"""
            function(chunk)
              local function tagged(kind, names, ...)
                local values = {...}
                local result = { ["$type"] = kind }
                for index, name in ipairs(names) do result[name] = values[index] end
                return result
              end
              local function proxy(path)
                local value = { ["$ref"] = path }
                return setmetatable(value, {
                  __index = function(_, key) return proxy(path .. "." .. tostring(key)) end,
                  __call = function(_, ...)
                    return { ["$call"] = path, args = {...} }
                  end
                })
              end
              local env = {
                Vector2 = function(...) return tagged("Vector2", {"x", "y"}, ...) end,
                Vector3 = function(...) return tagged("Vector3", {"x", "y", "z"}, ...) end,
                Vector4 = function(...) return tagged("Vector4", {"x", "y", "z", "w"}, ...) end,
                Vector2Int = function(...) return tagged("Vector2Int", {"x", "y"}, ...) end,
                Vector3Int = function(...) return tagged("Vector3Int", {"x", "y", "z"}, ...) end,
                Quaternion = function(...) return tagged("Quaternion", {"x", "y", "z", "w"}, ...) end,
                Color = function(...) return tagged("Color", {"r", "g", "b", "a"}, ...) end,
                Color32 = function(...) return tagged("Color32", {"r", "g", "b", "a"}, ...) end,
                Rect = function(...) return tagged("Rect", {"x", "y", "width", "height"}, ...) end,
                setmetatable = setmetatable,
                getmetatable = getmetatable,
                pairs = pairs,
                ipairs = ipairs,
                next = next,
                type = type,
                tonumber = tonumber,
                tostring = tostring,
                math = math,
                string = string,
                table = table,
              }
              env.CS = { UnityEngine = env }
              setmetatable(env, {
                __index = function(_, key) return proxy(tostring(key)) end
              })
              local fn, err = load(chunk, "@resource", "b", env)
              if fn == nil then error(err) end
              return fn()
            end
            """
        )

    def execute(self, bytecode: bytes) -> tuple[Any, LuaChunkHeader]:
        converted, header = convert_lua53_size_t(bytecode, target_size_t=8)
        result = self.loader(converted)
        value = self._convert(result, set())
        self.runtime.gccollect()
        return value, header

    def _convert(self, value: Any, stack: set[str]) -> Any:
        kind = lua_type(value)
        if kind == "table":
            identity = str(value)
            if identity in stack:
                return {"$cycle": identity}
            stack.add(identity)
            result = LuaTable()
            for key, child in value.items():
                converted_key = self._convert(key, stack)
                converted_value = self._convert(child, stack)
                result.set(converted_key, converted_value)
            stack.remove(identity)
            return result
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                return {"$bytes_hex": value.hex()}
        return value
