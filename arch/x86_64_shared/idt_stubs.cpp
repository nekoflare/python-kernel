/* Real interrupt gates need the CPU-mandated entry/exit dance -- the
 * hardware itself pushes (RIP, CS, RFLAGS) onto the stack when it
 * delivers an interrupt, plus an error code for a handful of exception
 * vectors, plus (RSP, SS) too whenever the interrupt changes privilege
 * level (nothing runs below ring0 yet, so that's not reachable today,
 * but will be) -- and the handler must resume with `iretq`, not a
 * normal `ret`. That can't be produced by an ordinary PyCk function, so
 * this is the one piece of interrupt handling that has to be
 * hand-written -- mirroring exactly how limine_table.cpp is the one
 * hand-written piece needed for boot info, with everything else left
 * to PyCk source.
 *
 * The variable frame size (3 fields vs. 5, depending on whether a
 * privilege change happened) needs no special handling here: isr_common
 * never manually pops RIP/CS/RFLAGS/RSP/SS itself, it only does
 * `add $16, %rsp` to drop the vector+error-code pair it added, then
 * `iretq`s -- and `iretq` reads however many fields actually belong to
 * the frame based on the CS value it's restoring. PythonOS/kernel/
 * idt.py's on_interrupt() detects which shape it got the same way (by
 * checking the saved CS's RPL bits) before it dereferences RSP/SS.
 *
 * Earlier this used GCC's `__attribute__((interrupt))`, which generates
 * that entry/exit sequence automatically -- but it only exposes the
 * CPU-pushed frame to C++, not the general-purpose registers (GCC saves
 * and restores those transparently, with no way to read them). Printing
 * *registers*, not just "which vector fired", needs actual control over
 * what gets saved, so this is now hand-written raw assembly instead:
 * every vector 0-31 (the full set of CPU-defined exceptions) plus 0x80
 * (a software interrupt this kernel defines itself, for deliberately
 * triggering a handler from PyCk source via `_ASM("int $0x80")`) gets a
 * tiny trampoline that pushes a dummy error code (only for the vectors
 * that don't already get a real one from the CPU) and its own vector
 * number, then falls into one shared body that saves every
 * general-purpose register, calls into a plain C++ dispatcher with a
 * pointer to all of it, restores everything, and `iretq`s. The
 * dispatcher hands that same pointer straight to PyCk code as a raw
 * integer -- decoding *which* register is at *which* offset, and
 * deciding what to print, is ordinary PyCk source (see
 * PythonOS/kernel/idt.py), not anything hard-coded here.
 *
 * A vector with no stub here simply isn't given an IDT entry by
 * idt.py, so it's exactly as absent/not-present as it was with no IDT
 * loaded at all -- strictly no worse, never silently wrong.
 */
#include "pyck_runtime.h"
#include <stdint.h>

/* Defined by PyCk-generated code: a top-level `def on_interrupt(vector:
 * int, regs: int)` in PyCk source is the one function codegen.py emits
 * with C linkage (see codegen.py's _EXTERN_C_FUNCS) instead of `static`,
 * precisely so this different translation unit can call it directly.
 *
 * idt_stubs.cpp/isr_stub_table[] gets linked into *every* x86_64 and
 * x86_64_graphical build, though -- print42.py, kernel_hello.py, every
 * existing test, all of PythonOS before this feature -- essentially none
 * of which define on_interrupt(). A weak definition here is the fallback
 * for those: the PyCk-generated one (a strong symbol, whenever the
 * source actually has `def on_interrupt`) simply overrides it at link
 * time, and nothing that never builds/loads an IDT pointing at these
 * stubs ever calls this fallback anyway. */
extern "C" __attribute__((weak)) void _pyck_on_interrupt(PyObject* vector, PyObject* regs) {
    (void)vector;
    (void)regs;
    py_fatal_error("unhandled interrupt (no on_interrupt() defined)");
}

/* Called with RDI = a pointer to the saved-register block isr_common
 * built on the stack (see the layout comment above the asm block
 * below) -- a plain function, entered via an ordinary `call` (not an
 * interrupt gate itself), so there's nothing unusual about it: normal
 * SysV argument passing, normal `ret`. */
extern "C" void isr_dispatch(uint64_t* regs) {
    uint64_t vector = regs[15];
    _pyck_on_interrupt(_pyck_long_from_long((int64_t)vector),
                        _pyck_long_from_long((int64_t)(uintptr_t)regs));
}

