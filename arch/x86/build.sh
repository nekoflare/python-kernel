#!/bin/bash
# Build and run PyCk freestanding x86 kernel in QEMU.
#
# Usage:
#   ./arch/x86/build.sh <source.py> [--run]
#
# Examples:
#   ./arch/x86/build.sh examples/kernel_hello.py
#   ./arch/x86/build.sh examples/kernel_hello.py --run
#
# Prerequisites:
#   - i686-elf-gcc or gcc with -m32 support
#   - QEMU (qemu-system-i386 or qemu-system-x86_64)
#   - GRUB (for creating bootable ISO) — optional, can use -kernel direct

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

SOURCE="$1"
RUN_FLAG="${2:---run}"

if [ -z "$SOURCE" ]; then
    echo "Usage: $0 <source.py> [--run]"
    exit 1
fi

# Step 1: Transpile to C++ (freestanding mode)
echo "=== Transpiling $SOURCE ==="
python3 -m pyck "$SOURCE" \
    --target=freestanding \
    --arch=x86 \
    --transpile-only \
    -o /tmp/pyck_kernel.cpp

echo "C++ source written to /tmp/pyck_kernel.cpp"

# Step 2: Compile to ELF
echo "=== Compiling ==="

# Find a suitable compiler
CC=""
if command -v i686-elf-gcc &> /dev/null; then
    CC="i686-elf-gcc"
elif command -v i686-linux-gnu-gcc &> /dev/null; then
    CC="i686-linux-gnu-gcc"
elif command -v gcc &> /dev/null; then
    CC="gcc"
else
    echo "error: no C compiler found"
    exit 1
fi

$CC -m32 -ffreestanding -nostdlib -fno-pic \
    -I "$PROJECT_DIR/runtime/freestanding" \
    -I "$SCRIPT_DIR" \
    -T "$SCRIPT_DIR/linker.ld" \
    -o /tmp/pyck_kernel.elf \
    "$SCRIPT_DIR/start.S" \
    "$SCRIPT_DIR/py_print_vga.cpp" \
    "$PROJECT_DIR/runtime/freestanding/py_runtime.cpp" \
    /tmp/pyck_kernel.cpp

echo "ELF binary written to /tmp/pyck_kernel.elf"

# Step 3: Run in QEMU (if --run)
if [ "$RUN_FLAG" = "--run" ]; then
    echo "=== Running in QEMU ==="
    if command -v qemu-system-i386 &> /dev/null; then
        qemu-system-i386 \
            -kernel /tmp/pyck_kernel.elf \
            -nographic \
            -serial stdio \
            -m 128M
    elif command -v qemu-system-x86_64 &> /dev/null; then
        qemu-system-x86_64 \
            -kernel /tmp/pyck_kernel.elf \
            -nographic \
            -serial stdio \
            -m 128M
    else
        echo "QEMU not found. Install qemu-system-i386 to run."
        echo "ELF binary at /tmp/pyck_kernel.elf can be tested with:"
        echo "  qemu-system-i386 -kernel /tmp/pyck_kernel.elf"
    fi
fi

echo "=== Done ==="
