"""Unit tests for pyck.typechecker: whole-program type inference and validation."""
import unittest

from pyck.types import PYBOOL, PYFLOAT, PYINT, PYNONE, PYSTRING, PyType, PyTypeKind

from .helpers import typecheck, typecheck_errors


class TestAnnotatedFunctions(unittest.TestCase):
    def test_clean_annotated_function_has_no_errors(self):
        src = (
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n"
        )
        self.assertEqual(typecheck_errors(src), [])

    def test_annotation_inconsistent_with_inferred_type(self):
        src = (
            "def g():\n"
            "    return f(1.5)\n"
            "def f(x: int) -> int:\n"
            "    return x\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("inconsistent" in e for e in errors), errors)

    def test_annotation_mismatch_caught_regardless_of_definition_order(self):
        # Same semantic mismatch as test_annotation_inconsistent_with_
        # inferred_type above, with the caller/callee definition order
        # swapped. _pass2_infer_and_validate() runs annotation validation
        # in a pass separate from (and after) body inference has reached a
        # fixed point across all functions, so this must be caught the same
        # way no matter which function's FunctionDef comes first in source.
        src = (
            "def f(x: int) -> int:\n"
            "    return x\n"
            "def g():\n"
            "    return f(1.5)\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("inconsistent" in e for e in errors), errors)

    def test_return_annotation_inconsistent_with_inferred_return(self):
        src = (
            "def f() -> int:\n"
            "    return 1.5\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("Return annotation" in e for e in errors), errors)


class TestInferredFunctions(unittest.TestCase):
    def test_param_types_inferred_from_call_site(self):
        # Body inference runs to a fixed point across all functions
        # (_pass2_infer_and_validate) before any errors are recorded, so an
        # unannotated parameter's type -- discovered from a call site in
        # another function, possibly processed in an earlier or later
        # round -- is visible by the time this function's own body is
        # walked for real.
        src = (
            "def add(a, b):\n"
            "    return a + b\n"
            "def main():\n"
            "    print(add(1, 2))\n"
            "main()\n"
        )
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        sig = checker._funcs["add"]
        self.assertEqual(sig.param_types, [PYINT, PYINT])
        self.assertEqual(sig.return_type, PYINT)

    def test_never_called_unannotated_param_is_an_error(self):
        src = (
            "def f(x):\n"
            "    return x\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("Cannot infer type of parameter" in e for e in errors), errors)

    def test_no_return_statements_infers_none(self):
        src = (
            "def f():\n"
            "    x = 1\n"
        )
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["f"].return_type, PYNONE)


class TestNameResolution(unittest.TestCase):
    def test_undefined_variable_is_an_error(self):
        src = (
            "def f():\n"
            "    return y\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("Undefined variable 'y'" in e for e in errors), errors)

    def test_undefined_function_is_an_error(self):
        src = (
            "def main():\n"
            "    does_not_exist(1)\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("Undefined function 'does_not_exist'" in e for e in errors), errors)

    def test_global_variable_usable_at_module_top_level(self):
        src = "counter = 5\nprint(counter)\n"
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._global_vars["counter"], PYINT)

    def test_global_variable_visible_inside_function_body(self):
        # _pass2_infer_and_validate() infers globals before it infers any
        # function body, so a global read from inside a function resolves
        # the same way it does when read at module top level (see the test
        # above).
        src = (
            "counter = 0\n"
            "def bump() -> int:\n"
            "    return counter + 1\n"
        )
        errors = typecheck_errors(src)
        self.assertEqual(errors, [])


class TestGlobalVarInference(unittest.TestCase):
    def test_global_var_type_inferred_from_literal(self):
        src = "x = 5\n"
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._global_vars["x"], PYINT)

    def test_later_global_can_reference_earlier_global(self):
        src = "x = 5\ny = x + 1\n"
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._global_vars["x"], PYINT)
        self.assertEqual(checker._global_vars["y"], PYINT)


class TestPrintValidation(unittest.TestCase):
    def test_print_sep_keyword_rejected(self):
        src = 'def main():\n    print(1, sep=",")\n'
        errors = typecheck_errors(src)
        self.assertTrue(any("not supported in v1" in e for e in errors), errors)

    def test_print_end_keyword_rejected(self):
        src = 'def main():\n    print(1, end="")\n'
        errors = typecheck_errors(src)
        self.assertTrue(any("not supported in v1" in e for e in errors), errors)

    def test_plain_print_is_fine(self):
        src = "def main():\n    print(1, 2, 3)\n"
        self.assertEqual(typecheck_errors(src), [])


class TestDangerousMemoryOps(unittest.TestCase):
    def test_peek_rejected_without_flag(self):
        src = "def main():\n    x = peek8(1000)\n"
        errors = typecheck_errors(src, dangerous_ops=False)
        self.assertTrue(
            any("enable-dangerous-memory-ops" in e for e in errors), errors
        )

    def test_peek_allowed_with_flag(self):
        src = "def main():\n    x = peek8(1000)\n"
        errors = typecheck_errors(src, dangerous_ops=True)
        self.assertEqual(errors, [])

    def test_poke_rejected_without_flag(self):
        src = "def main():\n    poke8(1000, 1)\n"
        errors = typecheck_errors(src, dangerous_ops=False)
        self.assertTrue(
            any("enable-dangerous-memory-ops" in e for e in errors), errors
        )

    def test_poke_wrong_arg_count_is_an_error(self):
        src = "def main():\n    poke8(1000)\n"
        errors = typecheck_errors(src, dangerous_ops=True)
        self.assertTrue(any("takes exactly 2 arguments" in e for e in errors), errors)


class TestHardwareIntrinsics(unittest.TestCase):
    def test_rejected_without_flag(self):
        src = "def main():\n    x = inb(0x60)\n"
        errors = typecheck_errors(src, dangerous_io_ops=False)
        self.assertTrue(any("enable-dangerous-io-ops" in e for e in errors), errors)

    def test_allowed_with_flag(self):
        src = "def main():\n    x = inb(0x60)\n"
        errors = typecheck_errors(src, dangerous_io_ops=True)
        self.assertEqual(errors, [])

    def test_port_io_return_types(self):
        src = (
            "def main():\n"
            "    a = inb(0x60)\n"
            "    b = inw(0x60)\n"
            "    c = inl(0x60)\n"
        )
        checker = typecheck(src, dangerous_io_ops=True)
        self.assertEqual(checker._errors, [])
        local_types = checker._funcs["main"].local_types
        self.assertEqual(local_types["a"], PYINT)
        self.assertEqual(local_types["b"], PYINT)
        self.assertEqual(local_types["c"], PYINT)

    def test_outb_wrong_arg_count_is_an_error(self):
        src = "def main():\n    outb(0x60)\n"
        errors = typecheck_errors(src, dangerous_io_ops=True)
        self.assertTrue(any("takes exactly 2 arguments" in e for e in errors), errors)

    def test_rdmsr_wrong_arg_count_is_an_error(self):
        src = "def main():\n    x = rdmsr()\n"
        errors = typecheck_errors(src, dangerous_io_ops=True)
        self.assertTrue(any("takes exactly 1 argument" in e for e in errors), errors)

    def test_cpuid_variants_all_gated_and_typed(self):
        src = (
            "def main():\n"
            "    a = cpuid_eax(0)\n"
            "    b = cpuid_ebx(0)\n"
            "    c = cpuid_ecx(0)\n"
            "    d = cpuid_edx(0)\n"
        )
        errors_without = typecheck_errors(src, dangerous_io_ops=False)
        self.assertEqual(len(errors_without), 4, errors_without)
        checker = typecheck(src, dangerous_io_ops=True)
        self.assertEqual(checker._errors, [])
        for name in ("a", "b", "c", "d"):
            self.assertEqual(checker._funcs["main"].local_types[name], PYINT)

    def test_read_cr_takes_no_arguments(self):
        src = "def main():\n    x = read_cr0(1)\n"
        errors = typecheck_errors(src, dangerous_io_ops=True)
        self.assertTrue(any("takes exactly 0 arguments" in e for e in errors), errors)

    def test_write_cr_round_trip_types(self):
        src = "def main():\n    write_cr0(read_cr0())\n"
        errors = typecheck_errors(src, dangerous_io_ops=True)
        self.assertEqual(errors, [])

    def test_memory_ops_flag_does_not_enable_io_ops(self):
        # The two dangerous-ops flags are independent: enabling peek/poke
        # must not silently also enable port I/O / MSR / CPUID / control
        # register access.
        src = "def main():\n    x = inb(0x60)\n"
        errors = typecheck_errors(src, dangerous_ops=True, dangerous_io_ops=False)
        self.assertTrue(any("enable-dangerous-io-ops" in e for e in errors), errors)


class TestMemoryMap(unittest.TestCase):
    def test_always_available_no_flag_needed(self):
        # Unlike the hardware intrinsics above, the Limine request table
        # is read-only environment info, not I/O/memory manipulation, so
        # it's not gated behind --enable-dangerous-io-ops. _LIMINE() is
        # the *only* builtin -- everything else (finding a specific
        # request by its magic id, its response pointer, entry_count,
        # entries pointer, base/length/type per memmap entry, ...) is
        # decoded in PyCk source with peek64 (see PythonOS/kernel/'s
        # limine_response/MemoryMap/MemoryMapEntry), which is why there's
        # only one name to test here now.
        src = "def main():\n    n = _LIMINE()\n"
        self.assertEqual(typecheck_errors(src), [])

    def test_return_type(self):
        src = "def main():\n    n = _LIMINE()\n"
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["n"], PYINT)

    def test_takes_no_arguments(self):
        src = "def main():\n    n = _LIMINE(0)\n"
        errors = typecheck_errors(src)
        self.assertTrue(any("takes exactly 0 arguments" in e for e in errors), errors)


class TestChr(unittest.TestCase):
    def test_always_available_no_flag_needed(self):
        src = "def main():\n    x = chr(65)\n"
        errors = typecheck_errors(src)
        self.assertEqual(errors, [])

    def test_return_type_is_string(self):
        src = "def main():\n    x = chr(65)\n"
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["x"], PYSTRING)

    def test_wrong_arg_count_is_an_error(self):
        src = "def main():\n    x = chr()\n"
        errors = typecheck_errors(src)
        self.assertTrue(any("takes exactly 1 argument" in e for e in errors), errors)

    def test_concatenation_type_checks_to_string(self):
        src = "def main():\n    x = chr(65) + chr(66)\n"
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["x"], PYSTRING)