extern "C" void isr_0();
extern "C" void isr_1();
extern "C" void isr_2();
extern "C" void isr_3();
extern "C" void isr_4();
extern "C" void isr_5();
extern "C" void isr_6();
extern "C" void isr_7();
extern "C" void isr_8();
extern "C" void isr_9();
extern "C" void isr_10();
extern "C" void isr_11();
extern "C" void isr_12();
extern "C" void isr_13();
extern "C" void isr_14();
extern "C" void isr_15();
extern "C" void isr_16();
extern "C" void isr_17();
extern "C" void isr_18();
extern "C" void isr_19();
extern "C" void isr_20();
extern "C" void isr_21();
extern "C" void isr_22();
extern "C" void isr_23();
extern "C" void isr_24();
extern "C" void isr_25();
extern "C" void isr_26();
extern "C" void isr_27();
extern "C" void isr_28();
extern "C" void isr_29();
extern "C" void isr_30();
extern "C" void isr_31();
extern "C" void isr_64();
extern "C" void isr_65();
extern "C" void isr_66();
extern "C" void isr_128();

/* Raw, file-scope assembly (not inline asm inside a C++ function, so
 * none of _ASM()'s "basic vs extended asm" %-escaping rules apply here
 * -- this is exactly what a human writing a .s file by hand would
 * write). Each isr_N trampoline pushes a dummy 0 error code (skipped
 * for the 8 vectors the CPU already pushes a real one for: 8, 10-14,
 * 17, 21) then its own vector number, and falls through to
 * isr_common.
 *
 * isr_common saves all 15 general-purpose registers (rax, rbx, rcx,
 * rdx, rsi, rdi, rbp, r8-r15), passes a pointer to that block (plus the
 * vector/error-code/CPU frame sitting right above it on the stack) to
 * isr_dispatch, restores everything, drops the vector+error-code pair,
 * and `iretq`s. From that pointer, byte offsets are:
 *   0   r15        64  rbp        128 error_code
 *   8   r14        72  rdi        136 rip
 *   16  r13        80  rsi        144 cs
 *   24  r12        88  rdx        152 rflags
 *   32  r11        96  rcx        160 rsp (if privilege changed)
 *   40  r10        104 rbx        168 ss  (if privilege changed)
 *   48  r9         112 rax
 *   56  r8         120 vector
 * rsp/ss at 160/168 are only meaningful when the interrupt changed
 * privilege level (check the saved cs's RPL bits, as
 * PythonOS/kernel/idt.py's came_from_userspace() does) -- otherwise
 * the CPU never pushed them and nothing valid lives there. */
