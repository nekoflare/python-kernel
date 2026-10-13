#!/bin/bash
# Transpile and compile the kernel hello world for freestanding x86_64,
# booted via Limine (https://limine-bootloader.org), then boot via QEMU.
#
# Unlike run_kernel_hello.sh (32-bit Multiboot1 + GRUB), this needs no
# custom assembly entry stub or GRUB: Limine already brings the CPU up in
# 64-bit long mode with paging enabled before jumping to our entry point.
# See arch/x86_64/entry.cpp for details.
#
# The Limine bootloader binaries and deploy tool are downloaded once (from
# the pinned v12.7.0 release) and cached under LIMINE_CACHE below; nothing
# is vendored into the repo.
set -e
cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"

LIMINE_VERSION="v12.7.0"
LIMINE_CACHE="/tmp/pyck-limine-${LIMINE_VERSION}"

echo "=== Transpiling + compiling kernel_hello.py (x86_64) ==="
python3 -m pyck examples/kernel_hello.py --target=freestanding --arch=x86_64 \
    -o /tmp/pyck_kernel_x64.elf

echo "=== Fetching Limine ${LIMINE_VERSION} (cached at $LIMINE_CACHE) ==="
if [ ! -x "$LIMINE_CACHE/limine-binary/limine" ]; then
    rm -rf "$LIMINE_CACHE"
    mkdir -p "$LIMINE_CACHE"
    curl -sL "https://github.com/limine-bootloader/limine/releases/download/${LIMINE_VERSION}/limine-binary.tar.gz" \
        -o "$LIMINE_CACHE/limine-binary.tar.gz"
    tar -xzf "$LIMINE_CACHE/limine-binary.tar.gz" -C "$LIMINE_CACHE"
    cc -std=c99 -D_FILE_OFFSET_BITS=64 \
        "$LIMINE_CACHE/limine-binary/limine.c" -o "$LIMINE_CACHE/limine-binary/limine"
fi
LIMINE_BIN="$LIMINE_CACHE/limine-binary"

echo "=== Assembling bootable ISO ==="
ISO_DIR="/tmp/pyck_kernel_x64_iso"
rm -rf "$ISO_DIR"
mkdir -p "$ISO_DIR/boot/limine" "$ISO_DIR/EFI/BOOT"
cp /tmp/pyck_kernel_x64.elf "$ISO_DIR/boot/kernel"
cat > "$ISO_DIR/boot/limine/limine.conf" << 'LIMINE_CONF'
timeout: 0

/PyCk Kernel
    protocol: limine
    path: boot():/boot/kernel
LIMINE_CONF
cp "$LIMINE_BIN/limine-bios.sys" "$LIMINE_BIN/limine-bios-cd.bin" "$LIMINE_BIN/limine-uefi-cd.bin" \
    "$ISO_DIR/boot/limine/"
cp "$LIMINE_BIN/BOOTX64.EFI" "$ISO_DIR/EFI/BOOT/"

xorriso -as mkisofs -R -r -J -b boot/limine/limine-bios-cd.bin \
    -no-emul-boot -boot-load-size 4 -boot-info-table -hfsplus \
    -apm-block-size 2048 --efi-boot boot/limine/limine-uefi-cd.bin \
    -efi-boot-part --efi-boot-image --protective-msdos-label \
    "$ISO_DIR" -o /tmp/pyck_kernel_x64.iso 2>&1 | tail -3
"$LIMINE_BIN/limine" bios-install /tmp/pyck_kernel_x64.iso

echo "=== Running in QEMU ==="
echo "Output will appear on serial (stdout):"
timeout 5 qemu-system-x86_64 -M q35 -cdrom /tmp/pyck_kernel_x64.iso -boot d \
    -m 256M -nographic -serial mon:stdio 2>&1 || true
