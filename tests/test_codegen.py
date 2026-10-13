"""Unit tests for pyck.codegen: IR -> C++ text generation.

These drive the full in-process pipeline (source -> IR -> C++ string) and
assert on substrings of the generated code. They don't invoke a C++
compiler -- see test_integration_hosted.py / test_integration_freestanding.py
for tests that actually compile and run the output.
"""
import unittest

from pyck.codegen import CodegenError

from .helpers import generate_cpp


class TestHostedVsFreestandingDispatch(unittest.TestCase):
    SRC = "def add(a: int, b: int) -> int:\n    return a + b\n"

    def test_hosted_uses_python_c_api(self):
        cpp = generate_cpp(self.SRC, mode="hosted")
        self.assertIn("#include <Python.h>", cpp)
        self.assertIn("PyNumber_Add(a, b)", cpp)
        self.assertIn("static PyObject* _pyck_add(PyObject* a, PyObject* b) {", cpp)

    def test_freestanding_uses_vendored_bignum(self):
        cpp = generate_cpp(self.SRC, mode="freestanding")
        self.assertIn('#include "pyck_runtime.h"', cpp)
        self.assertIn("_pyck_bignum_add(a, b)", cpp)
        self.assertNotIn("Python.h", cpp)
        self.assertNotIn("PyNumber_Add", cpp)

    def test_recursion_guard_emitted_in_every_function(self):
        cpp = generate_cpp(self.SRC, mode="hosted")
        self.assertIn("PYCK_CHECK_RECURSION();", cpp)
        self.assertIn("PYCK_LEAVE_RECURSION();", cpp)

    def test_hosted_main_wraps_pyck_main_with_init_finalize(self):
        cpp = generate_cpp(self.SRC, mode="hosted")
        self.assertIn("Py_Initialize();", cpp)
        self.assertIn("pyck_main();", cpp)
        self.assertIn("Py_Finalize();", cpp)

    def test_freestanding_has_no_libc_main_wrapper(self):
        cpp = generate_cpp(self.SRC, mode="freestanding")
        self.assertNotIn("Py_Initialize", cpp)
        self.assertIn('extern "C" void pyck_main() {', cpp)


class TestRecursionDepth(unittest.TestCase):
    def test_hosted_embeds_configured_max_recursion_depth(self):
        cpp = generate_cpp("def f(x: int) -> int:\n    return x\n",
                            mode="hosted", max_recursion_depth=42)
        self.assertIn("_pyck_max_recursion_depth = 42;", cpp)


class TestVariableDeclaration(unittest.TestCase):
    def test_reassignment_does_not_redeclare(self):
        cpp = generate_cpp("def main():\n    x = 1\n    x = 2\nmain()\n", mode="hosted")
        self.assertEqual(cpp.count("PyObject* x ="), 1)
        self.assertIn("x = ", cpp)

    def test_new_value_is_increfed(self):
        cpp = generate_cpp("def main():\n    x = 1\nmain()\n", mode="hosted")
        self.assertIn("Py_INCREF(x);", cpp)


class TestDivisionOperators(unittest.TestCase):
    def test_true_div_uses_truediv_regardless_of_operand_types(self):
        cpp = generate_cpp("def main():\n    x = 7 / 2\nmain()\n", mode="hosted")
        self.assertIn("PyNumber_TrueDivide(", cpp)
        cpp_fs = generate_cpp("def main():\n    x = 7 / 2\nmain()\n", mode="freestanding")
        self.assertIn("_pyck_bignum_truediv(", cpp_fs)

    def test_floor_div_and_mod(self):
        cpp = generate_cpp("def main():\n    a = 7 // 2\n    b = 7 % 2\nmain()\n", mode="hosted")
        self.assertIn("PyNumber_FloorDivide(", cpp)
        self.assertIn("PyNumber_Remainder(", cpp)


class TestBitwiseOperators(unittest.TestCase):
    def test_xor_hosted(self):
        cpp = generate_cpp("def main():\n    x = 5 ^ 3\nmain()\n", mode="hosted")
        self.assertIn("PyNumber_Xor(", cpp)

    def test_xor_freestanding(self):
        cpp = generate_cpp("def main():\n    x = 5 ^ 3\nmain()\n", mode="freestanding")
        self.assertIn("_pyck_bignum_xor(", cpp)


