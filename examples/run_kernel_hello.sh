#!/bin/bash
# Transpile and compile the kernel hello world for freestanding x86
# Then boot via GRUB ISO (QEMU SeaBIOS does not support Multiboot1 -kernel)
set -e
cd "$(dirname "$0")/.."

echo "=== Transpiling kernel_hello.py ==="
python3 -m pyck examples/kernel_hello.py --target=freestanding --arch=x86 \
    --transpile-only -o /tmp/pyck_kernel_hello.cpp

echo "=== Compiling ==="
g++ -std=c++17 -m32 -ffreestanding -nostdlib -fno-exceptions -fno-rtti \
    -fno-stack-protector \
    -static -nodefaultlibs -nostartfiles -no-pie -fno-pic -fno-PIE \
    -I runtime/freestanding \
    -I arch/x86 \
    -T arch/x86/linker.ld \
    -o /tmp/pyck_kernel.elf \
    arch/x86/start.S \
    arch/x86/py_print_vga.cpp \
    arch/x86/support.cpp \
    runtime/freestanding/py_runtime.cpp \
    /tmp/pyck_kernel_hello.cpp

echo "=== Creating GRUB ISO ==="
ISO_DIR="/tmp/pyck_iso"
rm -rf "$ISO_DIR"
mkdir -p "$ISO_DIR/boot/grub"
cp /tmp/pyck_kernel.elf "$ISO_DIR/boot/kernel.elf"

cat > "$ISO_DIR/boot/grub/grub.cfg" << 'GRUB_CFG'
set timeout=0
set default=0

menuentry "PyCk Kernel" {
    multiboot /boot/kernel.elf
    boot
}
GRUB_CFG

grub-mkrescue -o /tmp/pyck_kernel.iso "$ISO_DIR" 2>&1 | tail -1

echo "=== Running in QEMU ==="
echo "Output will appear on serial (stdout):"
timeout 5 qemu-system-i386 -cdrom /tmp/pyck_kernel.iso -m 128M \
    -nographic -boot d 2>&1 || true