class TestStringLiteralsAsValues(unittest.TestCase):
    def test_assignable_to_a_variable(self):
        src = 'def main():\n    s = "hello"\n'
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["s"], PYSTRING)

    def test_concatenation_with_literal_on_either_side(self):
        src = (
            'def main():\n'
            '    a = "hi, " + chr(65)\n'
            '    b = chr(65) + " there"\n'
            '    c = "a" + "b"\n'
        )
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        local_types = checker._funcs["main"].local_types
        self.assertEqual(local_types["a"], PYSTRING)
        self.assertEqual(local_types["b"], PYSTRING)
        self.assertEqual(local_types["c"], PYSTRING)

    def test_str_annotation_on_params_and_return(self):
        src = (
            'def greet(name: str) -> str:\n'
            '    return "Hello, " + name\n'
        )
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        sig = checker._funcs["greet"]
        self.assertEqual(sig.param_types, [PYSTRING])
        self.assertEqual(sig.return_type, PYSTRING)


class TestLen(unittest.TestCase):
    def test_returns_int(self):
        src = 'def main():\n    n = len("hello")\n'
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["n"], PYINT)

    def test_wrong_arg_count_is_an_error(self):
        src = "def main():\n    n = len()\n"
        errors = typecheck_errors(src)
        self.assertTrue(any("takes exactly 1 argument" in e for e in errors), errors)

    def test_non_string_argument_is_an_error(self):
        src = "def main():\n    n = len(5)\n"
        errors = typecheck_errors(src)
        self.assertTrue(any("only supported on str values" in e for e in errors), errors)


