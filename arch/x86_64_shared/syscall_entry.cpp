/* x86-64 SYSCALL/SYSRET support -- the actual mechanism used by real
 * Linux binaries (like apps/hello), not a software interrupt.
 *
 * `syscall` (any privilege level, once IA32_EFER.SCE is set) saves the
 * instruction *after* itself into RCX and RFLAGS into R11, loads CS/SS
 * from IA32_STAR's "SYSCALL" field (this kernel's own, CPL0 -- see
 * PythonOS/kernel/syscall.py's init_syscalls()), and jumps to
 * IA32_LSTAR -- entirely in hardware, no IDT/GDT gate involved. Unlike
 * an interrupt, it does *not* switch stacks or push anything: RSP is
 * whatever the caller had.
 *
 * The loaded app (see PythonOS/kernel/elf.py, threads.py) now runs at
 * real CPL3 in its own address space, so the return trip genuinely is
 * `sysretq`: it reloads CS/SS from STAR's SYSRET field (RPL forced to
 * 3), RIP from RCX, and RFLAGS from R11 -- one instruction does the
 * privilege change, the jump, and the flag restore that a manual
 * return would otherwise need done by hand.
 *
 * Swaps to a dedicated kernel stack before touching anything else --
 * not just defensive hardening against a hostile RSP (this kernel only
 * loads self-built binaries), but a real correctness requirement:
 * on_syscall()'s SYS_EXIT handling calls schedule(), which calls
 * write_cr3(). The instant that runs, whatever stack the caller was
 * still using becomes unmapped if it lived in the exiting process's own
 * (now-abandoned) lower half -- exactly what the caller's own RSP is.
 * Running this whole handler on a *kernel* stack instead means it's
 * still mapped no matter which CR3 ends up active by the time
 * schedule() returns (or doesn't).
 *
 * That kernel stack is *per-thread* (_pyck_syscall_stack_top, below --
 * PythonOS/kernel/threads.py's schedule() keeps it pointed at whichever
 * thread is current, via _SET_SYSCALL_STACK_TOP()), not one shared
 * buffer. A single shared buffer was fine as long as only one userspace
 * thread could ever exist; fork() broke that assumption the moment a
 * parent could be sitting *mid-syscall* (blocked inside its own
 * SYS_WAIT4 loop, deep in this same C++ call chain, having called
 * schedule() itself) while a completely different thread -- the child
 * -- entered *this exact same asm* for its own, unrelated syscalls.
 * Both would swap onto the identical fixed address, so the child's
 * syscall handling silently overwrote whatever the blocked parent's
 * call chain had sitting there -- confirmed by boot: a parent resumed
 * after its child died would immediately jump into an address that
 * belonged to the child's own, already-finished execution, because
 * that's literally whose stack frame it read on resuming. Each thread
 * now getting its own dedicated stack for this exact purpose (the
 * per-thread kernel stack threads.py already allocates for
 * switch_context()'s own bookkeeping -- see setup_fork_child()) closes
 * that hole the same way per-thread page tables close the analogous
 * hole for memory.
 *
 * The saved-register block handed to syscall_dispatch/on_syscall is the
 * same idea as isr_common's for interrupts: raw bytes at fixed offsets,
 * decoded on the PyCk side (see PythonOS/kernel/syscall.py) rather than
 * via individual C arguments -- the real x86-64 syscall calling
 * convention (num=rax, args in rdi/rsi/rdx/r10/r8/r9, *not* rdi/rsi/
 * rdx/rcx/r8/r9 -- r10 stands in for the 4th argument specifically
 * because rcx is already spoken for by `syscall` itself) doesn't map
 * onto a plain 7-argument C call anyway. on_syscall() writing back into
 * the saved rax slot (offset 0) is what sets the return value the
 * caller sees after resuming -- exactly how the real syscall ABI
 * overloads rax for both the call number and the result.
 */
#include "pyck_runtime.h"
#include <stdint.h>

extern "C" __attribute__((weak)) void _pyck_on_syscall(PyObject* regs) {
    (void)regs;
    py_fatal_error("unhandled syscall (no on_syscall() defined)");
}

extern "C" void syscall_dispatch(uint64_t* regs) {
    _pyck_on_syscall(_pyck_long_from_long((int64_t)(uintptr_t)regs));
}

extern "C" void syscall_entry();

extern "C" uint64_t _pyck_syscall_entry_addr(void) {
    return (uint64_t)(uintptr_t)syscall_entry;
}

/* The top of whichever thread's own dedicated kernel stack
 * syscall_entry should swap onto next -- kept current by
 * PythonOS/kernel/threads.py's schedule() (see _SET_SYSCALL_STACK_TOP()
 * below), which sets it to the newly-current thread's own stack every
 * time it switches, the same way it already sets CR3. Defaults to 0,
 * never actually read at that value: userspace can't execute (so a
 * `syscall` instruction can't happen) until init_threads() has already
 * performed its first switch into thread 0, and every switch after
 * that goes through schedule(), which sets this before `switch_context`
 * ever hands control to a thread that could make a syscall. */
