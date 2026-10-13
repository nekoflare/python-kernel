"""End-to-end tests for --target=hosted: compile with a real C++ compiler
(against libpython) and run the resulting binary, checking actual stdout.

Skipped automatically if g++ or Python.h aren't available.
"""
import unittest

from .helpers import HOSTED_TOOLCHAIN_AVAILABLE, ROOT, compile_and_run_hosted


@unittest.skipUnless(HOSTED_TOOLCHAIN_AVAILABLE,
                      "g++ and/or Python.h not available")
class TestBasics(unittest.TestCase):
    def test_hello_world_and_int_arithmetic(self):
        src = (
            'def main():\n'
            '    print("Hello, World!")\n'
            '    x = 42\n'
            '    y = x + 8\n'
            '    print(y)\n'
            'main()\n'
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Hello, World!\n50\n")

    def test_print42_example_file(self):
        src = (ROOT / "examples" / "print42.py").read_text()
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "42\n")

    def test_none_prints_as_none(self):
        result = compile_and_run_hosted("def main():\n    print(None)\nmain()\n")
        self.assertEqual(result.stdout, "None\n")

    def test_bool_literals_print_as_true_false(self):
        result = compile_and_run_hosted(
            "def main():\n    print(True)\n    print(False)\nmain()\n"
        )
        self.assertEqual(result.stdout, "True\nFalse\n")


@unittest.skipUnless(HOSTED_TOOLCHAIN_AVAILABLE,
                      "g++ and/or Python.h not available")
class TestFixedPrintBugs(unittest.TestCase):
    """Regression tests for print()'s separator/ordering handling, found
    while adding __str__ support (which makes mixing a literal label with
    a class instance a common pattern, e.g. print("Point:", p))."""

    def test_string_literal_followed_by_value_gets_a_separator(self):
        result = compile_and_run_hosted(
            'def main():\n    x = 5\n    print("value:", x)\nmain()\n'
        )
        self.assertEqual(result.stdout, "value: 5\n")

    def test_value_followed_by_string_literal_gets_a_separator(self):
        result = compile_and_run_hosted(
            'def main():\n    x = 5\n    print(x, "value")\nmain()\n'
        )
        self.assertEqual(result.stdout, "5 value\n")

    def test_multiple_string_literals_get_separators(self):
        result = compile_and_run_hosted('def main():\n    print("a", "b", "c")\nmain()\n')
        self.assertEqual(result.stdout, "a b c\n")


@unittest.skipUnless(HOSTED_TOOLCHAIN_AVAILABLE,
                      "g++ and/or Python.h not available")
