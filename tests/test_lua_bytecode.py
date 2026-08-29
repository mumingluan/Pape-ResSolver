import unittest

from lupa.lua53 import LuaRuntime

from pape_res_solver.lua_bytecode import convert_lua53_size_t


class LuaBytecodeTests(unittest.TestCase):
    def test_size_t_round_trip_preserves_executable_chunk(self) -> None:
        runtime = LuaRuntime(encoding=None, register_eval=False, register_builtins=False)
        chunk = runtime.eval(b"string.dump(function() return {[9007199254740993] = {'ok', 42}} end, true)")
        size_t4, original = convert_lua53_size_t(chunk, 4)
        self.assertEqual(original.size_t_size, 8)
        size_t8, converted = convert_lua53_size_t(size_t4, 8)
        self.assertEqual(converted.size_t_size, 4)
        loader = runtime.eval(
            b"function(data) local fn, err = load(data, '@test', 'b', {}); if not fn then error(err) end; return fn() end"
        )
        result = loader(size_t8)
        self.assertEqual(result[9007199254740993][1], b"ok")
        self.assertEqual(result[9007199254740993][2], 42)


if __name__ == "__main__":
    unittest.main()
