"""End-to-end tests for --target=freestanding (x86).

Two tiers:
  - Compile-only: run the real cross/freestanding toolchain (g++ -m32
    -ffreestanding ... -T linker.ld) and check we get a linked ELF binary.
    Skipped unless g++ can produce -m32 binaries.
  - Boot tests: additionally build a GRUB multiboot ISO and boot it in
    QEMU, capturing the serial (COM1) output that runtime/freestanding's
    py_print writes to alongside VGA. This actually proves the generated
    kernel runs and prints the right thing on real (emulated) hardware,
    not just that it compiles. Skipped unless qemu-system-i386 and
    grub-mkrescue are both available.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from .helpers import (
    FREESTANDING_BOOT_AVAILABLE,
    FREESTANDING_TOOLCHAIN_AVAILABLE,
    ROOT,
    boot_and_capture_serial,
    compile_freestanding_elf,
)


class _TempDirMixin:
    def make_workdir(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d


@unittest.skipUnless(FREESTANDING_TOOLCHAIN_AVAILABLE, "g++ -m32 not available")
class TestCompileOnly(_TempDirMixin, unittest.TestCase):
    def test_print42_example_compiles_and_links(self):
        src = (ROOT / "examples" / "print42.py").read_text()
        elf = compile_freestanding_elf(self.make_workdir(), src)
        self.assertTrue(elf.exists())
        # Sanity check it's a real i386 ELF, not e.g. an empty/stub file.
        header = elf.read_bytes()[:20]
        self.assertEqual(header[:4], b"\x7fELF")
        self.assertEqual(header[4], 1)  # ELFCLASS32

    def test_kernel_hello_example_compiles_and_links(self):
        src = (ROOT / "examples" / "kernel_hello.py").read_text()
        elf = compile_freestanding_elf(self.make_workdir(), src)
        self.assertTrue(elf.exists())

    def test_recursion_compiles(self):
        src = (
            "def fact(n: int) -> int:\n"
            "    if n <= 1:\n"
            "        return 1\n"
            "    return n * fact(n - 1)\n"
            "def main():\n"
            "    print(fact(5))\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src)
        self.assertTrue(elf.exists())

    def test_stack_allocated_class_instances_compile_and_link(self):
        # Exercises pyck/escape.py's stack-allocation path end to end
        # against the real -m32 freestanding toolchain: a non-escaping
        # instance (compute's `p`), an escaping one that stays heap
        # allocated (leak's `p`, returned), and a loop that reuses one
        # stack slot across iterations. See tests/test_escape.py and
        # tests/test_codegen.py's TestStackAllocation for the analysis-
        # and codegen-level checks this only needs to additionally prove
        # actually compiles under real freestanding kernel flags.
        src = (
            "class Point:\n"
            "    def __init__(self, x: int, y: int):\n"
            "        self.x = x\n"
            "        self.y = y\n"
            "    def total(self) -> int:\n"
            "        return self.x + self.y\n"
            "def compute() -> int:\n"
            "    p = Point(3, 4)\n"
            "    return p.total()\n"
            "def leak():\n"
            "    p = Point(1, 2)\n"
            "    return p\n"
            "def loop() -> int:\n"
            "    total = 0\n"
            "    i = 0\n"
            "    while i < 3:\n"
            "        p = Point(i, i)\n"
            "        total = total + p.total()\n"
            "        i = i + 1\n"
            "    return total\n"
            "def main():\n"
            "    print(compute())\n"
            "    leak()\n"
            "    print(loop())\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src)
        self.assertTrue(elf.exists())


@unittest.skipUnless(FREESTANDING_BOOT_AVAILABLE,
                      "qemu-system-i386 and/or grub-mkrescue not available")
class TestBootAndRun(_TempDirMixin, unittest.TestCase):
    def test_print42_prints_42_over_serial(self):
        src = (ROOT / "examples" / "print42.py").read_text()
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("42", output)

    def test_kernel_hello_prints_greeting_and_arithmetic(self):
        src = (ROOT / "examples" / "kernel_hello.py").read_text()
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("Hello, PyCk!", output)
        self.assertIn("3", output)  # x = 1 + 2

    def test_xor(self):
        src = "def main():\n    print(5 ^ 3)\n    print(6 ^ 6)\nmain()\n"
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("6", output)
        self.assertIn("0", output)

    def test_hardware_intrinsics(self):
        # Port 0x80 is the classic ISA "diagnostic POST code" port: reads
        # from an unmapped/no-op port like this reliably come back as all
        # 1-bits in QEMU, so those three values are safe to assert exactly.
        # MSR/CPUID/control-register values vary by QEMU version/machine
        # type, so those are only checked for "didn't crash" via the
        # trailing sentinel print.
        src = (
            "def main():\n"
            "    print(inb(0x80))\n"
            "    print(inw(0x80))\n"
            "    print(inl(0x80))\n"
            "    outb(0x80, 1)\n"
            "    outw(0x80, 1)\n"
            "    outl(0x80, 1)\n"
            "    wrmsr(0x1B, rdmsr(0x1B))\n"
            "    write_cr0(read_cr0())\n"
            "    print(read_cr0() & 1)\n"  # CR0.PE is always set
            "    print(999)\n"  # sentinel: reached the end without crashing
            "main()\n"
        )
        elf = compile_freestanding_elf(
            self.make_workdir(), src, compile_args=["--enable-dangerous-io-ops"]
        )
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("255\n65535\n4294967295", output)
        self.assertIn("1\n999", output)

    def test_chr(self):
        src = 'def main():\n    print(chr(65))\n    print(chr(97))\nmain()\n'
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("A\na", output)

    def test_chr_concatenation(self):
        src = (
            "def main():\n"
            "    s = chr(72) + chr(73)\n"
            "    s = s + chr(33)\n"
            "    print(s)\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("HI!", output)

    def test_hex(self):
        src = (
            "def main():\n"
            "    print(hex(255))\n"
            "    print(hex(0))\n"
            "    print(hex(-1))\n"
            "    print(hex(-255))\n"
            "    print(hex(4096))\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("0xff\n0x0\n-0x1\n-0xff\n0x1000", output)

    def test_hex_of_a_value_needing_the_full_unsigned_64_bits(self):
        # See the same-named test in test_integration_hosted.py: hex()'s
        # first implementation silently mishandled any value in
        # [2^63, 2^64), which fits perfectly in 64 bits unsigned but has
        # no signed 64-bit representation at all.
        src = (
            "def main():\n"
            "    print(hex(True))\n"
            "    x = 2 ** 64 - 0x800000000000\n"
            "    print(hex(x))\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("0x1\n0xffff800000000000", output)

    def test_string_literal_as_a_value(self):
        src = (
            'def greet(name: str) -> str:\n'
            '    return "Hello, " + name\n'
            'def main():\n'
            '    s = "Hi"\n'
            '    print(s)\n'
            '    print("a" + "b")\n'
            '    print(greet("World"))\n'
            'main()\n'
        )
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("Hi\nab\nHello, World", output)

    def test_string_operations(self):
        # len(), indexing (incl. negative), slicing, comparison, and
        # multiplication -- exercises _pyck_str_length/_getitem/_slice/
        # _cmp/_repeat for real on a booted kernel, not just codegen text.
        src = (
            'def main():\n'
            '    s = "hello"\n'
            '    print(len(s))\n'
            '    print(s[0])\n'
            '    print(s[-1])\n'
            '    print(s[1:3])\n'
            '    print(s[:2])\n'
            '    print(s[3:])\n'
            '    print(s == "hello")\n'
            '    print(s == "world")\n'
            '    print("abc" < "abd")\n'
            '    print(s * 2)\n'
            '    print(2 * s)\n'
            'main()\n'
        )
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn(
            "5\nh\no\nel\nhe\nlo\nTrue\nFalse\nTrue\nhellohello\nhellohello",
            output,
        )

    def test_extended_slicing_with_step(self):
        src = (
            'def main():\n'
            '    s = "Hello, World!"\n'
            '    print(s[::-1])\n'
            '    print(s[0:10:2])\n'
            '    print(s[10:0:-2])\n'
            '    print(s[::2])\n'
            'main()\n'
        )
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("!dlroW ,olleH\nHlo o\nlo ol\nHlo ol!", output)

    def test_for_range_with_explicit_step_actually_works_here(self):
        # Freestanding mode's runtime has always defined
        # _pyck_int_is_negative (hosted mode needed it added -- see
        # test_integration_hosted.TestFixedIntegrationBugs.
        # test_for_range_with_explicit_step_in_hosted_mode), so an explicit
        # range() step compiles and runs correctly here.
        src = "def main():\n    for i in range(0, 10, 2):\n        print(i)\nmain()\n"
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        for expected in ("0", "2", "4", "6", "8"):
            self.assertIn(expected, output)

    def test_recursion_with_decrement(self):
        # Exercises fixed _pyck_bignum_sub (see TestFixedRuntimeBugs.
        # test_integer_subtraction below): fact(5) recurses via
        # fact(n - 1), which only reaches its base case if subtraction
        # actually decrements.
        src = (
            "def fact(n: int) -> int:\n"
            "    if n <= 1:\n"
            "        return 1\n"
            "    return n * fact(n - 1)\n"
            "def main():\n"
            "    print(fact(5))\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("120", output)


@unittest.skipUnless(FREESTANDING_BOOT_AVAILABLE,
                      "qemu-system-i386 and/or grub-mkrescue not available")
class TestFixedRuntimeBugs(_TempDirMixin, unittest.TestCase):
    """Regression test for freestanding runtime arithmetic that used to be
    broken, discovered by actually booting compiled kernels in QEMU.
    """

    def test_integer_subtraction(self):
        # runtime/freestanding/py_runtime.cpp's _pyck_bignum_sub() used to
        # negate b (flip ob_size's sign) and then call _long_normalize(),
        # whose trim loop only works when handed a non-negative size --
        # fed a negative one, it silently flipped the sign straight back to
        # positive, undoing the negation. _pyck_bignum_sub(a, b) computed
        # a + b instead of a - b for every non-negative b. The redundant
        # (and unsafe-on-a-negated-size) normalize call has been removed;
        # a plain sign-flipped copy of an already-normalized value is
        # already normalized. This also used to make any freestanding
        # function that recurses by decrementing (`f(n - 1)`) hang, since
        # it never reached its base case -- see
        # test_recursion_with_decrement in TestBootAndRun below.
        src = "def main():\n    print(5 - 3)\n    print(1 - 1)\nmain()\n"
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("2", output)
        self.assertIn("0", output)
        self.assertNotIn("8", output)

    def test_subtraction_producing_a_negative_result(self):
        # A second, distinct bug in the same neighborhood: _pyck_bignum_add
        # (which _pyck_bignum_sub delegates to after negating its second
        # operand) computed the correctly-signed, already-normalized
        # result in each branch, then called _long_normalize() on it
        # *again* unconditionally before returning -- and since that
        # result's ob_size could now be negative, this hit the exact same
        # "_long_normalize() silently flips a negative size back to
        # positive" bug as test_integer_subtraction above, just one level
        # up the call stack. `3 - 5` printed `2` instead of `-2`; `0 - 5`
        # printed `5` instead of `-5`. The redundant trailing normalize()
        # call has been removed.
        src = "def main():\n    print(3 - 5)\n    print(0 - 5)\nmain()\n"
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("-2", output)
        self.assertIn("-5", output)

    def test_repeated_floor_division_gives_correct_results(self):
        # _long_divmod's "repeated subtraction" quotient loop set
        # q->ob_size to the *subtraction count* (e.g. 25 for 255 // 10)
        # instead of the actual digit count the quotient occupies (1,
        # since 25 fits in a single base-2^30 digit). Every later reader
        # of that quotient (_long_normalize, _pyck_long_as_long, and any
        # further arithmetic on it) then read past the end of its real
        # digit array into whatever adjacent heap memory followed --
        # garbage that was zero often enough (fresh, never-yet-used heap)
        # for a first division to look fine, but not for a second
        # division performed on that (actually out-of-bounds) result:
        # 255 // 10 // 10 used to hang/exhaust the heap on the second
        # division instead of reaching 2.
        src = (
            "def main():\n"
            "    n = 255\n"
            "    n = n // 10\n"
            "    n = n // 10\n"
            "    print(n)\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("2", output)

    def test_division_with_a_large_quotient_does_not_exhaust_the_heap(self):
        # A second, distinct bug in the same function: dividing via
        # repeated single-unit subtraction needs one allocating loop
        # iteration *per unit of the quotient's value* -- 142857 of them
        # for 1000000 // 7 -- which exhausts even a generously-sized heap
        # on an entirely ordinary division. Replaced with binary long
        # division (doubling the divisor as far as it still fits, then
        # subtracting), which takes O(log(quotient)) iterations instead.
        src = (
            "def main():\n"
            "    print(1000000 // 7)\n"
            "    print(1000000 % 7)\n"
            "    print(999999999 // 1000)\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("142857\n1\n999999", output)


@unittest.skipUnless(FREESTANDING_BOOT_AVAILABLE,
                      "qemu-system-i386 and/or grub-mkrescue not available")
class TestClasses(_TempDirMixin, unittest.TestCase):
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
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("3\n4\n7", output)

    def test_method_mutates_attribute_and_instances_are_independent(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int, y: int):\n"
            "        self.x = x\n"
            "        self.y = y\n"
            "    def move(self, dx: int, dy: int):\n"
            "        self.x = self.x + dx\n"
            "        self.y = self.y + dy\n"
            "def main():\n"
            "    a = Point(1, 1)\n"
            "    b = Point(10, 10)\n"
            "    a.move(2, 3)\n"
            "    print(a.x)\n"
            "    print(a.y)\n"
            "    print(b.x)\n"
            "    print(b.y)\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("3\n4\n10\n10", output)

    def test_class_with_no_init_and_unknown_type_print_fallback(self):
        src = (
            "class Foo:\n"
            "    def greet(self):\n"
            "        print(42)\n"
            "def main():\n"
            "    f = Foo()\n"
            "    f.greet()\n"
            "    print(f)\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("42\n<Foo object>", output)

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
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("4\n12\nFalse\nTrue", output)

    def test_str_dunder_is_used_by_print(self):
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
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("Greeting: Hello, World!", output)

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
        elf = compile_freestanding_elf(self.make_workdir(), src)
        output = boot_and_capture_serial(elf, timeout=8)
        self.assertIn("True\nFalse\nTrue\nTrue\nFalse", output)


if __name__ == "__main__":
    unittest.main()