class TestArithmetic(unittest.TestCase):
    def test_int_operators(self):
        src = (
            "def main():\n"
            "    print(7 // 2)\n"
            "    print(7 % 2)\n"
            "    print(2 * 3)\n"
            "    print(10 - 3)\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "3\n1\n6\n7\n")

    def test_true_div_is_always_float(self):
        result = compile_and_run_hosted("def main():\n    print(7 / 2)\nmain()\n")
        self.assertEqual(result.stdout, "3.5\n")

    def test_int_float_mix_widens_to_float(self):
        result = compile_and_run_hosted("def main():\n    print(3 + 4.5)\nmain()\n")
        self.assertEqual(result.stdout, "7.5\n")

    def test_xor(self):
        result = compile_and_run_hosted(
            "def main():\n    print(5 ^ 3)\n    print(6 ^ 6)\nmain()\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "6\n0\n")


@unittest.skipUnless(HOSTED_TOOLCHAIN_AVAILABLE,
                      "g++ and/or Python.h not available")
class TestControlFlow(unittest.TestCase):
    def test_while_with_if_break_continue(self):
        src = (
            "def main():\n"
            "    i = 0\n"
            "    while i < 5:\n"
            "        if i == 2:\n"
            "            i = i + 1\n"
            "            continue\n"
            "        if i == 4:\n"
            "            break\n"
            "        print(i)\n"
            "        i = i + 1\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0\n1\n3\n")

    def test_for_range_single_arg(self):
        src = "def main():\n    for i in range(5):\n        print(i)\nmain()\n"
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0\n1\n2\n3\n4\n")

    def test_if_elif_else(self):
        src = (
            "def classify(n: int) -> int:\n"
            "    if n < 0:\n"
            "        return -1\n"
            "    elif n == 0:\n"
            "        return 0\n"
            "    else:\n"
            "        return 1\n"
            "def main():\n"
            "    print(classify(-5))\n"
            "    print(classify(0))\n"
            "    print(classify(5))\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "-1\n0\n1\n")


@unittest.skipUnless(HOSTED_TOOLCHAIN_AVAILABLE,
                      "g++ and/or Python.h not available")
class TestFunctions(unittest.TestCase):
    def test_call_earlier_defined_function(self):
        src = (
            "def square(x: int) -> int:\n"
            "    return x * x\n"
            "def main():\n"
            "    print(square(5))\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "25\n")

    def test_recursion(self):
        src = (
            "def fact(n: int) -> int:\n"
            "    if n <= 1:\n"
            "        return 1\n"
            "    return n * fact(n - 1)\n"
            "def main():\n"
            "    print(fact(6))\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "720\n")

    def test_fibonacci_example_file_first_few_terms(self):
        # Run the shipped fibonacci example, but only fib(0..9) -- fib(19)
        # is still fast in hosted mode, but there's no need to depend on the
        # full 20-line example output to prove recursive calls work.
        src = (
            "def fibonacci(n: int) -> int:\n"
            "    if n <= 1:\n"
            "        return n\n"
            "    return fibonacci(n - 1) + fibonacci(n - 2)\n"
            "def main():\n"
            "    i = 0\n"
            "    while i < 10:\n"
            "        print(fibonacci(i))\n"
            "        i = i + 1\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0\n1\n1\n2\n3\n5\n8\n13\n21\n34\n")


@unittest.skipUnless(HOSTED_TOOLCHAIN_AVAILABLE,
                      "g++ and/or Python.h not available")
class TestGlobals(unittest.TestCase):
    def test_global_readable_at_module_top_level(self):
        result = compile_and_run_hosted("counter = 5\nprint(counter)\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "5\n")


@unittest.skipUnless(HOSTED_TOOLCHAIN_AVAILABLE,
                      "g++ and/or Python.h not available")
class TestChr(unittest.TestCase):
    def test_prints_the_right_character(self):
        result = compile_and_run_hosted(
            'def main():\n    print(chr(65))\n    print(chr(97))\nmain()\n'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "A\na\n")

    def test_assignable_to_a_variable(self):
        result = compile_and_run_hosted(
            "def main():\n    c = chr(66)\n    print(c)\nmain()\n"
        )
        self.assertEqual(result.stdout, "B\n")

    def test_multiple_chr_args_in_one_print_call(self):
        result = compile_and_run_hosted(
            "def main():\n    print(chr(72), chr(73))\nmain()\n"
        )
        self.assertEqual(result.stdout, "H I\n")

    def test_concatenation_prints_as_one_tight_word(self):
        # The point of "+" over multi-arg print(): no space gets inserted
        # between characters, unlike test_multiple_chr_args_in_one_print_call
        # above.
        result = compile_and_run_hosted(
            "def main():\n"
            "    s = chr(72) + chr(73)\n"
            "    s = s + chr(33)\n"
            "    print(s)\n"
            "main()\n"
        )
        self.assertEqual(result.stdout, "HI!\n")


@unittest.skipUnless(HOSTED_TOOLCHAIN_AVAILABLE,
                      "g++ and/or Python.h not available")
class TestHex(unittest.TestCase):
    def test_matches_real_python_hex(self):
        result = compile_and_run_hosted(
            "def main():\n"
            "    print(hex(255))\n"
            "    print(hex(0))\n"
            "    print(hex(-1))\n"
            "    print(hex(-255))\n"
            "    print(hex(4096))\n"
            "    print(hex(16))\n"
            "main()\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout,
                          hex(255) + "\n" + hex(0) + "\n" + hex(-1) + "\n" +
                          hex(-255) + "\n" + hex(4096) + "\n" + hex(16) + "\n")

    def test_assignable_to_a_variable(self):
        result = compile_and_run_hosted(
            "def main():\n    s = hex(42)\n    print(s)\nmain()\n"
        )
        self.assertEqual(result.stdout, "0x2a\n")

    def test_bool_arguments(self):
        result = compile_and_run_hosted(
            "def main():\n    print(hex(True))\n    print(hex(False))\nmain()\n"
        )
        self.assertEqual(result.stdout, "0x1\n0x0\n")

    def test_value_needing_the_full_unsigned_64_bits(self):
        # hex()'s first implementation used PyLong_AsLongLong (signed),
        # which raises OverflowError for any value in [2^63, 2^64) --
        # e.g. a canonical x86-64 higher-half address like
        # 0xffff800000000000 -- even though such a value fits perfectly
        # in 64 bits unsigned. Found while normalizing a peek64()'d
        # address for display in PythonOS/kernel/. Fixed by checking
        # the sign with a real comparison, then converting the
        # (always non-negative) magnitude with the unsigned counterpart.
        result = compile_and_run_hosted(
            "def main():\n"
            "    x = 2 ** 64 - 0x800000000000\n"
            "    print(hex(x))\n"
            "main()\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0xffff800000000000\n")


@unittest.skipUnless(HOSTED_TOOLCHAIN_AVAILABLE,
                      "g++ and/or Python.h not available")
class TestStringLiteralsAsValues(unittest.TestCase):
    def test_literal_assignable_to_a_variable(self):
        result = compile_and_run_hosted('def main():\n    s = "Hello"\n    print(s)\nmain()\n')
        self.assertEqual(result.stdout, "Hello\n")

    def test_literal_concatenated_with_chr_either_side(self):
        result = compile_and_run_hosted(
            'def main():\n'
            '    print("Hi, " + chr(65))\n'
            '    print(chr(65) + " there")\n'
            '    print("a" + "b")\n'
            'main()\n'
        )
        self.assertEqual(result.stdout, "Hi, A\nA there\nab\n")

    def test_str_param_and_return_annotations(self):
        result = compile_and_run_hosted(
            'def greet(name: str) -> str:\n'
            '    return "Hello, " + name\n'
            'def main():\n'
            '    print(greet("Bob"))\n'
            'main()\n'
        )
        self.assertEqual(result.stdout, "Hello, Bob\n")


@unittest.skipUnless(HOSTED_TOOLCHAIN_AVAILABLE,
                      "g++ and/or Python.h not available")
class TestStringOperations(unittest.TestCase):
    def test_len(self):
        result = compile_and_run_hosted('def main():\n    print(len("hello"))\nmain()\n')
        self.assertEqual(result.stdout, "5\n")

    def test_indexing_including_negative(self):
        result = compile_and_run_hosted(
            'def main():\n'
            '    s = "hello"\n'
            '    print(s[0])\n'
            '    print(s[-1])\n'
            'main()\n'
        )
        self.assertEqual(result.stdout, "h\no\n")

    def test_slicing(self):
        result = compile_and_run_hosted(
            'def main():\n'
            '    s = "hello"\n'
            '    print(s[1:3])\n'
            '    print(s[:2])\n'
            '    print(s[3:])\n'
            '    print(s[:])\n'
            'main()\n'
        )
        self.assertEqual(result.stdout, "el\nhe\nlo\nhello\n")

    def test_extended_slicing_with_step(self):
        result = compile_and_run_hosted(
            'def main():\n'
            '    s = "Hello, World!"\n'
            '    print(s[::-1])\n'
            '    print(s[0:10:2])\n'
            '    print(s[10:0:-2])\n'
            '    print(s[::2])\n'
            '    print(s[100:])\n'
            '    print(s[5:2])\n'
            'main()\n'
        )
        self.assertEqual(
            result.stdout,
            "!dlroW ,olleH\nHlo o\nlo ol\nHlo ol!\n\n\n",
        )

    def test_extended_slicing_zero_step_is_a_fatal_error(self):
        # No exception mechanism in this language subset -- a zero step
        # (which real Python raises ValueError for) aborts the process,
        # same as the runtime's other "can't happen, but just in case"
        # checks (e.g. division by zero).
        result = compile_and_run_hosted(
            'def main():\n'
            '    s = "hi"\n'
            '    step = 0\n'
            '    print(s[::step])\n'
            'main()\n'
        )
        self.assertNotEqual(result.returncode, 0)

    def test_comparison(self):
        result = compile_and_run_hosted(
            'def main():\n'
            '    print("abc" == "abc")\n'
            '    print("abc" == "xyz")\n'
            '    print("abc" != "xyz")\n'
            '    print("abc" < "abd")\n'
            '    print("abd" < "abc")\n'
            'main()\n'
        )
        self.assertEqual(result.stdout, "True\nFalse\nTrue\nTrue\nFalse\n")

    def test_multiplication_either_order(self):
        result = compile_and_run_hosted(
            'def main():\n'
            '    print("ab" * 3)\n'
            '    print(3 * "ab")\n'
            'main()\n'
        )
        self.assertEqual(result.stdout, "ababab\nababab\n")


@unittest.skipUnless(HOSTED_TOOLCHAIN_AVAILABLE,
                      "g++ and/or Python.h not available")
class TestHardwareIntrinsicsRejectedInHostedMode(unittest.TestCase):
    def test_port_io_rejected(self):
        src = "def main():\n    x = inb(0x60)\nmain()\n"
        with self.assertRaises(AssertionError) as cm:
            compile_and_run_hosted(src, compile_args=["--enable-dangerous-io-ops"])
        self.assertIn("only available in freestanding mode", str(cm.exception))


@unittest.skipUnless(HOSTED_TOOLCHAIN_AVAILABLE,
                      "g++ and/or Python.h not available")
class TestFixedIntegrationBugs(unittest.TestCase):
    """Regression tests for end-to-end behavior that used to be broken.

    Each was originally observed as a real compile or runtime failure by
    actually invoking g++ on generated output.
    """

    def test_comparison_used_as_a_value(self):
        # _emit_compare() now boxes its result as a real PyObject* bool
        # singleton instead of a native C++ `bool`, so a comparison works
        # as a plain value (assigned, printed, returned, ...) and not just
        # inline as an if/while test.
        result = compile_and_run_hosted(
            "def main():\n    x = 1 < 2\n    print(x)\nmain()\n"
        )
        self.assertEqual(result.stdout, "True\n")

    def test_variable_assigned_in_both_branches_of_if_else(self):
        # Found while adding a __str__ method that assigned a `label`
        # local inside both branches of an if/else and used it
        # afterward: codegen used to emit the PyObject* declaration
        # inside whichever branch happened to compile first, making it
        # invisible (a real C++ compile error) to the sibling branch and
        # to code after the if/else, even though Python considers the
        # name in scope for the rest of the function. See
        # CppCodegen._collect_assigned_names.
        result = compile_and_run_hosted(
            "def main():\n"
            "    x = 1\n"
            "    if x == 1:\n"
            "        label = 5\n"
            "    else:\n"
            "        label = 10\n"
            "    print(label)\n"
            "main()\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "5\n")

    def test_variable_assigned_only_inside_a_while_loop(self):
        result = compile_and_run_hosted(
            "def main():\n"
            "    i = 0\n"
            "    while i < 3:\n"
            "        total = i\n"
            "        i = i + 1\n"
            "    print(total)\n"
            "main()\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "2\n")

    def test_power_operator_computes_correct_value(self):
        # ast_to_ir now maps ast.Pow to a real IR.BinOp.POW (it used to
        # alias IR.BinOp.MUL as an unfinished placeholder), and codegen
        # emits PyNumber_Power / _pyck_bignum_pow for it.
        result = compile_and_run_hosted("def main():\n    print(2 ** 3)\nmain()\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "8\n")

    def test_two_for_loops_reusing_the_same_variable_name(self):
        # _emit_for_range() now checks _declared_vars (like _emit_assign
        # already did) before declaring the loop variable, so two
        # sequential for-loops over the same variable name -- an ordinary
        # pattern (`for i in ...` twice) -- compile correctly.
        src = (
            "def main():\n"
            "    for i in range(3):\n"
            "        print(i)\n"
            "    for i in range(2):\n"
            "        print(i)\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0\n1\n2\n0\n1\n")

    def test_for_loop_variable_reused_after_prior_assignment(self):
        # Same fix as test_two_for_loops_reusing_the_same_variable_name,
        # triggered by a plain assignment before the loop instead of a
        # second loop.
        src = (
            "def main():\n"
            "    i = 99\n"
            "    for i in range(3):\n"
            "        print(i)\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0\n1\n2\n")

    def test_for_range_with_explicit_step_in_hosted_mode(self):
        # _emit_for_range()'s step branch calls _pyck_int_is_negative() in
        # every mode, but that helper used to only be declared/defined by
        # the freestanding runtime. The hosted header (_emit_header) now
        # defines it too, so an explicit range() step compiles in hosted
        # mode exactly like it already did in freestanding mode (see
        # test_integration_freestanding.py).
        src = "def main():\n    for i in range(0, 10, 2):\n        print(i)\nmain()\n"
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0\n2\n4\n6\n8\n")

    def test_function_referencing_global_variable(self):
        # Two fixes needed to combine here: the typechecker now infers
        # globals before function bodies (see test_typechecker.py's
        # test_global_variable_visible_inside_function_body), and codegen
        # now declares module-level Python variables as real C++ globals
        # (generate()) instead of only as locals inside pyck_main(), so
        # other functions can actually see them.
        src = (
            "counter = 0\n"
            "def bump() -> int:\n"
            "    return counter + 1\n"
            "def main():\n"
            "    print(bump())\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "1\n")


@unittest.skipUnless(HOSTED_TOOLCHAIN_AVAILABLE,
                      "g++ and/or Python.h not available")
class TestClasses(unittest.TestCase):
    def test_basic_class_construct_read_and_method(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int, y: int):\n"
            "        self.x = x\n"
            "        self.y = y\n"
            "    def total(self):\n"
            "        return self.x + self.y\n"
            "def main():\n"
            "    p = Point(3, 4)\n"
            "    print(p.x)\n"
            "    print(p.y)\n"
            "    print(p.total())\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "3\n4\n7\n")

    def test_method_mutates_attribute_via_self(self):
        src = (
            "class Counter:\n"
            "    def __init__(self):\n"
            "        self.n = 0\n"
            "    def bump(self):\n"
            "        self.n = self.n + 1\n"
            "def main():\n"
            "    c = Counter()\n"
            "    c.bump()\n"
            "    c.bump()\n"
            "    c.bump()\n"
            "    print(c.n)\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "3\n")

    def test_attribute_assignment_from_outside_the_class(self):
        src = (
            "class Box:\n"
            "    def __init__(self):\n"
            "        self.value = 0\n"
            "def main():\n"
            "    b = Box()\n"
            "    b.value = 42\n"
            "    print(b.value)\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "42\n")

    def test_two_instances_are_independent(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int):\n"
            "        self.x = x\n"
            "def main():\n"
            "    a = Point(1)\n"
            "    b = Point(2)\n"
            "    print(a.x)\n"
            "    print(b.x)\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "1\n2\n")

    def test_class_with_no_init(self):
        src = (
            "class Foo:\n"
            "    def greet(self):\n"
            "        print(42)\n"
            "def main():\n"
            "    f = Foo()\n"
            "    f.greet()\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "42\n")

    def test_method_calling_another_method_via_self_and_boolean_method(self):
        src = (
            "class Calc:\n"
            "    def __init__(self, base: int):\n"
            "        self.base = base\n"
            "    def double(self):\n"
            "        return self.base * 2\n"
            "    def quadruple(self):\n"
            "        return self.double() * 2\n"
            "    def is_big(self):\n"
            "        return self.quadruple() > 10\n"
            "def main():\n"
            "    small = Calc(1)\n"
            "    big = Calc(3)\n"
            "    print(small.quadruple())\n"
            "    print(big.quadruple())\n"
            "    print(small.is_big())\n"
            "    print(big.is_big())\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "4\n12\nFalse\nTrue\n")

    def test_method_with_arguments(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int, y: int):\n"
            "        self.x = x\n"
            "        self.y = y\n"
            "    def move(self, dx: int, dy: int):\n"
            "        self.x = self.x + dx\n"
            "        self.y = self.y + dy\n"
            "def main():\n"
            "    p = Point(1, 1)\n"
            "    p.move(2, 3)\n"
            "    print(p.x)\n"
            "    print(p.y)\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "3\n4\n")

    def test_str_dunder_is_used_by_print(self):
        src = (
            "class Greeting:\n"
            "    def __init__(self, name: str):\n"
            "        self.name = name\n"
            "    def __str__(self):\n"
            "        return \"Hello, \" + self.name + \"!\"\n"
            "def main():\n"
            "    g = Greeting(\"World\")\n"
            "    print(g)\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Hello, World!\n")

    def test_str_dunder_mixed_with_a_literal_label(self):
        # Exercises the same literal+value print path fixed in
        # TestFixedPrintBugs, but with a class instance as the value.
        src = (
            "class Greeting:\n"
            "    def __init__(self, name: str):\n"
            "        self.name = name\n"
            "    def __str__(self):\n"
            "        return \"Hello, \" + self.name + \"!\"\n"
            "def main():\n"
            "    g = Greeting(\"World\")\n"
            "    print(\"Greeting:\", g)\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Greeting: Hello, World!\n")

    def test_repr_dunder_used_as_fallback_when_no_str(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int, y: int):\n"
            "        self.x = x\n"
            "        self.y = y\n"
            "    def __repr__(self):\n"
            "        return \"a point\"\n"
            "def main():\n"
            "    p = Point(1, 2)\n"
            "    print(p)\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "a point\n")

    def test_class_without_str_still_prints_something(self):
        src = (
            "class Foo:\n"
            "    def __init__(self):\n"
            "        self.x = 1\n"
            "def main():\n"
            "    f = Foo()\n"
            "    print(f)\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout)  # no crash; exact repr is unspecified

    def test_eq_and_lt_dunders(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int, y: int):\n"
            "        self.x = x\n"
            "        self.y = y\n"
            "    def __eq__(self, other):\n"
            "        return self.x == other.x and self.y == other.y\n"
            "    def __lt__(self, other):\n"
            "        return self.x < other.x\n"
            "def main():\n"
            "    a = Point(1, 2)\n"
            "    b = Point(1, 2)\n"
            "    c = Point(3, 4)\n"
            "    print(a == b)\n"
            "    print(a == c)\n"
            "    print(a != c)\n"
            "    print(a < c)\n"
            "    print(c < a)\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "True\nFalse\nTrue\nTrue\nFalse\n")

    def test_ne_falls_back_to_not_eq(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int):\n"
            "        self.x = x\n"
            "    def __eq__(self, other):\n"
            "        return self.x == other.x\n"
            "def main():\n"
            "    a = Point(1)\n"
            "    b = Point(2)\n"
            "    print(a != b)\n"
            "    print(a != a)\n"
            "main()\n"
        )
        result = compile_and_run_hosted(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "True\nFalse\n")


if __name__ == "__main__":
    unittest.main()
