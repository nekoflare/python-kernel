#!/bin/bash
set -e
cd "$(dirname "$0")/.."

echo "=== Transpiling ==="
python3 -m pyck examples/print42.py --target=freestanding --arch=x86 \
    --transpile-only -o /tmp/pyck_print42.cpp

echo "=== Compiling ==="
g++ -std=c++17 -m32 -ffreestanding -nostdlib -fno-exceptions -fno-rtti \
    -fno-stack-protector \
    -static -nodefaultlibs -nostartfiles -no-pie -fno-pic -fno-PIE \
    -I runtime/freestanding \
    -I arch/x86 \
    -T arch/x86/linker.ld \
    -o /tmp/pyck_print42.elf \
    arch/x86/start.S \
    arch/x86/py_print_vga.cpp \
    arch/x86/support.cpp \
    runtime/freestanding/py_runtime.cpp \
    /tmp/pyck_print42.cpp

echo "=== Creating GRUB ISO ==="
ISO_DIR="/tmp/pyck_print42_iso"
rm -rf "$ISO_DIR"
mkdir -p "$ISO_DIR/boot/grub"
cp /tmp/pyck_print42.elf "$ISO_DIR/boot/kernel.elf"
cat > "$ISO_DIR/boot/grub/grub.cfg" << 'EOF'
set timeout=0
set default=0
menuentry "PyCk" {
    multiboot /boot/kernel.elf
    boot
}
EOF
grub-mkrescue -o /tmp/pyck_print42.iso "$ISO_DIR" 2>&1 | tail -1

echo "=== Running ==="
timeout 5 qemu-system-i386 -cdrom /tmp/pyck_print42.iso -m 128M \
    -nographic -boot d 2>&1 || true