class TestPrint(unittest.TestCase):
    def test_hosted_print_mixes_string_literals_and_values(self):
        cpp = generate_cpp('def main():\n    print("hi", 1)\nmain()\n', mode="hosted")
        self.assertIn('fputs("hi", stdout);', cpp)
        self.assertIn("_pyck_fputPyObject(", cpp)

    def test_freestanding_mixed_print_boxes_everything_into_one_tuple(self):
        # A print() mixing a literal with a value can't split literals off
        # into separate _pyck_print_cstr calls (see TestFixedPrintBugs
        # below for why that used to scramble both order and spacing) --
        # every arg, including the literal, goes into one ordered tuple.
        cpp = generate_cpp('def main():\n    print("hi", 1)\nmain()\n', mode="freestanding")
        self.assertIn("_pyck_tuple_new(2)", cpp)
        self.assertIn('_pyck_str_from_cstr("hi", 2)', cpp)
        self.assertIn("py_print(_args, NULL, NULL);", cpp)

    def test_freestanding_all_literal_print_still_uses_cstr_fast_path(self):
        cpp = generate_cpp('def main():\n    print("hi", "there")\nmain()\n', mode="freestanding")
        self.assertIn("_pyck_print_cstr(", cpp)
        self.assertNotIn("_pyck_tuple_new(", cpp)

    def test_string_escaping(self):
        cpp = generate_cpp(r'def main():' "\n" r'    print("a\"b\nc")' "\n" 'main()\n',
                            mode="hosted")
        self.assertIn(r'"a\"b\nc"', cpp)


class TestFunctionMangling(unittest.TestCase):
    def test_user_functions_get_pyck_prefix(self):
        src = (
            "def helper(x: int) -> int:\n"
            "    return x\n"
            "def main():\n"
            "    print(helper(1))\n"
            "main()\n"
        )
        cpp = generate_cpp(src, mode="hosted")
        self.assertIn("_pyck_helper(", cpp)
        self.assertIn("static PyObject* _pyck_helper(PyObject* x) {", cpp)  # definition

    def test_builtins_are_not_mangled(self):
        cpp = generate_cpp('def main():\n    print(1)\nmain()\n', mode="hosted")
        self.assertNotIn("_pyck_print", cpp)


class TestDangerousMemoryOps(unittest.TestCase):
    def test_poke_emits_volatile_store(self):
        src = "def main():\n    poke8(4096, 5)\nmain()\n"
        cpp = generate_cpp(src, mode="hosted", dangerous_ops=True)
        self.assertIn("reinterpret_cast<volatile uint8_t*>", cpp)

    def test_peek_emits_volatile_load_wrapped_as_pyobject(self):
        src = "def main():\n    x = peek32(4096)\nmain()\n"
        cpp = generate_cpp(src, mode="hosted", dangerous_ops=True)
        self.assertIn("reinterpret_cast<volatile uint32_t*>", cpp)
        self.assertIn("PyLong_FromLong(", cpp)

        cpp_fs = generate_cpp(src, mode="freestanding", dangerous_ops=True)
        self.assertIn("_pyck_long_from_long(", cpp_fs)

    def test_peek_unboxes_the_address_argument(self):
        # peek32(4096) used to reinterpret_cast the boxed PyObject*
        # itself as the target address (dereferencing wherever the boxed
        # int 4096 happened to live on the heap, not address 4096) --
        # the address must be unboxed to a raw integer first.
        src = "def main():\n    x = peek32(4096)\nmain()\n"
        cpp = generate_cpp(src, mode="hosted", dangerous_ops=True)
        self.assertIn("reinterpret_cast<volatile uint32_t*>((uintptr_t)(", cpp)
        self.assertIn("PyLong_AsLong(", cpp)

        cpp_fs = generate_cpp(src, mode="freestanding", dangerous_ops=True)
        self.assertIn("reinterpret_cast<volatile uint32_t*>((uintptr_t)(", cpp_fs)
        self.assertIn("_pyck_long_as_long(", cpp_fs)

    def test_poke_unboxes_both_address_and_value(self):
        src = "def main():\n    poke32(4096, 7)\nmain()\n"
        cpp = generate_cpp(src, mode="hosted", dangerous_ops=True)
        poke_line = next(l for l in cpp.splitlines() if "reinterpret_cast<volatile uint32_t*>" in l)
        # Both the address and the stored value must be unboxed raw ints
        # on this line, not PyObject* pointers used directly.
        self.assertEqual(poke_line.count("PyLong_AsLong("), 2)


