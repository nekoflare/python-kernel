#!/usr/bin/env python3
"""PythonOS build script: compile a Python source file (or the kernel/
directory) into a bootable 64-bit kernel, package it as a Limine bootable
ISO, and launch it in QEMU with a graphical display.

Usage:
    python3 build.py                       # build kernel/, boot graphically
    python3 build.py --source other.py     # build a different single file
    python3 build.py --headless            # no display window (serial only)
    python3 build.py --screenshot out.ppm  # boot headless, save a screenshot

PythonOS itself is just this script and the kernel/ directory: it
compiles kernel/'s sources with the *same* pyck compiler and runtime used
by the rest of this project (no vendored copy -- one compiler, one source
of truth), targeting --arch=x86_64_graphical, a Limine boot profile that
lives alongside the other arch/ targets and adds a graphical framebuffer
console (see arch/x86_64_graphical/graphics.cpp) instead of serial-only
output.

kernel/'s files are plain PyCk source using real `from X import name`
statements to declare what each one depends on (see pyck/imports.py) --
pyck resolves that whole graph itself starting from kernel/main.py (the
only file that actually calls main()), so this script just always hands
pyck that one entry point; there's no manual file-order list to
maintain here anymore.

The external dependencies fetched at build time are the Limine
bootloader itself (binaries + deploy tool), from its pinned v12.7.0
release, and Alpine Linux's busybox-static package (a single fully
static multi-call ELF binary providing sh, ls, cat, and dozens of
other applets -- this kernel has no dynamic linker, so only the
*-static package will actually run) -- all cached under build/ after
the first run.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PYTHONOS_DIR = Path(__file__).resolve().parent
PYCK_ROOT = PYTHONOS_DIR.parent  # the main PyCk project, one level up
BUILD_DIR = PYTHONOS_DIR / "build"
KERNEL_DIR = PYTHONOS_DIR / "kernel"
APPS_DIR = PYCK_ROOT / "apps"

LIMINE_VERSION = "v12.7.0"
LIMINE_URL = (
    f"https://github.com/limine-bootloader/limine/releases/"
    f"download/{LIMINE_VERSION}/limine-binary.tar.gz"
)

# Alpine release branch to pull busybox-static from. Unlike GNU's ftp
# mirrors (which keep every released tarball forever), Alpine's repo
# only ever keeps the *current* package build for a branch -- an older
# revision gets deleted the moment a new one is pushed -- so the exact
# busybox-static-<version>-r<rev>.apk filename isn't itself pinnable
# the way LIMINE_VERSION above is. ensure_alpine_busybox() resolves the
# current filename from this branch's index at build time instead.
ALPINE_BRANCH = "v3.20"
ALPINE_REPO_URL = f"https://dl-cdn.alpinelinux.org/alpine/{ALPINE_BRANCH}/main/x86_64/"

# Multi-call busybox has no separate binary per command -- normally a
# real Alpine system gets there with a symlink farm (ls -> busybox,
# cat -> busybox, ...) that busybox's own argv[0]-basename dispatch
# reads at startup. This kernel's cpio loader (see kernel/cpio.py) has
# no symlink support, and busybox-static isn't built with standalone-
# shell dispatch either (verified: ash can't invoke applets it can't
# execve as a real file when $PATH is unset) -- so each name below
# gets its own literal copy of the same ~640KB static binary in the
# initramfs instead. "sh" is the one that matters most: it's what
# kernel/main.py execs as the initial process (see setup_user_stack()
# in kernel/process.py), replacing GNU bash as the real console.
BUSYBOX_APPLETS = [
    "sh", "ls", "cat", "echo", "pwd", "mkdir", "rmdir", "rm", "cp", "mv",
    "touch", "grep", "find", "ps", "kill", "sleep", "clear", "uname",
    "whoami", "id", "head", "tail", "wc", "sort", "uniq", "du", "df",
    "hostname", "date", "vi",
]


def log(msg: str) -> None:
    print(f"[PythonOS] {msg}")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result


def ensure_limine() -> Path:
    """Download+build the pinned Limine release if not already cached.
    Returns the limine-binary directory."""
    limine_dir = BUILD_DIR / "limine-binary"
    tool = limine_dir / "limine"
    if tool.exists():
        log(f"Using cached Limine {LIMINE_VERSION} at {limine_dir}")
        return limine_dir

    log(f"Fetching Limine {LIMINE_VERSION}...")
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    tarball = BUILD_DIR / "limine-binary.tar.gz"
    run(["curl", "-sL", "--fail", LIMINE_URL, "-o", str(tarball)])
    run(["tar", "-xzf", str(tarball), "-C", str(BUILD_DIR)])

    log("Building the limine deploy tool...")
    run(["cc", "-std=c99", "-D_FILE_OFFSET_BITS=64",
         str(limine_dir / "limine.c"), "-o", str(tool)])
    return limine_dir


def ensure_alpine_busybox() -> Path:
    """Download Alpine's busybox-static package (cached under build/
    after the first run) and return the path to its extracted
    bin/busybox.static -- a single fully static ELF binary (verified:
    `file` reports "statically linked", `ldd` refuses it as "not a
    dynamic executable") providing sh, ls, cat, and dozens of other
    applets, replacing the previous from-source GNU bash + coreutils
    build. Static specifically because this kernel has no dynamic
    linker (see kernel/elf.py): Alpine's plain "busybox" package is
    dynamically linked against musl and won't run here at all, only
    busybox-static will.

    An Alpine .apk is just a gzip'd tar (three concatenated gzip
    members -- signature, control, data -- which GNU tar happily reads
    straight through in one pass), so no `apk` tool is needed, just
    curl + tar."""
    busybox_bin = BUILD_DIR / "busybox.static"
    if busybox_bin.exists():
        log(f"Using cached Alpine busybox-static at {busybox_bin}")
        return busybox_bin

    log(f"Resolving current busybox-static package on Alpine {ALPINE_BRANCH}...")
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    index = run(["curl", "-sL", "--fail", ALPINE_REPO_URL]).stdout
    matches = re.findall(r'busybox-static-[0-9][^"]*?\.apk', index)
    if not matches:
        raise SystemExit(
            f"couldn't find a busybox-static-*.apk link at {ALPINE_REPO_URL}"
        )
    apk_name = matches[0]
    apk_url = ALPINE_REPO_URL + apk_name

    log(f"Fetching {apk_name}...")
    apk_path = BUILD_DIR / apk_name
    run(["curl", "-sL", "--fail", apk_url, "-o", str(apk_path)])

    log(f"Extracting bin/busybox.static from {apk_name}...")
    run(["tar", "-xzf", str(apk_path), "-C", str(BUILD_DIR), "bin/busybox.static"])
    (BUILD_DIR / "bin" / "busybox.static").rename(busybox_bin)
    return busybox_bin


def resolve_source(source: Path) -> Path:
    """If `source` is a directory (the kernel/ split), that means its
    entry point (main.py); pyck resolves that file's own `from X import
    ...` graph itself. If `source` is already a single file, return it
    unchanged."""
    if source.is_dir():
        return source / "main.py"
    return source


def show_generated_cpp(source: Path) -> None:
    """Transpile `source` to C++ (via pyck's own --transpile-only) and
    print it -- this is the actual code pyck compiles into, i.e. what g++
    turns into the kernel ELF a moment later, not the Python source."""
    log(f"Generated C++ (from {source.name}):")
    tmp_cpp = BUILD_DIR / "_preview.cpp"
    cmd = [sys.executable, "-m", "pyck", str(source),
           "--target=freestanding", "--enable-dangerous-io-ops",
           "--enable-dangerous-memory-ops",
           "--transpile-only", "-o", str(tmp_cpp)]
    result = subprocess.run(cmd, cwd=str(PYCK_ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit("transpilation failed")

    rule = "-" * 70
    print(rule)
    lines = tmp_cpp.read_text().splitlines()
    width = len(str(len(lines)))
    for i, line in enumerate(lines, start=1):
        print(f"{i:>{width}} | {line}")
    print(rule)
    tmp_cpp.unlink()


def compile_kernel(source: Path, elf_out: Path) -> None:
    source = resolve_source(source)
    show_generated_cpp(source)
    log(f"Compiling {source.name} -> {elf_out.name}")
    # Running with cwd=PYCK_ROOT (the main project, one level up) puts the
    # *single* pyck compiler at the front of sys.path -- there is no
    # separate copy bundled in PythonOS/. pyck's own find_pyck_root()
    # resolves relative to that one copy, so this picks up
    # PyCk/arch/x86_64_graphical and PyCk/runtime/freestanding.
    cmd = [sys.executable, "-m", "pyck", str(source),
           "--target=freestanding", "--arch=x86_64_graphical",
           "--enable-dangerous-io-ops", "--enable-dangerous-memory-ops",
           "-o", str(elf_out)]
    result = subprocess.run(cmd, cwd=str(PYCK_ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit("kernel compilation failed")
    print(result.stdout)


def build_hello_app(out_dir: Path) -> Path:
    """Compile apps/hello/hello.c against a *static* glibc into a fresh
    ELF binary -- this is the app packaged as /bin/init in the
    initramfs below. Static, not dynamic: this kernel has no
    filesystem to load a shared libc.so or dynamic linker from, only
    the one initramfs file it already parses whole. Real glibc means
    real CRT startup (TLS setup, atexit, stdio buffering, ...) making
    real syscalls this kernel doesn't support yet -- see
    PythonOS/kernel/syscall.py's on_syscall(), which prints "Unhandled
    syscall: N" for exactly this, one at a time, as they're added.
    apps/hello/hello.asm (the original raw-syscall version) is left
    alone as a still-working reference/fallback."""
    log("Building apps/hello (glibc, static)...")
    c_src = APPS_DIR / "hello" / "hello.c"
    elf_path = out_dir / "hello"
    run(["gcc", "-static", "-O0", "-o", str(elf_path), str(c_src)])
    return elf_path


def build_child_app(out_dir: Path) -> Path:
    """Compile apps/child/child.c the same way as build_hello_app() --
    a second, independent static binary packaged as /bin/child (see
    build_initramfs() below), for the shell (busybox's ash, packaged
    as /bin/sh) to fork()+execve() as proof those syscalls actually
    launch a separate program, not just built-in commands handled
    inline."""
    log("Building apps/child (glibc, static)...")
    c_src = APPS_DIR / "child" / "child.c"
    elf_path = out_dir / "child"
    run(["gcc", "-static", "-O0", "-o", str(elf_path), str(c_src)])
    return elf_path


def build_initramfs(busybox_bin: Path, child_elf: Path, out_path: Path) -> None:
    """Package one copy of busybox_bin per name in BUSYBOX_APPLETS
    (there's no symlink support in kernel/cpio.py's parser to do this
    the normal way -- see BUSYBOX_APPLETS' own comment), plus
    child_elf as /bin/child, all exec bit set, inside a newc-format
    cpio archive -- the kernel loads this as a Limine module and
    parses it itself (see PythonOS/kernel/cpio.py). "bin/sh" is the
    one kernel/main.py actually execs at boot."""
    log("Building initramfs.cpio...")
    import shutil
    staging = BUILD_DIR / "initramfs_root"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "bin").mkdir(parents=True)
    for name in BUSYBOX_APPLETS:
        applet_path = staging / "bin" / name
        shutil.copy(busybox_bin, applet_path)
        applet_path.chmod(0o755)
    child_path = staging / "bin" / "child"
    shutil.copy(child_elf, child_path)
    child_path.chmod(0o755)

    # cpio -o reads a newline-separated file list on stdin and writes the
    # archive to stdout; `find .` (run with cwd=staging, so paths come
    # out as "./bin/sh" etc.) supplies that list, including the
    # directory entries themselves ("." and "./bin"), same as a typical
    # initramfs. The archive is binary, so this bypasses build.py's
    # shared run() helper (which decodes subprocess output as text).
    find_result = subprocess.run(["find", "."], cwd=str(staging),
                                  capture_output=True, text=True, check=True)
    with open(out_path, "wb") as f:
        subprocess.run(["cpio", "-o", "-H", "newc"], cwd=str(staging),
                        input=find_result.stdout.encode(), stdout=f,
                        stderr=subprocess.PIPE, check=True)


def build_iso(elf_path: Path, iso_path: Path, limine_dir: Path,
              initramfs_path: Path | None = None) -> None:
    log("Assembling bootable ISO...")
    iso_root = BUILD_DIR / "iso_root"
    if iso_root.exists():
        import shutil
        shutil.rmtree(iso_root)
    (iso_root / "boot" / "limine").mkdir(parents=True)
    (iso_root / "EFI" / "BOOT").mkdir(parents=True)

    import shutil
    shutil.copy(elf_path, iso_root / "boot" / "kernel")
    conf = "timeout: 0\n\n/PythonOS\n    protocol: limine\n    path: boot():/boot/kernel\n"
    if initramfs_path is not None:
        shutil.copy(initramfs_path, iso_root / "boot" / "initramfs.cpio")
        conf += "    module_path: boot():/boot/initramfs.cpio\n"
    (iso_root / "boot" / "limine" / "limine.conf").write_text(conf)
    for name in ("limine-bios.sys", "limine-bios-cd.bin", "limine-uefi-cd.bin"):
        shutil.copy(limine_dir / name, iso_root / "boot" / "limine" / name)
    shutil.copy(limine_dir / "BOOTX64.EFI", iso_root / "EFI" / "BOOT" / "BOOTX64.EFI")

    run(["xorriso", "-as", "mkisofs", "-R", "-r", "-J",
         "-b", "boot/limine/limine-bios-cd.bin",
         "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table", "-hfsplus",
         "-apm-block-size", "2048", "--efi-boot", "boot/limine/limine-uefi-cd.bin",
         "-efi-boot-part", "--efi-boot-image", "--protective-msdos-label",
         str(iso_root), "-o", str(iso_path)])
    run([str(limine_dir / "limine"), "bios-install", str(iso_path)])


# QEMU's default CPU model (no -cpu flag) reports AVX in CPUID but not
# XSAVE -- an inconsistent combination no real CPU has, since AVX
# requires XSAVE for the OS to manage its state. entry.cpp's
# enable_avx() correctly checks CPUID before touching CR4.OSXSAVE (an
# unconditional write there is #GP on a CPU that doesn't support XSAVE,
# which freezes the VCPU this early in boot with no diagnostic at all),
# so on the default model it correctly leaves AVX disabled -- but glibc
# still sees AVX reported and executes a VEX-encoded instruction
# directly, which #UDs unconditionally without CR4.OSXSAVE regardless
# of what the hardware actually supports. "max" reports every feature
# the accelerator itself can actually back, so XSAVE and AVX are
# consistent and enable_avx() can safely turn both on.
QEMU_CPU = "max"

# Where every QEMU invocation below dumps xHCI/USB-HID activity -- every
# usb_xhci_* trace event (hw/usb/hcd-xhci*.c: port/slot/endpoint state,
# doorbells, transfer start/success/error) and every hid_*/usb_hid_*
# event (hw/usb/dev-hid.c, hw/input/hid.c: the usb-kbd device itself --
# reports queued, SET_PROTOCOL/SET_IDLE, reset). "enable=" takes one
# glob per -trace flag; the output file is set via its own plain
# "-trace file=..." (combining file= into the same flag as an enable=
# pattern only sticks if it's the LAST -trace option given, so it's
# kept separate here rather than relying on argument order).
QEMU_TRACE_LOG = BUILD_DIR / "qemu-trace.log"
QEMU_TRACE_ARGS = [
    "-trace", "enable=usb_*",
    "-trace", "enable=hid_*",
    "-trace", f"file={QEMU_TRACE_LOG}",
]


def run_qemu_graphical(iso_path: Path) -> None:
    log(f"Launching QEMU (graphical window, debugcon on stdio, xhci/hid trace -> {QEMU_TRACE_LOG})...")
    subprocess.run([
        "qemu-system-x86_64", "-M", "q35", "-m", "8G", "-cpu", QEMU_CPU,
        "-enable-kvm",
        "-cdrom", str(iso_path), "-boot", "d",
        "-debugcon", "stdio",
        "-device", "qemu-xhci", "-device", "usb-kbd",
        *QEMU_TRACE_ARGS,
    ])


def run_qemu_headless(iso_path: Path) -> None:
    log(f"Launching QEMU (headless, debugcon on stdio, xhci/hid trace -> {QEMU_TRACE_LOG})...")
    # Not also "-serial stdio": debugcon (see arch/x86_64_graphical/
    # graphics.cpp's _serial_write()) mirrors serial byte-for-byte, and
    # QEMU refuses to attach two devices to the same literal stdio
    # anyway ("cannot use stdio by multiple character devices").
    subprocess.run([
        "qemu-system-x86_64", "-M", "q35", "-m", "8G", "-cpu", QEMU_CPU,
        "-enable-kvm",
        "-cdrom", str(iso_path), "-boot", "d",
        "-display", "none", "-debugcon", "stdio",
        "-device", "qemu-xhci", "-device", "usb-kbd",
        *QEMU_TRACE_ARGS,
    ])


def take_screenshot(iso_path: Path, out_path: Path, boot_delay: float = 3.0) -> None:
    log(f"Booting headlessly to capture a screenshot ({boot_delay}s boot delay, xhci/hid trace -> {QEMU_TRACE_LOG})...")
    proc = subprocess.Popen(
        ["qemu-system-x86_64", "-M", "q35", "-m", "8G", "-cpu", QEMU_CPU,
         "-enable-kvm",
         "-cdrom", str(iso_path), "-boot", "d",
         "-display", "none", "-monitor", "stdio",
         "-device", "qemu-xhci", "-device", "usb-kbd",
         *QEMU_TRACE_ARGS],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        time.sleep(boot_delay)
        proc.stdin.write(f"screendump {out_path}\n")
        proc.stdin.flush()
        time.sleep(1.0)
        proc.stdin.write("quit\n")
        proc.stdin.flush()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    log(f"Screenshot saved to {out_path}")


def capture_log(iso_path: Path, out_path: Path, boot_delay: float = 5.0) -> None:
    """Boot headlessly for `boot_delay` seconds and save whatever the
    kernel wrote to the debugcon (port 0xE9, see arch/x86_64_graphical/
    graphics.cpp's _serial_write()) log -- the text equivalent of
    take_screenshot(), for when a plain log of everything printed is
    more useful than a picture of the framebuffer."""
    log(f"Booting headlessly to capture a debugcon log ({boot_delay}s boot delay, xhci/hid trace -> {QEMU_TRACE_LOG})...")
    proc = subprocess.Popen([
        "qemu-system-x86_64", "-M", "q35", "-m", "8G", "-cpu", QEMU_CPU,
        "-enable-kvm",
        "-cdrom", str(iso_path), "-boot", "d",
        "-display", "none", "-no-reboot",
        "-debugcon", f"file:{out_path}",
        "-device", "qemu-xhci", "-device", "usb-kbd",
        *QEMU_TRACE_ARGS,
    ])
    try:
        time.sleep(boot_delay)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    log(f"Debugcon log saved to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(KERNEL_DIR),
                         help="Python source file, or a directory whose main.py "
                              "is the entry point (default: kernel/)")
    parser.add_argument("--headless", action="store_true",
                         help="run without a graphical window (serial on stdio)")
    parser.add_argument("--screenshot", metavar="FILE",
                         help="boot headlessly and save a screenshot (PPM) to FILE, then exit")
    parser.add_argument("--capture-log", metavar="FILE",
                         help="boot headlessly and save the debugcon (port 0xE9) log to FILE, then exit")
    parser.add_argument("--boot-delay", type=float, default=5.0,
                         help="seconds to wait before terminating QEMU (for --capture-log)")
    args = parser.parse_args()

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    source = Path(args.source).resolve()
    elf_path = BUILD_DIR / "kernel.elf"
    iso_path = BUILD_DIR / "PythonOS.iso"

    limine_dir = ensure_limine()
    compile_kernel(source, elf_path)
    busybox_bin = ensure_alpine_busybox()
    child_elf = build_child_app(BUILD_DIR)
    initramfs_path = BUILD_DIR / "initramfs.cpio"
    build_initramfs(busybox_bin, child_elf, initramfs_path)
    build_iso(elf_path, iso_path, limine_dir, initramfs_path)

    if args.screenshot:
        take_screenshot(iso_path, Path(args.screenshot).resolve(), args.boot_delay)
    elif args.capture_log:
        capture_log(iso_path, Path(args.capture_log).resolve(), args.boot_delay)
    elif args.headless:
        run_qemu_headless(iso_path)
    else:
        run_qemu_graphical(iso_path)


if __name__ == "__main__":
    main()