class TestHex(unittest.TestCase):
    def test_always_available_no_flag_needed(self):
        src = "def main():\n    s = hex(255)\n"
        self.assertEqual(typecheck_errors(src), [])

    def test_returns_str(self):
        src = "def main():\n    s = hex(255)\n"
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["s"], PYSTRING)

    def test_wrong_arg_count_is_an_error(self):
        src = "def main():\n    s = hex()\n"
        errors = typecheck_errors(src)
        self.assertTrue(any("takes exactly 1 argument" in e for e in errors), errors)

    def test_non_int_argument_is_an_error(self):
        src = 'def main():\n    s = hex("abc")\n'
        errors = typecheck_errors(src)
        self.assertTrue(any("only supported on int values" in e for e in errors), errors)

    def test_bool_argument_is_accepted(self):
        # bool is a subtype of int, like Python's own hex(True) == '0x1'.
        src = "def main():\n    s = hex(True)\n"
        self.assertEqual(typecheck_errors(src), [])


class TestStringIndexingAndSlicing(unittest.TestCase):
    def test_indexing_returns_string(self):
        src = 'def main():\n    s = "hello"\n    c = s[0]\n'
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["c"], PYSTRING)

    def test_negative_index_type_checks(self):
        src = 'def main():\n    s = "hello"\n    c = s[-1]\n'
        errors = typecheck_errors(src)
        self.assertEqual(errors, [])

    def test_slicing_returns_string(self):
        src = 'def main():\n    s = "hello"\n    t = s[1:3]\n'
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["t"], PYSTRING)

    def test_slicing_with_omitted_bounds(self):
        src = (
            'def main():\n'
            '    s = "hello"\n'
            '    a = s[:2]\n'
            '    b = s[2:]\n'
            '    c = s[:]\n'
        )
        errors = typecheck_errors(src)
        self.assertEqual(errors, [])

    def test_slicing_with_step_type_checks(self):
        src = 'def main():\n    s = "hello"\n    t = s[::2]\n'
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["t"], PYSTRING)

    def test_slicing_with_literal_zero_step_is_an_error(self):
        src = 'def main():\n    s = "hello"\n    t = s[::0]\n'
        errors = typecheck_errors(src)
        self.assertTrue(any("slice step cannot be zero" in e for e in errors), errors)

    def test_subscript_on_non_string_is_an_error(self):
        src = "def main():\n    x = 5\n    y = x[0]\n"
        errors = typecheck_errors(src)
        self.assertTrue(any("only supported on str values" in e for e in errors), errors)