class TestHardwareIntrinsics(unittest.TestCase):
    def test_inb_outb_reuse_the_existing_per_arch_functions(self):
        # inb/outb already exist (byte-width) in every arch's own
        # support.cpp; codegen calls them directly rather than adding a
        # _pyck_-prefixed wrapper.
        cpp = generate_cpp(
            "def main():\n    x = inb(0x60)\n    outb(0x60, 1)\nmain()\n",
            mode="freestanding", dangerous_io_ops=True,
        )
        self.assertIn("_pyck_long_from_long((int64_t)inb(", cpp)
        self.assertIn("outb((uint16_t)_pyck_long_as_long", cpp)

    def test_wider_port_io_uses_shared_helpers(self):
        cpp = generate_cpp(
            "def main():\n"
            "    a = inw(0x60)\n    outw(0x60, 1)\n"
            "    b = inl(0x60)\n    outl(0x60, 1)\n"
            "main()\n",
            mode="freestanding", dangerous_io_ops=True,
        )
        self.assertIn("_pyck_inw(", cpp)
        self.assertIn("_pyck_outw(", cpp)
        self.assertIn("_pyck_inl(", cpp)
        self.assertIn("_pyck_outl(", cpp)

    def test_msr(self):
        cpp = generate_cpp(
            "def main():\n    wrmsr(0x1B, rdmsr(0x1B))\nmain()\n",
            mode="freestanding", dangerous_io_ops=True,
        )
        self.assertIn("_pyck_rdmsr((uint32_t)", cpp)
        self.assertIn("_pyck_wrmsr((uint32_t)", cpp)

    def test_cpuid_variants(self):
        cpp = generate_cpp(
            "def main():\n"
            "    a = cpuid_eax(0)\n    b = cpuid_ebx(0)\n"
            "    c = cpuid_ecx(0)\n    d = cpuid_edx(0)\n"
            "main()\n",
            mode="freestanding", dangerous_io_ops=True,
        )
        for reg in ("eax", "ebx", "ecx", "edx"):
            self.assertIn(f"_pyck_cpuid_{reg}(", cpp)

    def test_control_register_round_trip(self):
        cpp = generate_cpp(
            "def main():\n    write_cr0(read_cr0())\nmain()\n",
            mode="freestanding", dangerous_io_ops=True,
        )
        self.assertIn("_pyck_read_cr0()", cpp)
        self.assertIn("_pyck_write_cr0(", cpp)

    def test_hosted_mode_rejects_hardware_intrinsics(self):
        src = "def main():\n    x = inb(0x60)\nmain()\n"
        with self.assertRaises(CodegenError):
            generate_cpp(src, mode="hosted", dangerous_io_ops=True)


class TestMemoryMap(unittest.TestCase):
    def test_freestanding_emits_the_limine_table_accessor(self):
        # _LIMINE() is the only compiler builtin now -- everything else
        # (finding a request by magic id, entry_count/entries/base/
        # length/type, ...) is decoded in PyCk source via peek64 (see
        # PythonOS/kernel/), not hidden C++ accessors.
        cpp = generate_cpp("def main():\n    n = _LIMINE()\nmain()\n",
                            mode="freestanding")
        self.assertIn("_pyck_limine_table_addr(", cpp)

    def test_not_gated_behind_dangerous_io_ops(self):
        # Unlike cpuid/inb/etc., the Limine table needs no
        # --enable-dangerous-* flag at all -- generate_cpp() here passes
        # none.
        cpp = generate_cpp("def main():\n    n = _LIMINE()\nmain()\n",
                            mode="freestanding")
        self.assertIn("_pyck_limine_table_addr(", cpp)

    def test_hosted_mode_rejects_memory_map_access(self):
        src = "def main():\n    n = _LIMINE()\nmain()\n"
        with self.assertRaises(CodegenError):
            generate_cpp(src, mode="hosted")


