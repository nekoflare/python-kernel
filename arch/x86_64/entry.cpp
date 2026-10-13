/* Kernel entry point for PyCk freestanding x86_64, booted via Limine.
 *
 * Per the Limine boot protocol, by the time this function is called the
 * CPU is already in 64-bit long mode with paging enabled, the kernel is
 * mapped at its linked higher-half address, all legacy PIC IRQs are
 * masked, and RSP points at a bootloader-provided stack of at least
 * 64KiB. Standard ELF loading has already zeroed .bss. So, unlike the
 * 32-bit Multiboot1 target, no assembly trampoline is needed here at all
 * -- Limine does the mode switch and setup that start.S used to do by
 * hand.
 *
 * The one thing Limine does *not* do for us is enable SSE: PyCk's float
 * type needs real `double` arithmetic, so we do the standard CR0/CR4
 * dance before calling into any PyCk-generated code.
 */
#include "limine.h"
#include <stdint.h>

extern "C" void pyck_main();

/* Limine requests must live in the .limine_requests section (kept via
 * linker.ld) and must not be optimized away -- hence "used" + volatile. */

__attribute__((used, section(".limine_requests")))
static volatile uint64_t limine_base_revision[] = LIMINE_BASE_REVISION(6);

__attribute__((used, section(".limine_requests_start")))
static volatile uint64_t limine_requests_start_marker[] = LIMINE_REQUESTS_START_MARKER;

__attribute__((used, section(".limine_requests_end")))
static volatile uint64_t limine_requests_end_marker[] = LIMINE_REQUESTS_END_MARKER;

static void hcf(void) {
    for (;;) {
        asm volatile("cli; hlt");
    }
}

static inline void enable_sse(void) {
    uint64_t cr0, cr4;
    asm volatile("mov %%cr0, %0" : "=r"(cr0));
    cr0 &= ~(1ULL << 2);   /* clear EM (emulation) */
    cr0 |= (1ULL << 1);    /* set MP (monitor coprocessor) */
    asm volatile("mov %0, %%cr0" :: "r"(cr0));

    asm volatile("mov %%cr4, %0" : "=r"(cr4));
    cr4 |= (1ULL << 9);    /* set OSFXSR (enable SSE/FXSAVE) */
    cr4 |= (1ULL << 10);   /* set OSXMMEXCPT (enable unmasked SSE exceptions) */
    asm volatile("mov %0, %%cr4" :: "r"(cr4));
}

extern "C" void pyck_kernel_start(void) {
    if (!LIMINE_BASE_REVISION_SUPPORTED(limine_base_revision)) {
        hcf();
    }

    enable_sse();
    pyck_main();
    hcf();
}