class TestStringComparison(unittest.TestCase):
    def test_equality_and_ordering_type_check(self):
        src = (
            'def main():\n'
            '    a = "abc" == "abc"\n'
            '    b = "abc" != "xyz"\n'
            '    c = "abc" < "xyz"\n'
            '    d = "abc" <= "xyz"\n'
            '    e = "abc" > "xyz"\n'
            '    f = "abc" >= "xyz"\n'
        )
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        local_types = checker._funcs["main"].local_types
        for name in "abcdef":
            self.assertEqual(local_types[name], PYBOOL)


class TestStringMultiplication(unittest.TestCase):
    def test_string_times_int(self):
        src = 'def main():\n    s = "ab" * 3\n'
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["s"], PYSTRING)

    def test_int_times_string(self):
        src = 'def main():\n    s = 3 * "ab"\n'
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["s"], PYSTRING)


class TestBinopInference(unittest.TestCase):
    def test_true_div_is_always_float(self):
        src = "def main():\n    x = 7 / 2\n"
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["x"], PYFLOAT)

    def test_floor_div_is_int(self):
        src = "def main():\n    x = 7 // 2\n"
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["x"], PYINT)

    def test_mixed_int_float_add_unifies_to_float(self):
        src = "def main():\n    x = 1 + 2.5\n"
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["x"], PYFLOAT)

    def test_incompatible_binop_operands_error(self):
        src = 'def main():\n    x = "hi" + 1\n'
        errors = typecheck_errors(src)
        self.assertTrue(any("Cannot unify" in e for e in errors), errors)

    def test_comparison_result_is_bool(self):
        src = "def main():\n    x = 1 < 2\n"
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["x"], PYBOOL)


