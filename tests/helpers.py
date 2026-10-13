"""Shared test utilities for the PyCk test suite.

Provides two speeds of testing the compiler pipeline:

- In-process helpers (``build_ir``, ``generate_cpp``, ``typecheck``) drive
  TypeChecker / IRBuilder / CppCodegen directly, mirroring the steps in
  ``pyck/__main__.py``. These are fast and used by unit tests.
- Subprocess helpers (``run_cli``, ``compile_and_run_hosted``,
  ``compile_freestanding_elf``, ``boot_and_capture_serial``) shell out to
  ``python -m pyck`` and a real C++ toolchain (and, for freestanding boot
  tests, grub-mkrescue + qemu-system-i386). These exercise the real,
  end-to-end behavior of compiled programs and are used by integration
  tests. They are skipped automatically when the required tools aren't
  on PATH.
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pyck.typechecker import TypeChecker, CompileError, FuncSig  # noqa: E402
from pyck.ast_to_ir import IRBuilder, IRBuildError  # noqa: E402
from pyck.codegen import CppCodegen, CodegenError  # noqa: E402
from pyck import ir as IR  # noqa: E402


# ---------------------------------------------------------------------------
# In-process pipeline helpers
# ---------------------------------------------------------------------------

def typecheck(source: str, filename: str = "<test>", dangerous_ops: bool = False,
              dangerous_io_ops: bool = False) -> TypeChecker:
    """Run pass1+pass2 and return the TypeChecker (errors collected, not raised)."""
    tree = ast.parse(source, filename=filename)
    checker = TypeChecker(source, filename=filename, dangerous_ops=dangerous_ops,
                          dangerous_io_ops=dangerous_io_ops)
    checker._pass1_collect(tree)
    checker._pass2_infer_and_validate()
    return checker


def typecheck_errors(source: str, filename: str = "<test>", dangerous_ops: bool = False,
                      dangerous_io_ops: bool = False) -> list[str]:
    """Return the list of type-check error strings (empty if the source is clean)."""
    checker = typecheck(source, filename=filename, dangerous_ops=dangerous_ops,
                        dangerous_io_ops=dangerous_io_ops)
    return [str(e) for e in checker._errors]


def build_ir(source: str, filename: str = "<test>", dangerous_ops: bool = False,
             dangerous_io_ops: bool = False) -> list[IR.IRNode]:
    """Type-check and build IR for `source`, mirroring pyck/__main__.py Steps 1-2.

    Raises CompileError on type errors, IRBuildError on unsupported constructs.
    """
    tree = ast.parse(source, filename=filename)
    checker = TypeChecker(source, filename=filename, dangerous_ops=dangerous_ops,
                          dangerous_io_ops=dangerous_io_ops)
    checker._pass1_collect(tree)
    checker._pass2_infer_and_validate()
    if checker._errors:
        raise checker._errors[0]
    builder = IRBuilder(
        source, filename, checker._funcs, checker._global_vars,
        dangerous_ops=dangerous_ops, classes=checker._classes,
    )
    return builder.build(tree)


def generate_cpp(source: str, mode: str = "hosted", dangerous_ops: bool = False,
                  dangerous_io_ops: bool = False,
                  max_recursion_depth: int = 10000, filename: str = "<test>") -> str:
    """Full in-process pipeline: Python source -> generated C++ source string."""
    ir_stmts = build_ir(source, filename=filename, dangerous_ops=dangerous_ops,
                         dangerous_io_ops=dangerous_io_ops)
    codegen = CppCodegen(mode=mode, max_recursion_depth=max_recursion_depth,
                         dangerous_ops=dangerous_ops)
    return codegen.generate(ir_stmts)


# ---------------------------------------------------------------------------
# Toolchain availability
# ---------------------------------------------------------------------------

def _probe_m32() -> bool:
    if shutil.which("g++") is None:
        return False
    try:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "t.cpp"
            src.write_text("int main(){return 0;}\n")
            out = Path(d) / "t"
            result = subprocess.run(
                ["g++", "-m32", "-o", str(out), str(src)],
                capture_output=True, timeout=30,
            )
            return result.returncode == 0
    except Exception:
        return False


def _probe_python_headers() -> bool:
    try:
        import sysconfig
        include = sysconfig.get_path("include")
        return include is not None and (Path(include) / "Python.h").exists()
    except Exception:
        return False


def _probe_m64_kernel() -> bool:
    if shutil.which("g++") is None:
        return False
    try:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "t.cpp"
            src.write_text("int main(){return 0;}\n")
            out = Path(d) / "t"
            result = subprocess.run(
                ["g++", "-m64", "-mno-red-zone", "-mcmodel=kernel",
                 "-fno-pic", "-c", "-o", str(out.with_suffix(".o")), str(src)],
                capture_output=True, timeout=30,
            )
            return result.returncode == 0
    except Exception:
        return False


HAVE_GXX = shutil.which("g++") is not None
HAVE_PY_HEADERS = _probe_python_headers()
HAVE_M32 = _probe_m32()
HAVE_M64_KERNEL = _probe_m64_kernel()
HAVE_QEMU_I386 = shutil.which("qemu-system-i386") is not None
HAVE_GRUB_MKRESCUE = shutil.which("grub-mkrescue") is not None
HAVE_HOST_CC = shutil.which("cc") is not None
HAVE_XORRISO = shutil.which("xorriso") is not None
HAVE_QEMU_X86_64 = shutil.which("qemu-system-x86_64") is not None

HOSTED_TOOLCHAIN_AVAILABLE = HAVE_GXX and HAVE_PY_HEADERS
FREESTANDING_TOOLCHAIN_AVAILABLE = HAVE_GXX and HAVE_M32
FREESTANDING_BOOT_AVAILABLE = (
    FREESTANDING_TOOLCHAIN_AVAILABLE and HAVE_QEMU_I386 and HAVE_GRUB_MKRESCUE
)

FREESTANDING_X86_64_TOOLCHAIN_AVAILABLE = HAVE_GXX and HAVE_M64_KERNEL
# Network access (to fetch Limine's release binaries) is checked lazily by
# ensure_limine_binaries(), not here -- see ensure_limine_binaries's callers.
FREESTANDING_X86_64_BOOT_TOOLING_AVAILABLE = (
    FREESTANDING_X86_64_TOOLCHAIN_AVAILABLE
    and HAVE_HOST_CC and HAVE_XORRISO and HAVE_QEMU_X86_64
)


# ---------------------------------------------------------------------------
# Subprocess-based CLI helpers
# ---------------------------------------------------------------------------

def run_cli(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Invoke `python -m pyck <args>` as a subprocess from the repo root."""
    cmd = [sys.executable, "-m", "pyck", *args]
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)


