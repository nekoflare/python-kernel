/* Multiboot2 header for PyCk freestanding x86 kernel.
 * This goes in its own section so the linker can place it at the start
 * of the ELF, which GRUB / Multiboot2-compliant bootloaders require.
 */
#ifndef MULTIBOOT2_H
#define MULTIBOOT2_H

#include <stdint.h>

/* Multiboot2 magic value */
#define MULTIBOOT2_MAGIC 0xe85250d6

/* Multiboot2 architecture: i386 = 0 */
#define MULTIBOOT2_ARCH 0

/* Header size (computed at link time, placeholder here) */
/* We use a fixed header size of 24 bytes (header + one tag) */

/* Multiboot2 header tag types */
#define MULTIBOOT2_TAG_END          0
#define MULTIBOOT2_TAG_INFO_REQUEST 1
#define MULTIBOOT2_TAG_FRAMEBUFFER  5

/* Multiboot2 header flags */
#define MULTIBOOT2_MEMORY_INFO     (1 << 1)

/* Multiboot2 tag_end */
struct multiboot2_tag_end {
    uint32_t type;
    uint32_t size;
};

/* Multiboot2 header structure */
struct multiboot2_header {
    uint32_t magic;
    uint32_t architecture;
    uint32_t header_length;
    uint32_t checksum;
};

/* Compute header checksum */
#define MULTIBOOT2_CHECKSUM(magic, arch, len) \
    (0x100000000 - (magic + arch + len))

#endif /* MULTIBOOT2_H */