class TestClasses(unittest.TestCase):
    def test_basic_class_no_errors(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int, y: int):\n"
            "        self.x = x\n"
            "        self.y = y\n"
            "def main():\n"
            "    p = Point(1, 2)\n"
            "    print(p.x)\n"
            "main()\n"
        )
        self.assertEqual(typecheck_errors(src), [])

    def test_instantiation_infers_class_type(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int, y: int):\n"
            "        self.x = x\n"
            "        self.y = y\n"
            "def main():\n"
            "    p = Point(1, 2)\n"
        )
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["p"],
                          PyType(PyTypeKind.CLASS, "Point"))

    def test_attribute_types_inferred_from_init(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int, y: float):\n"
            "        self.x = x\n"
            "        self.y = y\n"
        )
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._classes["Point"].attributes["x"], PYINT)
        self.assertEqual(checker._classes["Point"].attributes["y"], PYFLOAT)

    def test_method_call_infers_return_type(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int, y: int):\n"
            "        self.x = x\n"
            "        self.y = y\n"
            "    def total(self):\n"
            "        return self.x + self.y\n"
            "def main():\n"
            "    p = Point(1, 2)\n"
            "    t = p.total()\n"
        )
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["t"], PYINT)

    def test_method_mutates_attribute_via_self(self):
        src = (
            "class Counter:\n"
            "    def __init__(self):\n"
            "        self.n = 0\n"
            "    def bump(self):\n"
            "        self.n = self.n + 1\n"
        )
        self.assertEqual(typecheck_errors(src), [])

    def test_attribute_set_outside_init_also_discovered(self):
        # Attribute types are inferred from *any* `obj.attr = ...` where
        # obj's type is a class, not just assignments inside __init__.
        src = (
            "class Box:\n"
            "    def __init__(self):\n"
            "        pass\n"
            "def main():\n"
            "    b = Box()\n"
            "    b.value = 5\n"
            "    print(b.value)\n"
        )
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._classes["Box"].attributes["value"], PYINT)

    def test_undefined_attribute_is_an_error(self):
        src = (
            "class Foo:\n"
            "    def __init__(self):\n"
            "        self.x = 1\n"
            "def main():\n"
            "    f = Foo()\n"
            "    print(f.y)\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("no attribute 'y'" in e for e in errors), errors)

    def test_undefined_method_is_an_error(self):
        src = (
            "class Foo:\n"
            "    def __init__(self):\n"
            "        self.x = 1\n"
            "def main():\n"
            "    f = Foo()\n"
            "    f.bar()\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("no method 'bar'" in e for e in errors), errors)

    def test_class_with_no_init_allows_zero_arg_instantiation(self):
        src = (
            "class Foo:\n"
            "    def greet(self):\n"
            "        print(1)\n"
            "def main():\n"
            "    f = Foo()\n"
            "    f.greet()\n"
        )
        self.assertEqual(typecheck_errors(src), [])

    def test_wrong_constructor_arg_count_is_an_error(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int, y: int):\n"
            "        self.x = x\n"
            "        self.y = y\n"
            "def main():\n"
            "    p = Point(1)\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("Point" in e for e in errors), errors)

    def test_attribute_access_on_non_class_is_an_error(self):
        src = "def main():\n    x = 5\n    print(x.y)\n"
        errors = typecheck_errors(src)
        self.assertTrue(
            any("only supported on class instances" in e for e in errors), errors)

    def test_method_can_call_another_method_of_the_same_class_via_self(self):
        src = (
            "class Calc:\n"
            "    def __init__(self, base: int):\n"
            "        self.base = base\n"
            "    def double(self):\n"
            "        return self.base * 2\n"
            "    def quadruple(self):\n"
            "        return self.double() * 2\n"
        )
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._classes["Calc"].methods["quadruple"].return_type, PYINT)

    def test_boolean_method_using_another_methods_result(self):
        src = (
            "class Calc:\n"
            "    def __init__(self, base: int):\n"
            "        self.base = base\n"
            "    def quadruple(self):\n"
            "        return self.base * 4\n"
            "    def is_big(self):\n"
            "        return self.quadruple() > 10\n"
        )
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._classes["Calc"].methods["is_big"].return_type, PYBOOL)

    def test_str_dunder_returning_str_is_accepted(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int):\n"
            "        self.x = x\n"
            "    def __str__(self):\n"
            "        return \"a point\"\n"
        )
        self.assertEqual(typecheck_errors(src), [])

    def test_str_dunder_must_return_str(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int):\n"
            "        self.x = x\n"
            "    def __str__(self):\n"
            "        return self.x\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("must return a str" in e for e in errors), errors)

    def test_str_dunder_must_take_no_extra_arguments(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int):\n"
            "        self.x = x\n"
            "    def __str__(self, extra: int):\n"
            "        return \"a point\"\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("no arguments other than 'self'" in e for e in errors), errors)

    def test_two_different_classes_are_not_assignable(self):
        src = (
            "class A:\n"
            "    def __init__(self):\n"
            "        self.v = 1\n"
            "class B:\n"
            "    def __init__(self):\n"
            "        self.v = 1\n"
            "def main():\n"
            "    a = A()\n"
            "    a = B()\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("Cannot unify" in e for e in errors), errors)


class TestComparisonDunders(unittest.TestCase):
    _POINT = (
        "class Point:\n"
        "    def __init__(self, x: int, y: int):\n"
        "        self.x = x\n"
        "        self.y = y\n"
    )

    def test_eq_dunder_used_by_equality(self):
        src = (
            self._POINT +
            "    def __eq__(self, other):\n"
            "        return self.x == other.x and self.y == other.y\n"
            "def main():\n"
            "    a = Point(1, 2)\n"
            "    b = Point(1, 2)\n"
            "    r = a == b\n"
        )
        checker = typecheck(src)
        self.assertEqual(checker._errors, [])
        self.assertEqual(checker._funcs["main"].local_types["r"], PYBOOL)

    def test_ne_falls_back_to_not_eq_without_explicit_ne(self):
        src = (
            self._POINT +
            "    def __eq__(self, other):\n"
            "        return self.x == other.x\n"
            "def main():\n"
            "    a = Point(1, 2)\n"
            "    b = Point(3, 4)\n"
            "    print(a != b)\n"
        )
        self.assertEqual(typecheck_errors(src), [])

    def test_lt_dunder_used_by_less_than(self):
        src = (
            self._POINT +
            "    def __lt__(self, other):\n"
            "        return self.x < other.x\n"
            "def main():\n"
            "    a = Point(1, 2)\n"
            "    b = Point(3, 4)\n"
            "    print(a < b)\n"
        )
        self.assertEqual(typecheck_errors(src), [])

    def test_comparing_two_different_classes_is_an_error(self):
        src = (
            "class A:\n"
            "    def __init__(self):\n"
            "        self.v = 1\n"
            "class B:\n"
            "    def __init__(self):\n"
            "        self.v = 1\n"
            "def main():\n"
            "    a = A()\n"
            "    b = B()\n"
            "    print(a == b)\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("Cannot compare" in e for e in errors), errors)

    def test_comparing_class_to_non_class_is_an_error(self):
        src = (
            "class A:\n"
            "    def __init__(self):\n"
            "        self.v = 1\n"
            "def main():\n"
            "    a = A()\n"
            "    print(a == 5)\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("Cannot compare" in e for e in errors), errors)

    def test_missing_dunder_is_an_error(self):
        src = (
            self._POINT +
            "def main():\n"
            "    a = Point(1, 2)\n"
            "    b = Point(3, 4)\n"
            "    print(a < b)\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("no __lt__ defined" in e for e in errors), errors)

    def test_eq_dunder_must_return_bool(self):
        src = (
            self._POINT +
            "    def __eq__(self, other):\n"
            "        return self.x\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(any("must return a bool" in e for e in errors), errors)

    def test_eq_dunder_must_take_exactly_one_extra_argument(self):
        src = (
            self._POINT +
            "    def __eq__(self, other, extra):\n"
            "        return True\n"
        )
        errors = typecheck_errors(src)
        self.assertTrue(
            any("must take exactly one argument other than 'self'" in e for e in errors),
            errors)


if __name__ == "__main__":
    unittest.main()