class TestChr(unittest.TestCase):
    def test_hosted_uses_real_cpython_string(self):
        cpp = generate_cpp("def main():\n    x = chr(65)\nmain()\n", mode="hosted")
        self.assertIn("PyUnicode_FromOrdinal(", cpp)

    def test_freestanding_uses_minimal_char_type(self):
        # Also proves no dangerous-ops flag is needed: unlike peek/poke and
        # the hardware intrinsics, chr() touches no memory or hardware, and
        # generate_cpp()'s dangerous_ops/dangerous_io_ops both default to
        # False here.
        cpp = generate_cpp("def main():\n    x = chr(65)\nmain()\n", mode="freestanding")
        self.assertIn("_pyck_char_from_code(", cpp)

    def test_concatenation_hosted(self):
        cpp = generate_cpp("def main():\n    x = chr(65) + chr(66)\nmain()\n", mode="hosted")
        self.assertIn("PyUnicode_Concat(", cpp)

    def test_concatenation_freestanding(self):
        cpp = generate_cpp("def main():\n    x = chr(65) + chr(66)\nmain()\n", mode="freestanding")
        self.assertIn("_pyck_str_concat(", cpp)


class TestStringLiteralsAsValues(unittest.TestCase):
    def test_plain_print_of_a_literal_still_uses_the_fast_unboxed_path(self):
        # Regression check: print("literal") alone must keep going straight
        # to fputs/_pyck_print_cstr, not through the newly-added boxing
        # path -- boxing a literal just to hand it to print() would be
        # pure overhead for the single most common case.
        cpp_hosted = generate_cpp('def main():\n    print("Hello")\nmain()\n', mode="hosted")
        self.assertIn('fputs("Hello", stdout)', cpp_hosted)
        # NOTE: PyUnicode_FromString/PyUnicode_Substring/etc. still appear
        # in the hosted preamble's *definitions* of the string-slicing
        # helpers (emitted unconditionally, regardless of whether this
        # source uses slicing) -- what must NOT happen is *this specific
        # literal* getting boxed for a plain print().
        self.assertNotIn('PyUnicode_FromString("Hello")', cpp_hosted)

        cpp_free = generate_cpp('def main():\n    print("Hello")\nmain()\n', mode="freestanding")
        self.assertIn('_pyck_print_cstr("Hello")', cpp_free)
        self.assertNotIn("_pyck_str_from_cstr", cpp_free)

    def test_literal_assigned_to_a_variable_is_boxed(self):
        cpp_hosted = generate_cpp('def main():\n    s = "Hello"\nmain()\n', mode="hosted")
        self.assertIn('PyUnicode_FromString("Hello")', cpp_hosted)

        cpp_free = generate_cpp('def main():\n    s = "Hello"\nmain()\n', mode="freestanding")
        self.assertIn('_pyck_str_from_cstr("Hello", 5)', cpp_free)

    def test_literal_concatenated_with_chr_is_boxed(self):
        cpp = generate_cpp('def main():\n    x = "hi, " + chr(65)\nmain()\n', mode="freestanding")
        self.assertIn('_pyck_str_from_cstr("hi, ", 4)', cpp)
        self.assertIn("_pyck_str_concat(", cpp)


class TestLen(unittest.TestCase):
    def test_hosted(self):
        cpp = generate_cpp('def main():\n    n = len("hi")\nmain()\n', mode="hosted")
        self.assertIn("PyUnicode_GetLength(", cpp)

    def test_freestanding(self):
        cpp = generate_cpp('def main():\n    n = len("hi")\nmain()\n', mode="freestanding")
        self.assertIn("_pyck_str_length(", cpp)


