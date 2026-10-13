#!/usr/bin/env python3
"""PyCk CLI driver.

Usage:
    python -m pyck source.py --target=hosted -o output
    python -m pyck source.py --target=freestanding -o output
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .typechecker import TypeChecker, CompileError
from .ast_to_ir import IRBuilder
from .codegen import CppCodegen
from .imports import resolve_program, merge_modules, ModuleResolutionError


def find_pyck_root() -> Path:
    """Find the PyCk project root (contains runtime/ and arch/)."""
    return Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(
        prog="pyck",
        description="PyCk: Python-to-C++ transpiler with freestanding/hosted targets",
    )
    parser.add_argument("source", help="Python source file to transpile")
    parser.add_argument(
        "--target",
        choices=["hosted", "freestanding"],
        required=True,
        help="Target mode: hosted (libc+libpython) or freestanding (no OS)",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output file path (default: <source>.cpp for transpile-only, or compiled binary)",
    )
    parser.add_argument(
        "--transpile-only",
        action="store_true",
        help="Only transpile to C++, do not compile",
    )
    parser.add_argument(
        "--enable-dangerous-memory-ops",
        action="store_true",
        help="Enable peek/poke memory access builtins",
    )
    parser.add_argument(
        "--enable-dangerous-io-ops",
        action="store_true",
        help="Enable port I/O, MSR, CPUID, and control-register builtins "
             "(freestanding targets only)",
    )
    parser.add_argument(
        "--max-recursion-depth",
        type=int,
        default=10000,
        help="Maximum recursion depth (freestanding only, default: 10000)",
    )
    parser.add_argument(
        "--arch",
        default="x86_64",
        help="Architecture for freestanding target (default: x86_64). "
             "x86_64 targets Limine; x86 targets 32-bit Multiboot1/GRUB.",
    )
    parser.add_argument(
        "--compiler",
        default=None,
        help="C++ compiler to use (default: auto-detect g++/clang++)",
    )
    parser.add_argument(
        "--python-include",
        default=None,
        help="Path to Python.h (hosted mode, auto-detected if not set)",
    )
    parser.add_argument(
        "--python-lib",
        default=None,
        help="Python library flags for hosted mode (auto-detected if not set)",
    )

    args = parser.parse_args()

    # Read source
    source_path = Path(args.source)
    if not source_path.exists():
        print(f"error: file not found: {args.source}", file=sys.stderr)
        sys.exit(1)

    # Resolve source_path's own `from X import ...` statements and every
    # file it transitively depends on into one merged AST, in dependency
    # order -- see pyck/imports.py. A file with no imports at all (every
    # existing single-file example/test) resolves trivially to just
    # itself, so this path is always taken, not just for multi-file
    # programs.
    try:
        modules = resolve_program(source_path)
        tree = merge_modules(modules)
    except ModuleResolutionError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    source = source_path.read_text()

    # Step 1: Type check
    try:
        checker = TypeChecker(
            source,
            filename=str(source_path),
            dangerous_ops=args.enable_dangerous_memory_ops,
            dangerous_io_ops=args.enable_dangerous_io_ops,
        )
        # Pass 1 + 2 (type inference)
        checker._pass1_collect(tree)
        checker._compute_reachable(tree)
        checker._pass2_infer_and_validate()
        if checker._errors:
            for e in checker._errors:
                print(str(e), file=sys.stderr)
            sys.exit(1)
    except CompileError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    # Step 2: Build IR
    try:
        builder = IRBuilder(
            source, str(source_path),
            checker._funcs, checker._global_vars,
            dangerous_ops=args.enable_dangerous_memory_ops,
            classes=checker._classes,
            reachable_funcs=checker._reachable_funcs,
            reachable_classes=checker._reachable_classes,
        )
        ir_stmts = builder.build(tree)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    # Step 3: Generate C++
    try:
        codegen = CppCodegen(
            mode=args.target,
            max_recursion_depth=args.max_recursion_depth,
            dangerous_ops=args.enable_dangerous_memory_ops,
        )
        cpp_source = codegen.generate(ir_stmts)
    except Exception as e:
        print(f"error: codegen failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Step 4: Write C++ output
    if args.transpile_only:
        out_path = args.output or str(source_path.with_suffix(".cpp"))
        Path(out_path).write_text(cpp_source)
        print(f"C++ source written to {out_path}")
        return

    # Step 5: Compile
    pyck_root = find_pyck_root()
    compiler = args.compiler or _find_compiler()

    with tempfile.NamedTemporaryFile(suffix=".cpp", mode="w", delete=False) as f:
        f.write(cpp_source)
        tmp_cpp = f.name

    try:
        out_path = args.output or str(source_path.with_suffix(""))
        cmd = _build_compile_command(
            compiler, tmp_cpp, out_path, args.target, args.arch,
            pyck_root, args.python_include, args.python_lib,
        )
        print(f"Compiling: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print("Compilation failed:", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            sys.exit(1)
        print(f"Binary written to {out_path}")
    finally:
        os.unlink(tmp_cpp)


def _find_compiler() -> str:
    """Auto-detect g++ or clang++."""
    for compiler in ["g++", "clang++"]:
        try:
            subprocess.run([compiler, "--version"], capture_output=True, check=True)
            return compiler
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    print("error: no C++ compiler found (install g++ or clang++)", file=sys.stderr)
    sys.exit(1)


def _build_compile_command(
    compiler: str, cpp_file: str, out_path: str,
    target: str, arch: str, pyck_root: Path,
    python_include: str | None, python_lib: str | None,
) -> list[str]:
    cmd = [compiler, "-std=c++17", "-o", out_path, cpp_file]

    if target == "hosted":
        # Hosted mode: link against libpython via Stable ABI
        cmd.extend(["-DPy_LIMITED_API=0x03070000"])
        if python_include:
            cmd.extend(["-I", python_include])
        else:
            # Auto-detect
            try:
                result = subprocess.run(
                    ["python3", "-c", "import sysconfig; print(sysconfig.get_path('include'))"],
                    capture_output=True, text=True, check=True,
                )
                cmd.extend(["-I", result.stdout.strip()])
            except Exception:
                pass
        if python_lib:
            cmd.append(python_lib)
        else:
            try:
                # Try python3-config first (more reliable)
                result = subprocess.run(
                    ["python3-config", "--libs", "--embed"],
                    capture_output=True, text=True, check=True,
                )
                cmd.extend(result.stdout.strip().split())
            except Exception:
                try:
                    # Fallback to sysconfig
                    result = subprocess.run(
                        ["python3", "-c",
                         "import sysconfig; "
                         "flags = sysconfig.get_config_var('LDFLAGS') or ''; "
                         "libs = sysconfig.get_config_var('LIBS') or ''; "
                         "print(f'{flags} {libs}')"],
                        capture_output=True, text=True, check=True,
                    )
                    cmd.extend(result.stdout.strip().split())
                except Exception:
                    cmd.extend(["-lpython3"])
    else:
        # Freestanding mode
        cmd.extend(["-nostdlib", "-ffreestanding", "-fno-exceptions", "-fno-rtti",
                     "-fno-stack-protector",
                     "-static", "-nodefaultlibs", "-nostartfiles", "-no-pie"])
        cmd.extend(["-I", str(pyck_root / "runtime" / "freestanding")])

        # Architecture-specific flags and files
        arch_dir = pyck_root / "arch" / arch
        if arch == "x86":
            cmd.extend(["-m32", "-fno-pic", "-fno-PIE"])
            cmd.extend(["-I", str(arch_dir)])
            # Include arch-specific assembly and C++ files (not linker scripts)
            for f in ["start.S", "py_print_vga.cpp", "support.cpp"]:
                fp = arch_dir / f
                if fp.exists():
                    cmd.append(str(fp))
        elif arch == "x86_64":
            # Booted via Limine (see arch/x86_64/entry.cpp). SSE is left
            # enabled (unlike a typical bare-metal kernel) because PyCk's
            # float type needs real `double` arithmetic; entry.cpp enables
            # it at the CPU level before any generated code runs.
            cmd.extend(["-m64", "-march=x86-64", "-mabi=sysv",
                        "-mno-red-zone", "-mcmodel=kernel",
                        "-fno-pic", "-fno-PIE"])
            cmd.extend(["-I", str(arch_dir)])
            for f in ["entry.cpp", "py_print_serial.cpp", "support.cpp"]:
                fp = arch_dir / f
                if fp.exists():
                    cmd.append(str(fp))
        elif arch == "x86_64_graphical":
            # Same Limine/x86_64 boot story as "x86_64", but with a
            # graphical framebuffer console (arch/x86_64_graphical/
            # graphics.cpp) instead of the serial-only print backend.
            cmd.extend(["-m64", "-march=x86-64", "-mabi=sysv",
                        "-mno-red-zone", "-mcmodel=kernel",
                        "-fno-pic", "-fno-PIE"])
            cmd.extend(["-I", str(arch_dir)])
            for f in ["entry.cpp", "graphics.cpp", "support.cpp"]:
                fp = arch_dir / f
                if fp.exists():
                    cmd.append(str(fp))
        elif arch == "riscv":
            cmd.extend(["-march=rv64gc", "-mabi=lp64"])
            cmd.extend(["-I", str(arch_dir)])

        # Wider port I/O, MSRs, CPUID, and control-register intrinsics are
        # shared across every x86 arch (see arch/x86_shared/hw_intrinsics.cpp
        # for why one file suffices for both -m32 and -m64 targets).
        if arch in ("x86", "x86_64", "x86_64_graphical"):
            shared_intrinsics = pyck_root / "arch" / "x86_shared" / "hw_intrinsics.cpp"
            if shared_intrinsics.exists():
                cmd.append(str(shared_intrinsics))

        # Limine request table (_LIMINE() builtin; PyCk source finds the
        # request it wants by scanning for a matching magic id and
        # decodes everything else via peek64 -- see
        # arch/x86_64_shared/limine_table.cpp and PythonOS/kernel/):
        # only the Limine-booted 64-bit targets have one to expose.
        if arch in ("x86_64", "x86_64_graphical"):
            shared_limine_table = pyck_root / "arch" / "x86_64_shared" / "limine_table.cpp"
            if shared_limine_table.exists():
                cmd.append(str(shared_limine_table))

        # Interrupt stub trampolines (_ISR_STUB() builtin; a top-level
        # `def on_interrupt(vector: int)` in PyCk source is the real
        # handler -- see arch/x86_64_shared/idt_stubs.cpp and
        # PythonOS/kernel/): same 64-bit-only availability as the Limine
        # table above.
        if arch in ("x86_64", "x86_64_graphical"):
            shared_idt_stubs = pyck_root / "arch" / "x86_64_shared" / "idt_stubs.cpp"
            if shared_idt_stubs.exists():
                cmd.append(str(shared_idt_stubs))

        # Kernel thread context switching (switch_context() and
        # _THREAD_TRAMPOLINE_ADDR() builtins; a top-level `def
        # thread_entry():` in PyCk source is the shared entry point every
        # thread's fake initial stack resumes into -- see
        # arch/x86_64_shared/thread_switch.cpp and PythonOS/kernel/
        # threads.py): same 64-bit-only availability as the ISR stubs
        # above, which this depends on (threads are switched from the
        # timer interrupt handler).
        if arch in ("x86_64", "x86_64_graphical"):
            shared_thread_switch = pyck_root / "arch" / "x86_64_shared" / "thread_switch.cpp"
            if shared_thread_switch.exists():
                cmd.append(str(shared_thread_switch))

        # Real x86-64 SYSCALL entry (switch_context()'s sibling: same
        # "one hand-written asm trampoline, PyCk source decides what it
        # does" shape -- see arch/x86_64_shared/syscall_entry.cpp and
        # PythonOS/kernel/syscall.py's top-level `def on_syscall(regs:
        # int):`).
        if arch in ("x86_64", "x86_64_graphical"):
            shared_syscall_entry = pyck_root / "arch" / "x86_64_shared" / "syscall_entry.cpp"
            if shared_syscall_entry.exists():
                cmd.append(str(shared_syscall_entry))

        # Include freestanding runtime
        runtime_dir = pyck_root / "runtime" / "freestanding"
        for f in ["py_runtime.cpp", "py_bignum.cpp"]:
            fp = runtime_dir / f
            if fp.exists():
                cmd.append(str(fp))

        # Linker script
        linker_script = arch_dir / "linker.ld"
        if linker_script.exists():
            cmd.extend(["-T", str(linker_script)])

    return cmd


if __name__ == "__main__":
    main()