def compile_and_run_hosted(source: str, compile_args: list[str] | None = None,
                            timeout: int = 30) -> subprocess.CompletedProcess:
    """Compile `source` in hosted mode and run the resulting binary.

    Returns the CompletedProcess of running the produced binary. Raises
    AssertionError if the pyck compile step itself fails (with compiler
    output attached), so a test failure here is unambiguous about which
    stage broke.
    """
    with tempfile.TemporaryDirectory() as d:
        src_path = Path(d) / "prog.py"
        src_path.write_text(source)
        out_path = Path(d) / "prog_bin"
        cmd = [str(src_path), "--target=hosted", "-o", str(out_path)]
        if compile_args:
            cmd.extend(compile_args)
        result = run_cli(cmd, timeout=timeout)
        if result.returncode != 0:
            raise AssertionError(
                f"pyck compile failed (exit {result.returncode}):\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )
        return subprocess.run([str(out_path)], capture_output=True, text=True, timeout=timeout)


def compile_freestanding_elf(workdir: Path, source: str, arch: str = "x86",
                              compile_args: list[str] | None = None,
                              timeout: int = 90) -> Path:
    """Compile `source` in freestanding mode to an ELF binary under `workdir`.

    The caller owns `workdir` and is responsible for cleaning it up.
    """
    src_path = workdir / "prog.py"
    src_path.write_text(source)
    out_path = workdir / "prog.elf"
    cmd = [str(src_path), "--target=freestanding", f"--arch={arch}", "-o", str(out_path)]
    if compile_args:
        cmd.extend(compile_args)
    result = run_cli(cmd, timeout=timeout)
    if result.returncode != 0:
        raise AssertionError(
            f"pyck freestanding compile failed (exit {result.returncode}):\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return out_path


def boot_and_capture_serial(elf_path: Path, timeout: int = 10) -> str:
    """Build a GRUB multiboot ISO from `elf_path` and boot it in QEMU.

    Returns the captured console text (serial COM1 is redirected to stdio
    by `-nographic`, alongside the SeaBIOS/GRUB banner). The QEMU kernel
    never exits on its own (it halts in a `hlt` loop), so this always
    waits out `timeout` seconds before killing the VM.
    """
    with tempfile.TemporaryDirectory() as d:
        iso_dir = Path(d) / "iso"
        (iso_dir / "boot" / "grub").mkdir(parents=True)
        shutil.copy(elf_path, iso_dir / "boot" / "kernel.elf")
        (iso_dir / "boot" / "grub" / "grub.cfg").write_text(
            'set timeout=0\nset default=0\n'
            'menuentry "T" { multiboot /boot/kernel.elf; boot }\n'
        )
        iso_path = Path(d) / "kernel.iso"
        build = subprocess.run(
            ["grub-mkrescue", "-o", str(iso_path), str(iso_dir)],
            capture_output=True, text=True, timeout=60,
        )
        if build.returncode != 0:
            raise AssertionError(f"grub-mkrescue failed:\n{build.stdout}\n{build.stderr}")

        proc = subprocess.Popen(
            ["qemu-system-i386", "-cdrom", str(iso_path), "-m", "128M",
             "-nographic", "-boot", "d"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()
        return stdout


# ---------------------------------------------------------------------------
# Limine (x86_64) helpers
# ---------------------------------------------------------------------------

LIMINE_VERSION = "v12.7.0"
LIMINE_CACHE_DIR = Path(tempfile.gettempdir()) / f"pyck-limine-{LIMINE_VERSION}"


def ensure_limine_binaries(timeout: int = 60) -> Path:
    """Download (if not already cached) and build the pinned Limine release's
    bootloader binaries and deploy tool. Returns the extracted
    `limine-binary` directory (containing `limine-bios.sys`,
    `limine-bios-cd.bin`, `limine-uefi-cd.bin`, `BOOTX64.EFI`, and the
    built `limine` deploy-tool executable).

    Raises RuntimeError on any failure (no network, download error, build
    error). Callers running as tests should catch this and skip -- see
    boot_and_capture_serial_limine's callers for the pattern.
    """
    limine_dir = LIMINE_CACHE_DIR / "limine-binary"
    tool = limine_dir / "limine"
    if tool.exists():
        return limine_dir

    LIMINE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tarball = LIMINE_CACHE_DIR / "limine-binary.tar.gz"
    url = (
        f"https://github.com/limine-bootloader/limine/releases/"
        f"download/{LIMINE_VERSION}/limine-binary.tar.gz"
    )
    try:
        result = subprocess.run(
            ["curl", "-sL", "--fail", url, "-o", str(tarball)],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as exc:
        raise RuntimeError(f"could not fetch Limine binaries: {exc}") from exc
    if result.returncode != 0 or not tarball.exists() or tarball.stat().st_size == 0:
        raise RuntimeError(f"failed to download Limine binaries:\n{result.stderr}")

    extract = subprocess.run(
        ["tar", "-xzf", str(tarball), "-C", str(LIMINE_CACHE_DIR)],
        capture_output=True, text=True, timeout=timeout,
    )
    if extract.returncode != 0:
        raise RuntimeError(f"failed to extract Limine binaries:\n{extract.stderr}")

    build = subprocess.run(
        ["cc", "-std=c99", "-D_FILE_OFFSET_BITS=64",
         str(limine_dir / "limine.c"), "-o", str(tool)],
        capture_output=True, text=True, timeout=timeout,
    )
    if build.returncode != 0 or not tool.exists():
        raise RuntimeError(f"failed to build the limine deploy tool:\n{build.stderr}")

    return limine_dir


def boot_and_capture_serial_limine(elf_path: Path, timeout: int = 10) -> str:
    """Build a Limine-bootable ISO from `elf_path` and boot it in QEMU
    (qemu-system-x86_64), returning the captured console/serial text.

    Mirrors boot_and_capture_serial (the GRUB/Multiboot1 equivalent for
    the 32-bit x86 target), but uses the Limine boot protocol: a
    `limine.conf`, the Limine BIOS/UEFI stage-2 blobs, and the `limine`
    deploy tool's `bios-install` step instead of grub-mkrescue.
    """
    limine_dir = ensure_limine_binaries()

    with tempfile.TemporaryDirectory() as d:
        iso_dir = Path(d) / "iso"
        (iso_dir / "boot" / "limine").mkdir(parents=True)
        (iso_dir / "EFI" / "BOOT").mkdir(parents=True)
        shutil.copy(elf_path, iso_dir / "boot" / "kernel")
        (iso_dir / "boot" / "limine" / "limine.conf").write_text(
            "timeout: 0\n\n/T\n    protocol: limine\n    path: boot():/boot/kernel\n"
        )
        for name in ("limine-bios.sys", "limine-bios-cd.bin", "limine-uefi-cd.bin"):
            shutil.copy(limine_dir / name, iso_dir / "boot" / "limine" / name)
        shutil.copy(limine_dir / "BOOTX64.EFI", iso_dir / "EFI" / "BOOT" / "BOOTX64.EFI")

        iso_path = Path(d) / "kernel.iso"
        build = subprocess.run(
            ["xorriso", "-as", "mkisofs", "-R", "-r", "-J",
             "-b", "boot/limine/limine-bios-cd.bin",
             "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table", "-hfsplus",
             "-apm-block-size", "2048", "--efi-boot", "boot/limine/limine-uefi-cd.bin",
             "-efi-boot-part", "--efi-boot-image", "--protective-msdos-label",
             str(iso_dir), "-o", str(iso_path)],
            capture_output=True, text=True, timeout=60,
        )
        if build.returncode != 0:
            raise AssertionError(f"xorriso failed:\n{build.stdout}\n{build.stderr}")

        install = subprocess.run(
            [str(limine_dir / "limine"), "bios-install", str(iso_path)],
            capture_output=True, text=True, timeout=30,
        )
        if install.returncode != 0:
            raise AssertionError(f"limine bios-install failed:\n{install.stdout}\n{install.stderr}")

        proc = subprocess.Popen(
            ["qemu-system-x86_64", "-M", "q35", "-cdrom", str(iso_path), "-boot", "d",
             "-m", "256M", "-nographic", "-serial", "mon:stdio"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()
        return stdout
