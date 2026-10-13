"""End-to-end tests for --target=freestanding --arch=x86_64 (Limine).

Mirrors test_integration_freestanding.py's structure for the 32-bit
Multiboot1/GRUB target, but for the newer 64-bit Limine-booted target:
  - Compile-only: run the real -m64 freestanding toolchain and check we
    get a linked ELF64 kernel. Skipped unless g++ can produce the
    required -m64/-mcmodel=kernel object code.
  - Boot tests: additionally fetch+build the pinned Limine release
    (cached after the first run -- see helpers.ensure_limine_binaries),
    assemble a bootable ISO, and boot it in QEMU (qemu-system-x86_64),
    capturing the serial (COM1) output that arch/x86_64/py_print_serial.cpp
    writes. Skipped unless the host toolchain (cc, xorriso,
    qemu-system-x86_64) is available; further skipped at runtime if
    fetching Limine itself fails (e.g. no network).
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from .helpers import (
    FREESTANDING_X86_64_BOOT_TOOLING_AVAILABLE,
    FREESTANDING_X86_64_TOOLCHAIN_AVAILABLE,
    ROOT,
    boot_and_capture_serial_limine,
    compile_freestanding_elf,
    ensure_limine_binaries,
)


class _TempDirMixin:
    def make_workdir(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d


@unittest.skipUnless(FREESTANDING_X86_64_TOOLCHAIN_AVAILABLE,
                      "g++ -m64/-mcmodel=kernel not available")
class TestCompileOnly(_TempDirMixin, unittest.TestCase):
    def test_print42_example_compiles_and_links(self):
        src = (ROOT / "examples" / "print42.py").read_text()
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        self.assertTrue(elf.exists())
        header = elf.read_bytes()[:20]
        self.assertEqual(header[:4], b"\x7fELF")
        self.assertEqual(header[4], 2)  # ELFCLASS64

    def test_kernel_hello_example_compiles_and_links(self):
        src = (ROOT / "examples" / "kernel_hello.py").read_text()
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        self.assertTrue(elf.exists())

    def test_entry_point_is_higher_half(self):
        # Kernels booted via Limine must be linked in the topmost 2GiB of
        # the address space (arch/x86_64/linker.ld places them at
        # 0xffffffff80000000) -- sanity-check the ELF header's entry point
        # actually landed there rather than, say, silently falling back to
        # a low, non-higher-half address.
        src = "def main():\n    print(1)\nmain()\n"
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        data = elf.read_bytes()
        # ELF64 header: e_entry is an 8-byte little-endian field at offset 24.
        entry = int.from_bytes(data[24:32], "little")
        self.assertGreaterEqual(entry, 0xFFFFFFFF80000000)

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
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        self.assertTrue(elf.exists())

    def test_stack_allocated_class_instances_compile_and_link(self):
        # See test_integration_freestanding.py's counterpart -- same
        # non-escaping/escaping/loop mix, checked here against the
        # -m64/-mcmodel=kernel Limine toolchain instead of -m32.
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
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        self.assertTrue(elf.exists())


@unittest.skipUnless(FREESTANDING_X86_64_BOOT_TOOLING_AVAILABLE,
                      "cc/xorriso/qemu-system-x86_64 not available")
class TestBootAndRun(_TempDirMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            ensure_limine_binaries()
        except RuntimeError as exc:
            raise unittest.SkipTest(f"could not fetch/build Limine: {exc}")

    def test_print42_prints_42_over_serial(self):
        src = (ROOT / "examples" / "print42.py").read_text()
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("42", output)

    def test_kernel_hello_prints_greeting_and_arithmetic(self):
        src = (ROOT / "examples" / "kernel_hello.py").read_text()
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("Hello, PyCk!", output)
        self.assertIn("3", output)  # x = 1 + 2

    def test_xor(self):
        src = "def main():\n    print(5 ^ 3)\n    print(6 ^ 6)\nmain()\n"
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("6", output)
        self.assertIn("0", output)

    def test_hardware_intrinsics(self):
        # Same rationale as the 32-bit x86 version of this test in
        # test_integration_freestanding.py: port 0x80 (ISA diagnostic port)
        # reliably reads back as all 1-bits; MSR/CPUID/control-register
        # values vary by QEMU version/machine type, so those are only
        # checked for "didn't crash" via the trailing sentinel.
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
            self.make_workdir(), src, arch="x86_64",
            compile_args=["--enable-dangerous-io-ops"],
        )
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("255\n65535\n4294967295", output)
        self.assertIn("1\n999", output)

    def test_memory_map_via_limine_table(self):
        # The exact layout (entry count, base addresses, region sizes)
        # varies by QEMU version/machine type/memory size, so this only
        # checks structural facts that must hold regardless: at least one
        # entry, at least one usable region, and no crash walking the
        # whole map. _LIMINE() is the only compiler builtin; everything
        # else -- finding the memmap request by its magic id in the
        # table, its response pointer, entry_count/entries/base/length/
        # type -- is decoded here with peek64 (see
        # arch/x86_64_shared/limine_table.cpp and PythonOS/kernel/'s
        # real limine_response/MemoryMap/MemoryMapEntry), so this needs
        # --enable-dangerous-memory-ops (peek64's flag), unlike _LIMINE()
        # itself which needs no flag.
        src = (
            "def unsigned64(x: int):\n"
            "    if x < 0:\n"
            "        return x + 2 ** 64\n"
            "    return x\n"
            "def limine_response(magic_a: int, magic_b: int):\n"
            "    table = _LIMINE()\n"
            "    result = 0\n"
            "    i = 0\n"
            "    entry_addr = peek64(table)\n"
            "    while entry_addr != 0:\n"
            "        a = unsigned64(peek64(entry_addr + 16))\n"
            "        b = unsigned64(peek64(entry_addr + 24))\n"
            "        if a == magic_a and b == magic_b:\n"
            "            result = peek64(entry_addr + 40)\n"
            "        i = i + 1\n"
            "        entry_addr = peek64(table + i * 8)\n"
            "    return result\n"
            "def main():\n"
            "    response = limine_response(0x67cf3d9d378a806f, 0xe304acdfc50c3c62)\n"
            "    print(response != 0)\n"
            "    n = peek64(response + 8)\n"
            "    entries_ptr = peek64(response + 16)\n"
            "    print(n > 0)\n"
            "    i = 0\n"
            "    saw_usable = False\n"
            "    while i < n:\n"
            "        entry_addr = peek64(entries_ptr + i * 8)\n"
            "        b = peek64(entry_addr)\n"
            "        l = peek64(entry_addr + 8)\n"
            "        t = peek64(entry_addr + 16)\n"
            "        if t == 0:\n"
            "            saw_usable = True\n"
            "        i = i + 1\n"
            "    print(saw_usable)\n"
            "    print(999)\n"  # sentinel: reached the end without crashing
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64",
                                       compile_args=["--enable-dangerous-memory-ops"])
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("True\nTrue\nTrue\n999", output)

    def test_limine_table_finds_hhdm_a_second_distinct_feature(self):
        # Proof the mechanism is genuinely generic, not memmap-specific:
        # the same limine_response() lookup finds a completely different
        # Limine feature (HHDM) by its own magic id. HHDM's response is
        # {uint64_t revision; uint64_t offset;}, so offset sits at +8.
        # The standard Limine default HHDM base for x86-64 is
        # 0xffff800000000000.
        src = (
            "def unsigned64(x: int):\n"
            "    if x < 0:\n"
            "        return x + 2 ** 64\n"
            "    return x\n"
            "def limine_response(magic_a: int, magic_b: int):\n"
            "    table = _LIMINE()\n"
            "    result = 0\n"
            "    i = 0\n"
            "    entry_addr = peek64(table)\n"
            "    while entry_addr != 0:\n"
            "        a = unsigned64(peek64(entry_addr + 16))\n"
            "        b = unsigned64(peek64(entry_addr + 24))\n"
            "        if a == magic_a and b == magic_b:\n"
            "            result = peek64(entry_addr + 40)\n"
            "        i = i + 1\n"
            "        entry_addr = peek64(table + i * 8)\n"
            "    return result\n"
            "def main():\n"
            "    response = limine_response(0x48dcf1cb8ad2b852, 0x63984e959a98244b)\n"
            "    offset = peek64(response + 8)\n"
            "    print(hex(unsigned64(offset)))\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64",
                                       compile_args=["--enable-dangerous-memory-ops"])
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("0xffff800000000000", output)

    def test_chr(self):
        src = 'def main():\n    print(chr(65))\n    print(chr(97))\nmain()\n'
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("A\na", output)

    def test_chr_concatenation(self):
        src = (
            "def main():\n"
            "    s = chr(72) + chr(73)\n"
            "    s = s + chr(33)\n"
            "    print(s)\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
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
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("0xff\n0x0\n-0x1\n-0xff\n0x1000", output)

    def test_hex_of_a_value_needing_the_full_unsigned_64_bits(self):
        # See the same-named test in test_integration_hosted.py: hex()'s
        # first implementation silently mishandled any value in
        # [2^63, 2^64) -- e.g. a canonical x86-64 higher-half address
        # like 0xffff800000000000 -- which fits perfectly in 64 bits
        # unsigned but has no signed 64-bit representation at all.
        src = (
            "def main():\n"
            "    print(hex(True))\n"
            "    x = 2 ** 64 - 0x800000000000\n"
            "    print(hex(x))\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
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
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
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
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
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
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("!dlroW ,olleH\nHlo o\nlo ol\nHlo ol!", output)

    def test_float_arithmetic_works_with_sse_enabled(self):
        # Exercises arch/x86_64/entry.cpp's CR0/CR4 SSE-enable sequence:
        # without it, any `double` arithmetic (compiled with SSE2
        # instructions, since we deliberately don't pass -mno-sse like a
        # typical bare-metal kernel would) would fault immediately.
        src = "def main():\n    print(3.5 + 1.5)\nmain()\n"
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("5", output)

    def test_for_range_with_explicit_step(self):
        src = "def main():\n    for i in range(0, 10, 2):\n        print(i)\nmain()\n"
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
        for expected in ("0", "2", "4", "6", "8"):
            self.assertIn(expected, output)

    def test_recursion_with_decrement(self):
        src = (
            "def fact(n: int) -> int:\n"
            "    if n <= 1:\n"
            "        return 1\n"
            "    return n * fact(n - 1)\n"
            "def main():\n"
            "    print(fact(6))\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("720", output)

    def test_subtraction_producing_a_negative_result(self):
        # _pyck_bignum_add (which _pyck_bignum_sub delegates to after
        # negating its second operand) called _long_normalize() a second,
        # redundant time on an already-normalized, already-signed result --
        # and since that result's ob_size could be negative at that point,
        # this silently flipped it back to positive (the same
        # "_long_normalize() only trims a non-negative size" bug fixed
        # elsewhere for _pyck_bignum_neg/_long_mul/_pyck_long_from_long).
        # `3 - 5` printed `2` instead of `-2`; found while debugging what
        # looked like a pointer-arithmetic crash in the memmap feature,
        # which turned out to be this. See runtime/freestanding/py_runtime.cpp.
        src = "def main():\n    print(3 - 5)\n    print(0 - 5)\nmain()\n"
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("-2", output)
        self.assertIn("-5", output)

    def test_repeated_floor_division_gives_correct_results(self):
        # _long_divmod's "repeated subtraction" quotient loop set
        # q->ob_size to the *subtraction count* (e.g. 25 for 255 // 10)
        # instead of the actual digit count the quotient occupies (1,
        # since 25 fits in a single base-2^30 digit), so every later
        # reader of that quotient read past the end of its real digit
        # array. 255 // 10 // 10 used to hang/exhaust the heap on the
        # second division instead of reaching 2. See
        # runtime/freestanding/py_runtime.cpp.
        src = (
            "def main():\n"
            "    n = 255\n"
            "    n = n // 10\n"
            "    n = n // 10\n"
            "    print(n)\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("2", output)

    def test_division_with_a_large_quotient_does_not_exhaust_the_heap(self):
        # Dividing via repeated single-unit subtraction needed one
        # allocating loop iteration *per unit of the quotient's value* --
        # 142857 of them for 1000000 // 7 -- exhausting even a generously
        # sized heap on an entirely ordinary division. Replaced with
        # binary long division (doubling the divisor as far as it still
        # fits, then subtracting), which takes O(log(quotient))
        # iterations instead.
        src = (
            "def main():\n"
            "    print(1000000 // 7)\n"
            "    print(1000000 % 7)\n"
            "    print(999999999 // 1000)\n"
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("142857\n1\n999999", output)


@unittest.skipUnless(FREESTANDING_X86_64_BOOT_TOOLING_AVAILABLE,
                      "cc/xorriso/qemu-system-x86_64 not available")
class TestClasses(_TempDirMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            ensure_limine_binaries()
        except RuntimeError as exc:
            raise unittest.SkipTest(f"could not fetch/build Limine: {exc}")

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
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
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
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
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
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
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
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
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
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
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
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64")
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("True\nFalse\nTrue\nTrue\nFalse", output)

    def test_memory_map_wrapped_in_pyck_classes(self):
        # There is no compiler-level memmap type -- MemoryMapEntry/
        # MemoryMap are ordinary PyCk classes built entirely out of
        # _LIMINE() and peek64() (see PythonOS/kernel/'s real
        # versions of these). Exact byte counts are QEMU-layout-
        # dependent, so this only checks structural facts.
        src = (
            "def unsigned64(x: int):\n"
            "    if x < 0:\n"
            "        return x + 2 ** 64\n"
            "    return x\n"
            "def limine_response(magic_a: int, magic_b: int):\n"
            "    table = _LIMINE()\n"
            "    result = 0\n"
            "    i = 0\n"
            "    entry_addr = peek64(table)\n"
            "    while entry_addr != 0:\n"
            "        a = unsigned64(peek64(entry_addr + 16))\n"
            "        b = unsigned64(peek64(entry_addr + 24))\n"
            "        if a == magic_a and b == magic_b:\n"
            "            result = peek64(entry_addr + 40)\n"
            "        i = i + 1\n"
            "        entry_addr = peek64(table + i * 8)\n"
            "    return result\n"
            "class MemoryMapEntry:\n"
            "    def __init__(self, addr: int):\n"
            "        self.base = peek64(addr)\n"
            "        self.length = peek64(addr + 8)\n"
            "        self.type = peek64(addr + 16)\n"
            "    def is_usable(self):\n"
            "        return self.type == 0\n"
            "class MemoryMap:\n"
            "    def __init__(self):\n"
            "        response = limine_response(0x67cf3d9d378a806f, 0xe304acdfc50c3c62)\n"
            "        self.count = peek64(response + 8)\n"
            "        self.entries_ptr = peek64(response + 16)\n"
            "    def entry(self, index: int):\n"
            "        entry_addr = peek64(self.entries_ptr + index * 8)\n"
            "        return MemoryMapEntry(entry_addr)\n"
            "    def total_usable(self):\n"
            "        total = 0\n"
            "        i = 0\n"
            "        while i < self.count:\n"
            "            e = self.entry(i)\n"
            "            if e.is_usable():\n"
            "                total = total + e.length\n"
            "            i = i + 1\n"
            "        return total\n"
            "def main():\n"
            "    mm = MemoryMap()\n"
            "    print(mm.count > 0)\n"
            "    print(mm.total_usable() > 0)\n"
            "    print(999)\n"  # sentinel: reached the end without crashing
            "main()\n"
        )
        elf = compile_freestanding_elf(self.make_workdir(), src, arch="x86_64",
                                       compile_args=["--enable-dangerous-memory-ops"])
        output = boot_and_capture_serial_limine(elf, timeout=8)
        self.assertIn("True\nTrue\n999", output)


if __name__ == "__main__":
    unittest.main()