extern "C" {
uint64_t _pyck_syscall_stack_top = 0;
}

extern "C" void _pyck_set_syscall_stack_top(uint64_t top) {
    _pyck_syscall_stack_top = top;
}

/* Saved-register block layout (byte offsets from the pointer
 * syscall_dispatch/on_syscall receive):
 *   0  rax (syscall number in; return value out)   64  rcx (saved return rip)
 *   8  rdi (arg1)                                   72  r15
 *   16 rsi (arg2)                                   80  r14
 *   24 rdx (arg3)                                   88  r13
 *   32 r10 (arg4)                                   96  r12
 *   40 r8  (arg5)                                   104 rbp
 *   48 r9  (arg6)                                   112 rbx
 *   56 r11 (saved rflags)
 *
 * The last six (r15-rbx) are callee-saved by the ordinary x86-64 ABI --
 * nothing in this call chain (isr_common excluded; this is the
 * SYSCALL/SYSRET path, not an interrupt) ever had a reason to touch
 * them before, so a real syscall body could get away with leaving them
 * live in the actual CPU registers untouched the whole time. fork()
 * changes that: its child is a *new* thread, not a resumption of this
 * one, so it needs its own saved copy of every register the parent had
 * at the moment of the call -- including these -- to build a correct
 * initial context from (see PythonOS/kernel/syscall.py's SYS_FORK).
 * Pushing them here (and never popping them back into real registers,
 * since nothing here ever moved them out of place to begin with) is
 * what makes them visible to on_syscall() at all. */
asm(
    ".text\n"
    ".global syscall_entry\n"
    "syscall_entry:\n"
    /* Swap onto the current thread's own syscall stack (see
     * _pyck_syscall_stack_top above), stashing the caller's original
     * RSP (and RBX, borrowed as scratch here and restored before
     * returning -- the syscall ABI requires rbx/rbp/r12-r15 survive a
     * syscall unchanged) on the new stack, using the old one for
     * exactly one more instruction while it's still guaranteed valid:
     * an `xchg` with %rsp is what makes swapping stacks *and*
     * recovering the old value possible without a second scratch
     * register or a hardcoded old-stack slot. A `mov` (load the
     * pointer *value* this variable holds), not a `lea` (which would
     * compute the fixed address of the variable itself) -- unlike the
     * single shared buffer this replaced, the actual target moves
     * every time schedule() runs. */
    "push %rbx\n"
    "mov _pyck_syscall_stack_top(%rip), %rbx\n"
    "xchg %rbx, %rsp\n"
    "push %rbx\n"
    /* %rbx now holds the caller's old RSP (just pushed, but still also
     * live in the register) -- `push (%rbx)` reads straight through it
     * to recover the *real* rbx value the very first push stashed on
     * the caller's own (now-inactive) stack, without needing a second
     * scratch register to hold it in. */
    "push (%rbx)\n"
    "push %rbp\n"
    "push %r12\n"
    "push %r13\n"
    "push %r14\n"
    "push %r15\n"
    /* SysV wants RSP == 8 (mod 16) at a *called* function's entry. 16
     * pushes below (128 bytes total: the old-RSP save, the rbx copy,
     * rbp/r12-r15, and the original 9) is exactly 8*16, so `call
     * syscall_dispatch` lands on the required 8-mod-16 boundary,
     * assuming (guaranteed: threads.py's va.alloc() returns page- (so
     * 16-) aligned addresses, and STACK_SIZE, a multiple of 16, is
     * added to get each thread's own stack top) the stack was
     * 16-aligned right after the swap above. */
    "push %rcx\n"
    "push %r11\n"
    "push %r9\n"
    "push %r8\n"
    "push %r10\n"
    "push %rdx\n"
    "push %rsi\n"
    "push %rdi\n"
    "push %rax\n"
    "mov %rsp, %rdi\n"
    "call syscall_dispatch\n"
    "pop %rax\n"
    "pop %rdi\n"
    "pop %rsi\n"
    "pop %rdx\n"
    "pop %r10\n"
    "pop %r8\n"
    "pop %r9\n"
    "pop %r11\n"
    "pop %rcx\n"
    "pop %r15\n"
    "pop %r14\n"
    "pop %r13\n"
    "pop %r12\n"
    "pop %rbp\n"
    /* Discard the rbx copy (the real rbx is about to come back off the
     * caller's own stack instead, below) -- this pop's only job is to
     * keep RSP balanced. */
    "pop %rbx\n"
    /* Swap back: recover the original RSP (saved above), then read the
     * caller's original RBX back from *its* stack, now that RSP points
     * there again. */
    "pop %rbx\n"
    "xchg %rbx, %rsp\n"
    "pop %rbx\n"
    "sysretq\n"
);
