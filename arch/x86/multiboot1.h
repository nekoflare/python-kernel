/* Multiboot1 header for PyCk freestanding x86 kernel.
 * QEMU's -kernel flag supports Multiboot1 directly.
 */
#ifndef MULTIBOOT1_H
#define MULTIBOOT1_H

#include <stdint.h>

#define MULTIBOOT1_MAGIC 0x1BADB002
#define MULTIBOOT1_FLAGS (1 << 16)  /* align modules on page boundaries */
#define MULTIBOOT1_CHECKSUM -(MULTIBOOT1_MAGIC + MULTIBOOT1_FLAGS)

struct multiboot1_header {
    uint32_t magic;
    uint32_t flags;
    uint32_t checksum;
} __attribute__((packed));

#endif /* MULTIBOOT1_H */