class TestHex(unittest.TestCase):
    def test_hosted_defines_and_calls_the_helper(self):
        cpp = generate_cpp("def main():\n    s = hex(255)\nmain()\n", mode="hosted")
        self.assertIn("static PyObject* _pyck_int_to_hex(PyObject* obj)", cpp)
        self.assertIn("_pyck_int_to_hex(", cpp)
        # Uses the unsigned conversion (not PyLong_AsLongLong, which
        # can't represent values in [2^63, 2^64) -- see
        # test_hosted_hex_of_a_value_needing_the_full_unsigned_64_bits).
        self.assertIn("PyLong_AsUnsignedLongLong(", cpp)

    def test_freestanding_calls_the_runtime_function(self):
        cpp = generate_cpp("def main():\n    s = hex(255)\nmain()\n", mode="freestanding")
        self.assertIn("_pyck_int_to_hex(", cpp)
        # Unlike hosted, freestanding's implementation lives in
        # runtime/freestanding/py_runtime.cpp, not the generated preamble.
        self.assertNotIn("static PyObject* _pyck_int_to_hex", cpp)


class TestStringIndexingAndSlicing(unittest.TestCase):
    def test_getitem_freestanding(self):
        cpp = generate_cpp('def main():\n    s = "hi"\n    c = s[0]\nmain()\n', mode="freestanding")
        self.assertIn("_pyck_str_getitem(s,", cpp)

    def test_getitem_hosted(self):
        cpp = generate_cpp('def main():\n    s = "hi"\n    c = s[0]\nmain()\n', mode="hosted")
        self.assertIn("_pyck_str_getitem(s,", cpp)

    def test_slice_freestanding_passes_has_start_has_stop_has_step_flags(self):
        cpp = generate_cpp(
            'def main():\n'
            '    s = "hi"\n'
            '    a = s[1:]\n'
            '    b = s[:1]\n'
            '    c = s[:]\n'
            '    d = s[::2]\n'
            'main()\n',
            mode="freestanding",
        )
        self.assertIn("_pyck_str_slice(s, ", cpp)
        # Trailing (stop/step or start/step, has_start, has_stop, has_step)
        # args, one pattern per call:
        # s[1:]  -> stop=0, step=1 (default), has_start=1, has_stop=0, has_step=0
        # s[:1]  -> step=1 (default), has_start=0, has_stop=1, has_step=0
        # s[:]   -> start=0, stop=0, step=1 (default), has_start=0, has_stop=0, has_step=0
        # s[::2] -> start=0, stop=0, has_start=0, has_stop=0, has_step=1
        self.assertIn(", 0, 1, 1, 0, 0);", cpp)
        self.assertIn(", 1, 0, 1, 0);", cpp)
        self.assertIn(", 0, 0, 1, 0, 0, 0);", cpp)
        self.assertIn(", 0, 0, 1);", cpp)


class TestStringComparison(unittest.TestCase):
    def test_hosted_uses_rich_compare(self):
        cpp = generate_cpp('def main():\n    x = "a" == "b"\nmain()\n', mode="hosted")
        self.assertIn("PyObject_RichCompareBool(", cpp)
        self.assertIn("Py_EQ", cpp)

    def test_freestanding_uses_str_cmp(self):
        cpp = generate_cpp('def main():\n    x = "a" < "b"\nmain()\n', mode="freestanding")
        self.assertIn("_pyck_str_cmp(", cpp)


class TestStringMultiplication(unittest.TestCase):
    def test_string_times_int_hosted(self):
        cpp = generate_cpp('def main():\n    s = "ab" * 3\nmain()\n', mode="hosted")
        self.assertIn("PySequence_Repeat(", cpp)

    def test_int_times_string_freestanding(self):
        # Operand order in source is int-then-string; codegen must still
        # pass the string first to _pyck_str_repeat(s, n).
        cpp = generate_cpp('def main():\n    s = 3 * "ab"\nmain()\n', mode="freestanding")
        self.assertIn('_pyck_str_repeat(_pyck_str_from_cstr("ab", 2),', cpp)


