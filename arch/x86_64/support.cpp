/* Minimal freestanding support stubs for PyCk x86_64 (Limine).
 * Provides py_alloc, py_free, memset, memcpy, and x86 port I/O.
 *
 * Unlike the 32-bit arch/x86/support.cpp, no __moddi3/__divdi3 compiler-rt
 * helpers are needed here: x86-64 hardware divides 64-bit operands
 * natively, so the compiler never needs to call out to a software helper
 * for int64_t '/' or '%'.
 */
#include "../../runtime/freestanding/pyck_runtime.h"
#include <stdint.h>
#include <stddef.h>

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
