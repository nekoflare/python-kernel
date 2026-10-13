/* Minimal freestanding support stubs for PyCk.
 * Provides py_alloc, py_free, memset, memcpy, and BSS symbols.
 * This is the "arch support kit" that must be linked into any freestanding build.
 */
#include "../../runtime/freestanding/pyck_runtime.h"
#include <stdint.h>
#include <stddef.h>

/* Provided by the linker script */
extern char _bss_start[];
extern char _bss_end[];

/* Simple bump allocator using a statically-allocated heap region.
 * The user must call init_heap() before using any PyCk objects.
 */
#define HEAP_SIZE (256 * 1024)  /* 256 KiB default heap */
static uint8_t _heap[HEAP_SIZE];
static size_t _heap_offset = 0;

extern "C" void init_heap(void* base, size_t size) {
    /* Override the static heap if a real heap is provided */
    (void)base; (void)size;
}

extern "C" void* py_alloc(size_t size) {
    /* Align to 8 bytes */
    size = (size + 7) & ~(size_t)7;
    if (_heap_offset + size > HEAP_SIZE) {
        /* Every allocation call site immediately dereferences the
         * result to fill in fields (ob_refcnt, ob_type, ...), so
         * returning nullptr here silently turned running out of heap
         * into a null-pointer write and a triple fault/reboot with no
         * diagnostic at all -- see the same fix in
         * arch/x86_64_graphical/support.cpp, discovered when a
         * pixel-drawing demo exhausted a too-small heap. */
        py_fatal_error("out of memory");
    }
    void* ptr = &_heap[_heap_offset];
    _heap_offset += size;
    return ptr;
}

extern "C" void py_free(void* ptr) {
    /* Bump allocator — no-op free (memory is never reclaimed) */
    (void)ptr;
}

/* Minimal memset/memcpy for freestanding */
extern "C" void* memset(void* dest, int ch, size_t count) {
    unsigned char* d = (unsigned char*)dest;
    for (size_t i = 0; i < count; i++) {
        d[i] = (unsigned char)ch;
    }
    return dest;
}

extern "C" void* memcpy(void* dest, const void* src, size_t count) {
    unsigned char* d = (unsigned char*)dest;
    const unsigned char* s = (const unsigned char*)src;
    for (size_t i = 0; i < count; i++) {
        d[i] = s[i];
    }
    return dest;
}

/* x86 port I/O */
extern "C" uint8_t inb(uint16_t port) {
    uint8_t result;
    __asm__ volatile("inb %1, %0" : "=a"(result) : "Nd"(port));
    return result;
}

extern "C" void outb(uint16_t port, uint8_t val) {
    __asm__ volatile("outb %0, %1" : : "a"(val), "Nd"(port));
}

/* Compiler builtin stubs — proper 64-bit implementations for i386 GCC ABI.
 * GCC passes each int64_t as two 32-bit halves on the stack:
 *   __moddi3(int64_t a, int64_t b):
 *     [ebp+8]  = a_lo,  [ebp+12] = a_hi
 *     [ebp+16] = b_lo,  [ebp+20] = b_hi
 */
static uint64_t _udiv64(uint64_t dividend, uint64_t divisor, uint64_t* remainder) {
    if (divisor == 0) { if (remainder) *remainder = dividend; return 0; }
    uint64_t q = 0, r = 0;
    for (int i = 63; i >= 0; i--) {
        r = (r << 1) | ((dividend >> i) & 1);
        if (r >= divisor) { r -= divisor; q |= (1ULL << i); }
    }
    if (remainder) *remainder = r;
    return q;
}

extern "C" int64_t __moddi3(int64_t a, int64_t b) {
    uint64_t ua = a < 0 ? -(uint64_t)a : (uint64_t)a;
    uint64_t ub = b < 0 ? -(uint64_t)b : (uint64_t)b;
    uint64_t rem;
    _udiv64(ua, ub, &rem);
    return a < 0 ? -(int64_t)rem : (int64_t)rem;
}

extern "C" int64_t __divdi3(int64_t a, int64_t b) {
    uint64_t ua = a < 0 ? -(uint64_t)a : (uint64_t)a;
    uint64_t ub = b < 0 ? -(uint64_t)b : (uint64_t)b;
    uint64_t q = _udiv64(ua, ub, 0);
    if ((a < 0) != (b < 0)) q = -(uint64_t)q;
    return (int64_t)q;
}

extern "C" void __stack_chk_fail(void) {
    /* Stack protector stub — just halt */
    while(1) {}
}