class TestFixedCodegenBugs(unittest.TestCase):
    """Regression tests for codegen paths that used to be broken.

    `and`/`or` (ast.BoolOp) and chained comparisons (`a < b < c`) both lower
    in ast_to_ir to BINARY_OP(BITAND/BITOR) over PYBOOL operands, and
    CodegenError used to be raised for both because _emit_binop only
    special-cased BITAND/BITOR for PyTypeKind.INT operands. Comparisons are
    now boxed as a real PyObject* bool singleton (see _emit_compare), and
    _emit_binop combines truthiness for the BOOL/BITAND/BITOR case instead
    of falling through to the int-bitwise-and/or paths.
    """

    def test_boolop_and_codegen(self):
        src = "def main():\n    a = True\n    b = False\n    print(a and b)\nmain()\n"
        cpp = generate_cpp(src, mode="hosted")
        self.assertIn("PyObject_IsTrue", cpp)

    def test_chained_comparison_codegen(self):
        src = "def main():\n    a = 1\n    b = 2\n    c = 3\n    print(a < b < c)\nmain()\n"
        cpp = generate_cpp(src, mode="hosted")
        self.assertIn("&&", cpp)

    def test_variable_assigned_in_both_if_and_else_branches_usable_after(self):
        # A variable first assigned inside a nested if/else block used to
        # get its `PyObject* x = ...` declaration emitted inside whichever
        # branch's C++ { } block ran first, making it invisible to the
        # sibling branch and to any code after the if/else -- even though
        # Python considers it in scope for the rest of the function. Every
        # local assigned anywhere in the body (see _collect_assigned_names)
        # is now pre-declared at the top of the function instead.
        src = (
            "def main():\n"
            "    x = 1\n"
            "    if x == 1:\n"
            "        label = 5\n"
            "    else:\n"
            "        label = 10\n"
            "    print(label)\n"
            "main()\n"
        )
        cpp = generate_cpp(src, mode="hosted")
        self.assertIn("PyObject* label = nullptr;", cpp)
        self.assertNotIn("PyObject* label = PyLong_FromLong", cpp)

    def test_variable_assigned_only_inside_while_usable_after(self):
        src = (
            "def main():\n"
            "    i = 0\n"
            "    while i < 3:\n"
            "        total = i\n"
            "        i = i + 1\n"
            "    print(total)\n"
            "main()\n"
        )
        cpp = generate_cpp(src, mode="hosted")
        self.assertIn("PyObject* total = nullptr;", cpp)

    def test_forward_declarations_are_actually_emitted(self):
        # generate() calls _emit_func_forward() for every function, which
        # appends a prototype to self._forward_decls; that list is now
        # joined into the final output, so a function calling another
        # function defined *later* in the source compiles correctly.
        src = (
            "def main():\n"
            "    print(later())\n"
            "def later() -> int:\n"
            "    return 7\n"
            "main()\n"
        )
        cpp = generate_cpp(src, mode="hosted")
        self.assertRegex(cpp, r"static PyObject\* _pyck_later\([^)]*\);")


