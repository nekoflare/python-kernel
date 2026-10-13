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
 *
 * This runtime never frees anything it allocates (every int/str/tuple/
 * class instance leaks by design -- see py_free() below), so any
 * program doing a non-trivial amount of arithmetic burns through this
 * heap steadily -- and every byte of it, while comfortably within the
 * 8G QEMU is launched with (see PythonOS/build.py's run_qemu_*
 * functions), is leaked for good the moment it's handed out. 8MiB
 * silently ran out partway through drawing a pentagram; 64MiB and then
 * 128MiB each ran out in turn once the xHCI/USB-HID driver work added
 * its own steady churn of boxed ints and strings (every print_satanic()
 * call, hex()/int_to_str() conversion, and debug dump allocates fresh
 * objects that are never reclaimed) on top of the kernel's existing
 * demo sequence. 256MiB for now. */
#define HEAP_SIZE (256 * 1024 * 1024)
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
        /* Used to return nullptr here, silently -- every allocation call
         * site immediately dereferences the result to fill in fields
         * (ob_refcnt, ob_type, ...), so running out of heap turned into
         * a null-pointer write and a triple fault/reboot with no
         * diagnostic at all, instead of a clear message. */
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

/* Minimal memset/memcpy for freestanding.
 *
 * 8 bytes per step, not 1: this kernel compiles at -O0 (no optimizer
 * around to auto-vectorize a naive byte loop for you), and these two
 * functions are the actual hot path behind every framebuffer scroll
 * and screen clear (see arch/x86_64_graphical/graphics.cpp) -- a
 * byte-at-a-time loop here was still the real bottleneck no amount of
 * calling it "once per row instead of once per pixel" could fix, since
 * the loop *inside* memset/memcpy still ran once per individual byte
 * regardless of how few times the function itself got called. The tail
 * loop below only ever runs 0-7 times per call. */
extern "C" void* memset(void* dest, int ch, size_t count) {
    unsigned char* d = (unsigned char*)dest;
    uint64_t pattern = (uint64_t)(unsigned char)ch * 0x0101010101010101ULL;
    size_t i = 0;
    while (i + 8 <= count) {
        *(uint64_t*)(d + i) = pattern;
        i += 8;
    }
    while (i < count) {
        d[i] = (unsigned char)ch;
        i++;
    }
    return dest;
}

extern "C" void* memcpy(void* dest, const void* src, size_t count) {
    unsigned char* d = (unsigned char*)dest;
    const unsigned char* s = (const unsigned char*)src;
    size_t i = 0;
    while (i + 8 <= count) {
        *(uint64_t*)(d + i) = *(const uint64_t*)(s + i);
        i += 8;
    }
    while (i < count) {
        d[i] = s[i];
        i++;
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