asm(
    ".text\n"
    ".global isr_0\n"
    ".global isr_1\n"
    ".global isr_2\n"
    ".global isr_3\n"
    ".global isr_4\n"
    ".global isr_5\n"
    ".global isr_6\n"
    ".global isr_7\n"
    ".global isr_8\n"
    ".global isr_9\n"
    ".global isr_10\n"
    ".global isr_11\n"
    ".global isr_12\n"
    ".global isr_13\n"
    ".global isr_14\n"
    ".global isr_15\n"
    ".global isr_16\n"
    ".global isr_17\n"
    ".global isr_18\n"
    ".global isr_19\n"
    ".global isr_20\n"
    ".global isr_21\n"
    ".global isr_22\n"
    ".global isr_23\n"
    ".global isr_24\n"
    ".global isr_25\n"
    ".global isr_26\n"
    ".global isr_27\n"
    ".global isr_28\n"
    ".global isr_29\n"
    ".global isr_30\n"
    ".global isr_31\n"
    ".global isr_64\n"
    ".global isr_128\n"

    "isr_0:\n"
    "push $0\n"
    "push $0\n"
    "jmp isr_common\n"
    "isr_1:\n"
    "push $0\n"
    "push $1\n"
    "jmp isr_common\n"
    "isr_2:\n"
    "push $0\n"
    "push $2\n"
    "jmp isr_common\n"
    "isr_3:\n"
    "push $0\n"
    "push $3\n"
    "jmp isr_common\n"
    "isr_4:\n"
    "push $0\n"
    "push $4\n"
    "jmp isr_common\n"
    "isr_5:\n"
    "push $0\n"
    "push $5\n"
    "jmp isr_common\n"
    "isr_6:\n"
    "push $0\n"
    "push $6\n"
    "jmp isr_common\n"
    "isr_7:\n"
    "push $0\n"
    "push $7\n"
    "jmp isr_common\n"
    "isr_8:\n"
    "push $8\n"
    "jmp isr_common\n"
    "isr_9:\n"
    "push $0\n"
    "push $9\n"
    "jmp isr_common\n"
    "isr_10:\n"
    "push $10\n"
    "jmp isr_common\n"
    "isr_11:\n"
    "push $11\n"
    "jmp isr_common\n"
    "isr_12:\n"
    "push $12\n"
    "jmp isr_common\n"
    "isr_13:\n"
    "push $13\n"
    "jmp isr_common\n"
    "isr_14:\n"
    "push $14\n"
    "jmp isr_common\n"
    "isr_15:\n"
    "push $0\n"
    "push $15\n"
    "jmp isr_common\n"
    "isr_16:\n"
    "push $0\n"
    "push $16\n"
    "jmp isr_common\n"
    "isr_17:\n"
    "push $17\n"
    "jmp isr_common\n"
    "isr_18:\n"
    "push $0\n"
    "push $18\n"
    "jmp isr_common\n"
    "isr_19:\n"
    "push $0\n"
    "push $19\n"
    "jmp isr_common\n"
    "isr_20:\n"
    "push $0\n"
    "push $20\n"
    "jmp isr_common\n"
    "isr_21:\n"
    "push $21\n"
    "jmp isr_common\n"
    "isr_22:\n"
    "push $0\n"
    "push $22\n"
    "jmp isr_common\n"
    "isr_23:\n"
    "push $0\n"
    "push $23\n"
    "jmp isr_common\n"
    "isr_24:\n"
    "push $0\n"
    "push $24\n"
    "jmp isr_common\n"
    "isr_25:\n"
    "push $0\n"
    "push $25\n"
    "jmp isr_common\n"
    "isr_26:\n"
    "push $0\n"
    "push $26\n"
    "jmp isr_common\n"
    "isr_27:\n"
    "push $0\n"
    "push $27\n"
    "jmp isr_common\n"
    "isr_28:\n"
    "push $0\n"
    "push $28\n"
    "jmp isr_common\n"
    "isr_29:\n"
    "push $0\n"
    "push $29\n"
    "jmp isr_common\n"
    "isr_30:\n"
    "push $0\n"
    "push $30\n"
    "jmp isr_common\n"
    "isr_31:\n"
    "push $0\n"
    "push $31\n"
    "jmp isr_common\n"
    "isr_64:\n"
    "push $0\n"
    "push $64\n"
    "jmp isr_common\n"
    "isr_65:\n"
    "push $0\n"
    "push $65\n"
    "jmp isr_common\n"
    "isr_66:\n"
    "push $0\n"
    "push $66\n"
    "jmp isr_common\n"
    "isr_128:\n"
    "push $0\n"
    "push $128\n"
    "jmp isr_common\n"

    "isr_common:\n"
    "push %rax\n"
    "push %rbx\n"
    "push %rcx\n"
    "push %rdx\n"
    "push %rsi\n"
    "push %rdi\n"
    "push %rbp\n"
    "push %r8\n"
    "push %r9\n"
    "push %r10\n"
    "push %r11\n"
    "push %r12\n"
    "push %r13\n"
    "push %r14\n"
    "push %r15\n"
    "mov %rsp, %rdi\n"
    "call isr_dispatch\n"
    "pop %r15\n"
    "pop %r14\n"
    "pop %r13\n"
    "pop %r12\n"
    "pop %r11\n"
    "pop %r10\n"
    "pop %r9\n"
    "pop %r8\n"
    "pop %rbp\n"
    "pop %rdi\n"
    "pop %rsi\n"
    "pop %rdx\n"
    "pop %rcx\n"
    "pop %rbx\n"
    "pop %rax\n"
    "add $16, %rsp\n"
    "iretq\n"
);

/* Slot order here is PythonOS/kernel/idt.py's own contract with
 * _ISR_STUB(slot): slots 0-31 are vectors 0-31 (slot == vector), slot
 * 32 is vector 0x80, slot 33 is vector 0x40 (the LAPIC timer -- see
 * PythonOS/kernel/lapic.py), slot 34 is vector 0x41 (the PS/2 keyboard,
 * routed there by the I/O APIC -- see PythonOS/kernel/ps2.py), slot 35
 * is vector 0x42 (the XHCI USB 3.0 controller -- see
 * PythonOS/kernel/xhci.py). */
static void* const isr_stub_table[] = {
    (void*)isr_0,
    (void*)isr_1,
    (void*)isr_2,
    (void*)isr_3,
    (void*)isr_4,
    (void*)isr_5,
    (void*)isr_6,
    (void*)isr_7,
    (void*)isr_8,
    (void*)isr_9,
    (void*)isr_10,
    (void*)isr_11,
    (void*)isr_12,
    (void*)isr_13,
    (void*)isr_14,
    (void*)isr_15,
    (void*)isr_16,
    (void*)isr_17,
    (void*)isr_18,
    (void*)isr_19,
    (void*)isr_20,
    (void*)isr_21,
    (void*)isr_22,
    (void*)isr_23,
    (void*)isr_24,
    (void*)isr_25,
    (void*)isr_26,
    (void*)isr_27,
    (void*)isr_28,
    (void*)isr_29,
    (void*)isr_30,
    (void*)isr_31,
    (void*)isr_128,
    (void*)isr_64,
    (void*)isr_65,
    (void*)isr_66,
};

extern "C" uint64_t _pyck_isr_stub_addr(uint64_t slot) {
    return (uint64_t)(uintptr_t)isr_stub_table[slot];
}