class TestClasses(unittest.TestCase):
    SRC = (
        "class Point:\n"
        "    def __init__(self, x: int, y: int):\n"
        "        self.x = x\n"
        "        self.y = y\n"
        "    def total(self):\n"
        "        return self.x + self.y\n"
        "def main():\n"
        "    p = Point(1, 2)\n"
        "    print(p.x)\n"
        "    p.x = 5\n"
        "    print(p.total())\n"
        "main()\n"
    )

    def test_freestanding_emits_struct_and_type_object(self):
        cpp = generate_cpp(self.SRC, mode="freestanding")
        self.assertIn("struct _pyck_instance_Point {", cpp)
        self.assertIn("PyObject* x;", cpp)
        self.assertIn("PyObject* y;", cpp)
        self.assertIn(
            'static PyTypeObject _pyck_type_Point = { { 1, 0 }, "Point", _pyck_dealloc_Point };',
            cpp)

    def test_freestanding_method_is_a_mangled_direct_call(self):
        cpp = generate_cpp(self.SRC, mode="freestanding")
        self.assertIn("static PyObject* _pyck_Point_total(PyObject* self)", cpp)
        self.assertIn("_pyck_Point_total(p)", cpp)

    def test_freestanding_constructor_bundles_alloc_and_init(self):
        cpp = generate_cpp(self.SRC, mode="freestanding")
        self.assertIn("static PyObject* _pyck_new_Point(PyObject* x, PyObject* y)", cpp)
        self.assertIn("py_alloc(sizeof(_pyck_instance_Point))", cpp)
        self.assertIn("_pyck_Point___init__(self, x, y);", cpp)

    def test_freestanding_attribute_get_is_a_struct_field_access(self):
        cpp = generate_cpp(self.SRC, mode="freestanding")
        self.assertIn("((_pyck_instance_Point*)p)->x", cpp)

    def test_freestanding_attribute_set_increfs_the_new_value(self):
        cpp = generate_cpp(self.SRC, mode="freestanding")
        self.assertIn("->x = _attr_value;", cpp)
        self.assertIn("Py_INCREF(_attr_value);", cpp)

    def test_hosted_instance_is_a_real_dict(self):
        cpp = generate_cpp(self.SRC, mode="hosted")
        self.assertIn("PyObject* self = PyDict_New();", cpp)
        self.assertIn('PyDict_SetItemString(self, "x", x);', cpp)
        self.assertIn('PyDict_GetItemString(p, "x")', cpp)

    def test_hosted_method_is_still_a_direct_mangled_call(self):
        # Method dispatch is always static/monomorphic in both modes --
        # no vtables, since there's no inheritance/polymorphism in scope.
        cpp = generate_cpp(self.SRC, mode="hosted")
        self.assertIn("static PyObject* _pyck_Point_total(PyObject* self)", cpp)
        self.assertIn("_pyck_Point_total(p)", cpp)

    def test_method_calling_another_method_of_the_same_class(self):
        src = (
            "class Calc:\n"
            "    def __init__(self, base: int):\n"
            "        self.base = base\n"
            "    def double(self):\n"
            "        return self.base * 2\n"
            "    def quadruple(self):\n"
            "        return self.double() * 2\n"
            "def main():\n"
            "    c = Calc(3)\n"
            "    print(c.quadruple())\n"
            "main()\n"
        )
        cpp = generate_cpp(src, mode="freestanding")
        self.assertIn("_pyck_Calc_double(self)", cpp)

    def test_class_with_no_init_skips_the_init_call_in_constructor(self):
        src = (
            "class Foo:\n"
            "    def greet(self):\n"
            "        print(1)\n"
            "def main():\n"
            "    f = Foo()\n"
            "    f.greet()\n"
            "main()\n"
        )
        cpp = generate_cpp(src, mode="freestanding")
        self.assertIn("static PyObject* _pyck_new_Foo() {", cpp)
        self.assertNotIn("_pyck_Foo___init__", cpp)


