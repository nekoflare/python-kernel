/* Kernel entry point for PythonOS, booted via Limine.
 *
 * Per the Limine boot protocol, by the time this function is called the
 * CPU is already in 64-bit long mode with paging enabled, the kernel is
 * mapped at its linked higher-half address, all legacy PIC IRQs are
 * masked, and RSP points at a bootloader-provided stack of at least
 * 64KiB. Standard ELF loading has already zeroed .bss. So there's no
 * assembly trampoline needed here -- Limine does the mode switch and
 * setup that a Multiboot1 kernel's start.S would otherwise do by hand.
 *
 * The one thing Limine does *not* do for us is enable SSE: PyCk's float
 * type needs real `double` arithmetic, so we do the standard CR0/CR4
 * dance before calling into any PyCk-generated code.
 */
#include "limine.h"
#include <stdint.h>

extern "C" void pyck_main();
extern "C" void graphics_init(struct limine_framebuffer* fb);

/* Declared (not defined) here: the actual framebuffer request lives in
 * arch/x86_64_shared/limine_table.cpp, shared with the generic
 * _LIMINE() table PyCk source walks itself. Limine panics ("Conflict
 * detected for request ID") if the same request ID is declared twice, so
 * there can only be the one definition -- this is just a second
 * translation unit's view of it. */
extern volatile struct limine_framebuffer_request framebuffer_request;

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
        /* Not "cli; hlt": that would park the CPU with interrupts
         * permanently off, so a periodic interrupt (e.g. the LAPIC
         * timer armed in main()) could never be delivered. Plain hlt
         * sleeps until the next interrupt, runs its handler, then
         * loops back here to sleep again. */
        asm volatile("hlt");
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

/* VEX-prefixed instructions (AVX and later) #UD unconditionally unless
 * CR4.OSXSAVE is set and XCR0 has enabled the relevant state components
 * via xsetbv -- regardless of whether the CPU itself supports AVX. A
 * real glibc CRT hits this immediately: static PIE/EXEC startup
 * (_dl_aux_init, IFUNC resolvers picking a memcpy/strlen variant for
 * this CPU, ...) uses plain AVX instructions like `vpxor` on any CPU
 * that reports the feature via CPUID, with no fallback path for "AVX
 * exists but the OS never turned it on" -- that's supposed to be
 * unreachable outside of exactly this kind of freestanding kernel.
 * xsetbv itself needs CR4.OSXSAVE=1 first (it's #UD otherwise), hence
 * the ordering below.
 *
 * Setting CR4.OSXSAVE when CPUID.01H:ECX.XSAVE (bit 26) is 0 is
 * unconditionally #GP -- no IDT is loaded yet this early in boot (that
 * only happens later, inside main()), so a fault here has nowhere to
 * go and just freezes the VCPU with no diagnostic at all. Checked
 * empirically: this exact freeze is what an unconditional CR4 write
 * caused on this QEMU CPU model, so the check below isn't theoretical
 * hardening, it's load-bearing. */
static inline bool cpu_has_xsave(void) {
    uint32_t eax = 1, ebx, ecx, edx;
    asm volatile("cpuid" : "=a"(eax), "=b"(ebx), "=c"(ecx), "=d"(edx) : "a"(eax));
    return (ecx & (1u << 26)) != 0;
}

static inline void enable_avx(void) {
    if (!cpu_has_xsave()) {
        return;
    }
    uint64_t cr4;
    asm volatile("mov %%cr4, %0" : "=r"(cr4));
    cr4 |= (1ULL << 18);   /* set OSXSAVE (enable xsave/xrstor/xsetbv) */
    asm volatile("mov %0, %%cr4" :: "r"(cr4));

    uint32_t eax = 0x7;    /* bit0 x87, bit1 SSE, bit2 AVX */
    uint32_t edx = 0;
    asm volatile("xsetbv" :: "a"(eax), "d"(edx), "c"(0u));
}

extern "C" void pyck_kernel_start(void) {
    if (!LIMINE_BASE_REVISION_SUPPORTED(limine_base_revision)) {
        hcf();
    }

    enable_sse();
    enable_avx();

    struct limine_framebuffer* fb = nullptr;
    if (framebuffer_request.response != nullptr &&
        framebuffer_request.response->framebuffer_count >= 1) {
        fb = framebuffer_request.response->framebuffers[0];
    }
    graphics_init(fb);

    pyck_main();
    hcf();
}
