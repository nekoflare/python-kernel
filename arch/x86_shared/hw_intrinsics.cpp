/* Shared x86 hardware intrinsics: wider port I/O, MSRs, CPUID, and control
 * registers. Built for arch/x86, arch/x86_64, and arch/x86_64_graphical
 * alike (see pyck/__main__.py's _build_compile_command) -- one file, not
 * one copy per arch.
 *
 * This works unmodified under both -m32 and -m64 because control-register
 * accessors are written in terms of `unsigned long`, which GCC sizes to
 * match the target's native general-purpose register width (32 bits under
 * -m32, 64 bits under -m64): the same `mov %crN, %0` template therefore
 * assembles to `mov %crN, %eax` on one target and `mov %crN, %rax` on the
 * other, without any conditional compilation. Port I/O, MSRs, and CPUID
 * all operate on fixed-width registers (8/16/32/64-bit) regardless of
 * processor mode, so those need no such trick.
 *
 * inb/outb (byte-width port I/O) already exist per-arch in each support.cpp
 * and are reused directly by codegen; only the additional widths and the
 * MSR/CPUID/control-register accessors are added here.
 */
#include <stdint.h>

extern "C" {

/* --- Port I/O (16- and 32-bit; 8-bit inb/outb already exist per-arch) --- */

uint16_t _pyck_inw(uint16_t port) {
    uint16_t result;
    __asm__ volatile("inw %1, %0" : "=a"(result) : "Nd"(port));
    return result;
}

void _pyck_outw(uint16_t port, uint16_t val) {
    __asm__ volatile("outw %0, %1" : : "a"(val), "Nd"(port));
}

uint32_t _pyck_inl(uint16_t port) {
    uint32_t result;
    __asm__ volatile("inl %1, %0" : "=a"(result) : "Nd"(port));
    return result;
}

void _pyck_outl(uint16_t port, uint32_t val) {
    __asm__ volatile("outl %0, %1" : : "a"(val), "Nd"(port));
}

/* --- Model-specific registers --- */
/* RDMSR/WRMSR always transfer a 64-bit value split as EDX:EAX, in both
 * 32-bit and 64-bit (long) mode -- the instruction encoding never grew a
 * 64-bit register form, so this is identical on every x86 target. */

uint64_t _pyck_rdmsr(uint32_t msr) {
    uint32_t lo, hi;
    __asm__ volatile("rdmsr" : "=a"(lo), "=d"(hi) : "c"(msr));
    return ((uint64_t)hi << 32) | lo;
}

void _pyck_wrmsr(uint32_t msr, uint64_t value) {
    uint32_t lo = (uint32_t)value;
    uint32_t hi = (uint32_t)(value >> 32);
    __asm__ volatile("wrmsr" : : "c"(msr), "a"(lo), "d"(hi));
}

/* --- CPUID --- */
/* CPUID's registers are always 32-bit regardless of processor mode.
 * ECX (sub-leaf) input is fixed at 0 -- callers needing a specific sub-leaf
 * (e.g. leaf 4 or 7) aren't served by this simplified single-argument
 * form; that's a possible future addition, not needed yet. */

static void _pyck_cpuid(uint32_t leaf, uint32_t* eax, uint32_t* ebx, uint32_t* ecx, uint32_t* edx) {
    __asm__ volatile("cpuid"
                      : "=a"(*eax), "=b"(*ebx), "=c"(*ecx), "=d"(*edx)
                      : "a"(leaf), "c"(0));
}

uint32_t _pyck_cpuid_eax(uint32_t leaf) {
    uint32_t a, b, c, d;
    _pyck_cpuid(leaf, &a, &b, &c, &d);
    return a;
}

uint32_t _pyck_cpuid_ebx(uint32_t leaf) {
    uint32_t a, b, c, d;
    _pyck_cpuid(leaf, &a, &b, &c, &d);
    return b;
}

uint32_t _pyck_cpuid_ecx(uint32_t leaf) {
    uint32_t a, b, c, d;
    _pyck_cpuid(leaf, &a, &b, &c, &d);
    return c;
}

uint32_t _pyck_cpuid_edx(uint32_t leaf) {
    uint32_t a, b, c, d;
    _pyck_cpuid(leaf, &a, &b, &c, &d);
    return d;
}

/* --- Control registers --- */
/* See file header: `unsigned long` matches the native register width per
 * target, so one definition per register serves every x86 arch. */

unsigned long _pyck_read_cr0(void) {
    unsigned long value;
    __asm__ volatile("mov %%cr0, %0" : "=r"(value));
    return value;
}
void _pyck_write_cr0(unsigned long value) {
    __asm__ volatile("mov %0, %%cr0" :: "r"(value));
}

unsigned long _pyck_read_cr2(void) {
    unsigned long value;
    __asm__ volatile("mov %%cr2, %0" : "=r"(value));
    return value;
}
void _pyck_write_cr2(unsigned long value) {
    __asm__ volatile("mov %0, %%cr2" :: "r"(value));
}

unsigned long _pyck_read_cr3(void) {
    unsigned long value;
    __asm__ volatile("mov %%cr3, %0" : "=r"(value));
    return value;
}
void _pyck_write_cr3(unsigned long value) {
    __asm__ volatile("mov %0, %%cr3" :: "r"(value));
}

unsigned long _pyck_read_cr4(void) {
    unsigned long value;
    __asm__ volatile("mov %%cr4, %0" : "=r"(value));
    return value;
}
void _pyck_write_cr4(unsigned long value) {
    __asm__ volatile("mov %0, %%cr4" :: "r"(value));
}

} // extern "C"