class TestStackAllocation(unittest.TestCase):
    """Codegen-level checks for escape.py-driven stack allocation of
    non-escaping class instances. See tests/test_escape.py for the
    analysis-level (pure Python) verdicts this renders into C++."""

    POINT = (
        "class Point:\n"
        "    def __init__(self, x: int, y: int):\n"
        "        self.x = x\n"
        "        self.y = y\n"
        "    def total(self) -> int:\n"
        "        return self.x + self.y\n"
    )

    def test_non_escaping_instance_uses_stack_storage_and_constructor(self):
        src = self.POINT + (
            "def compute() -> int:\n"
            "    p = Point(1, 2)\n"
            "    return p.total()\n"
            "compute()\n"
        )
        cpp = generate_cpp(src, mode="freestanding")
        self.assertIn("_pyck_instance_Point _pyck_stack_p;", cpp)
        self.assertIn("p = _pyck_new_Point_stack(&_pyck_stack_p,", cpp)
        # The heap constructor still exists (definition), it's just unused
        # at this call site -- other call sites elsewhere may still need it.
        self.assertNotIn("p = _pyck_new_Point(", cpp)

    def test_stack_constructor_sets_up_refcount_and_stack_type_object(self):
        src = self.POINT + (
            "def compute() -> int:\n"
            "    p = Point(1, 2)\n"
            "    return p.total()\n"
            "compute()\n"
        )
        cpp = generate_cpp(src, mode="freestanding")
        self.assertIn(
            "static PyObject* _pyck_new_Point_stack(_pyck_instance_Point* _storage, "
            "PyObject* x, PyObject* y) {",
            cpp)
        self.assertIn("PyObject* self = (PyObject*)_storage;", cpp)
        self.assertIn("self->ob_refcnt = 1;", cpp)
        self.assertIn("self->ob_type = &_pyck_type_Point_stack;", cpp)
        self.assertIn("_pyck_Point___init__(self, x, y);", cpp)

    def test_stack_dealloc_decrefs_fields_but_never_frees(self):
        src = (
            "class Pair:\n"
            "    def __init__(self, a: int, b: int):\n"
            "        self.a = a\n"
            "        self.b = b\n"
            "def compute() -> int:\n"
            "    p = Pair(1, 2)\n"
            "    return p.a\n"
            "compute()\n"
        )
        cpp = generate_cpp(src, mode="freestanding")
        dealloc_start = cpp.index("static void _pyck_dealloc_Pair_stack(PyObject* self) {")
        dealloc_end = cpp.index("}", dealloc_start)
        dealloc_body = cpp[dealloc_start:dealloc_end]
        self.assertIn("Py_DECREF", dealloc_body)
        self.assertNotIn("py_free", dealloc_body)

    def test_returned_instance_stays_heap_allocated(self):
        src = self.POINT + (
            "def make():\n"
            "    p = Point(1, 2)\n"
            "    return p\n"
            "make()\n"
        )
        cpp = generate_cpp(src, mode="freestanding")
        self.assertIn("p = _pyck_new_Point(", cpp)
        self.assertNotIn("_pyck_stack_p", cpp)
        self.assertNotIn("_pyck_new_Point_stack", cpp)

    def test_unused_stack_variant_is_not_emitted_at_all(self):
        # No call site anywhere in the program is stack-eligible -> no
        # dead _stack type object/dealloc/constructor should be emitted.
        src = self.POINT + (
            "def make():\n"
            "    p = Point(1, 2)\n"
            "    return p\n"
            "make()\n"
        )
        cpp = generate_cpp(src, mode="freestanding")
        self.assertNotIn("_stack", cpp)

    def test_hosted_mode_never_stack_allocates(self):
        # Hosted-mode instances are real CPython PyDict objects -- the
        # optimization must not fire there at all.
        src = self.POINT + (
            "def compute() -> int:\n"
            "    p = Point(1, 2)\n"
            "    return p.total()\n"
            "compute()\n"
        )
        cpp = generate_cpp(src, mode="hosted")
        self.assertNotIn("_stack", cpp)
        self.assertNotIn("py_alloc", cpp)

    def test_loop_reuses_a_single_stack_slot(self):
        src = self.POINT + (
            "def loop() -> int:\n"
            "    total = 0\n"
            "    i = 0\n"
            "    while i < 3:\n"
            "        p = Point(i, i)\n"
            "        total = total + p.total()\n"
            "        i = i + 1\n"
            "    return total\n"
            "loop()\n"
        )
        cpp = generate_cpp(src, mode="freestanding")
        # Declared exactly once, up front, like every other local -- not
        # redeclared on each loop iteration.
        self.assertEqual(cpp.count("_pyck_instance_Point _pyck_stack_p;"), 1)
        self.assertIn("p = _pyck_new_Point_stack(&_pyck_stack_p,", cpp)

    def test_class_leaking_self_never_gets_a_stack_variant(self):
        src = (
            "class Node:\n"
            "    def __init__(self, v: int):\n"
            "        self.v = v\n"
            "    def identity(self):\n"
            "        return self\n"
            "def make() -> int:\n"
            "    n = Node(1)\n"
            "    return n.v\n"
            "make()\n"
        )
        cpp = generate_cpp(src, mode="freestanding")
        self.assertNotIn("_stack", cpp)


if __name__ == "__main__":
    unittest.main()
