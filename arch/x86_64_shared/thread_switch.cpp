/* Kernel thread context switching.
 *
 * Same story as idt_stubs.cpp: switching stacks means saving/restoring
 * raw callee-saved registers no ordinary PyCk function can produce (a
 * PyCk function's own prologue/epilogue is compiler-generated and gives
 * no control over exactly what's pushed where), so that one part stays
 * hand-written assembly. Everything else -- what a thread actually does,
 * deciding which thread runs next, building a fresh thread's initial
 * fake stack via poke64 -- is ordinary PyCk source (see
 * PythonOS/kernel/threads.py).
 *
 * _pyck_switch_context(old_rsp_addr, new_rsp) pushes RFLAGS and the six
 * callee-saved registers (SysV: rbp, rbx, r12-r15), saves the resulting
 * RSP to the raw memory cell at old_rsp_addr, loads RSP = new_rsp, pops
 * that same set back off (now *that* stack's own previously-saved
 * values), and `ret`s -- into whatever return address sits on top of
 * the stack it just switched to.
 *
 * RFLAGS is pushed/popped here, not left for whatever unwinds through
 * this call to handle, because this isn't only ever called from inside
 * on_interrupt()'s own C call chain (isr_common -> isr_dispatch ->
 * _pyck_on_interrupt -> ... -> switch_context, sitting on top of a real
 * hardware interrupt frame whose own `iretq` would otherwise restore
 * IF correctly on its own) -- PythonOS/kernel/syscall.py's SYS_READ and
 * SYS_WAIT4 call schedule() (and so this) directly from a blocking
 * loop, with no interrupt frame involved at all. IA32_FMASK clears IF
 * for the entire duration of a syscall (see syscall_entry.cpp), so a
 * thread blocked this way suspends with IF=0 -- and without saving/
 * restoring IF per-thread here, *every* thread resumed via
 * switch_context (not just the blocked one) would inherit whatever
 * IF happened to be live at that moment, since RFLAGS is a single,
 * global CPU register that persists across an ordinary function call.
 * Concretely: the idle thread could end up parked in `hlt` with IF=0,
 * which only NMI/SMI/reset can ever wake -- not the timer tick
 * schedule() itself depends on to run again. Saving/restoring RFLAGS
 * as part of this function's own suspension point, exactly like the
 * six GPRs already are, makes it correct regardless of *how* a thread
 * got here. (A separate, still-open bash crash during post-fork
 * signal-handler setup was investigated alongside this fix; toggling
 * this change on/off didn't cleanly correlate with that crash's
 * timing, so treat this as independently justified rather than as a
 * fix for that specific bug.)
 *
 * A brand-new thread that has never run yet gets a fake stack built to
 * look exactly like it's already parked here (see
 * PythonOS/kernel/threads.py's _init_thread_stack): the "return
 * address" this first `ret` finds is _pyck_thread_trampoline instead,
 * an ordinary (non-naked) C++ function -- the CPU can't tell the
 * difference between "returning" into it this way and a genuine `call`,
 * as long as RSP is left exactly where a `call` would have left it.
 */
#include "pyck_runtime.h"
#include <stdint.h>

/* Defined by PyCk-generated code: a top-level `def thread_entry():` in
 * PyCk source (see PythonOS/kernel/threads.py) is the one function
 * codegen.py emits with C linkage instead of `static`, precisely so
 * this different translation unit can call it directly. Every kernel
 * thread shares this same entry point; it reads a "which thread am I"
 * global itself to decide what to do.
 *
 * thread_switch.cpp is linked into every x86_64/x86_64_graphical build
 * (print42.py, kernel_hello.py, every existing test, ...), essentially
 * none of which define thread_entry() -- same story as idt_stubs.cpp's
 * weak _pyck_on_interrupt fallback: the PyCk-generated one (a strong
 * symbol, whenever the source actually has `def thread_entry`) simply
 * overrides this at link time, and nothing that never creates a kernel
 * thread ever calls this fallback anyway. */
extern "C" __attribute__((weak)) void _pyck_thread_entry() {
    py_fatal_error("unhandled thread (no thread_entry() defined)");
}

extern "C" void _pyck_thread_trampoline() {
    /* A brand-new thread's first-ever activation may happen from
     * *inside* the timer interrupt handler (any thread but the very
     * first one created) -- switch_context()'s `ret` diverts straight
     * here without ever reaching the isr_common `iretq` that would
     * otherwise restore IF=1 for it, so without this, such a thread
     * would run with interrupts permanently disabled and could never be
     * preempted again. Harmless (idempotent) for a thread whose first
     * activation came from ordinary code with IF already 1. */
    asm volatile("sti");
    _pyck_thread_entry();
    /* thread_entry() is an infinite loop; reaching here means it
     * returned, which a kernel thread should never do -- halt rather
     * than run off the end of the stack into whatever garbage follows. */
    for (;;) {
        asm volatile("cli; hlt");
    }
}

extern "C" uint64_t _pyck_thread_trampoline_addr() {
    return (uint64_t)(uintptr_t)_pyck_thread_trampoline;
}

extern "C" void ring3_trampoline();

extern "C" uint64_t _pyck_ring3_trampoline_addr() {
    return (uint64_t)(uintptr_t)ring3_trampoline;
}

/* The one way to change privilege level via something other than
 * syscall/sysret: build a fake interrupt-return frame by hand and
 * `iretq` into it. Used exactly once per process, to launch it into
 * ring3 for the very first time (a syscall's own return uses sysretq
 * instead -- see syscall_entry.cpp -- since by then the process is
 * already running and RCX/R11 already hold its saved RIP/RFLAGS).
 *
 * r12/r13 arrive already loaded with the target user RIP/RSP: this
 * trampoline is reached by switch_context()'s `ret` (see
 * _pyck_thread_trampoline above for why that's a safe way to reach a
 * fresh function), whose own pop sequence restores r12/r13 from
 * whatever threads.py's _init_ring3_thread_stack() put in their slots
 * of the fake stack -- smuggling this trampoline its "arguments"
 * without needing any dedicated calling convention of its own. */
/* 0x23/0x2B: USER_DATA_SEL/USER_CODE_SEL from PythonOS/kernel/gdt.py,
 * hand-copied here since a Python-level constant can't be shared with
 * a hand-written asm literal across languages -- gdt.py's own layout
 * comment explains why they're these specific values (SYSRET's fixed
 * base+8/base+16 formula, which this trampoline has nothing to do with
 * but happens to share a GDT with). */
/* A real Linux kernel zeroes every general-purpose register (RSP
 * excepted) on execve(), specifically so a static binary's _start can
 * trust RDX==0 to mean "no dynamic linker, don't call rtld_fini" --
 * glibc's __libc_start_main takes rtld_fini as its 6th argument (RDX
 * at process entry, per the x86-64 SysV/ELF ABI's handoff convention),
 * and unconditionally __cxa_atexit()s it if nonzero. Skipping the
 * zeroing here leaked whatever this kernel's own code last left in RDX
 * (garbage, since switch_context()/schedule() have no reason to ever
 * clear it) straight into the launched process as a bogus exit-handler
 * function pointer -- invisible until the process's own exit() call
 * finally walked that handler list and jumped into the garbage address,
 * long after everything else had already run correctly. Every register
 * this trampoline doesn't otherwise set (rip/rsp/cs/ss/rflags) gets
 * zeroed here, matching real kernel behavior, not just rdx -- nothing
 * about which other registers _start or __libc_start_main happen to
 * read should be this kernel's problem to individually enumerate. */
asm(
    ".text\n"
    ".global ring3_trampoline\n"
    "ring3_trampoline:\n"
    /* The hand-built frame below is several instructions of raw
     * stack pushes, not a single atomic operation -- if the LAPIC
     * timer fires partway through (this runs with interrupts enabled,
     * reached either at initial process launch or, via
     * _pyck_exec_into_ring3, from an already-preemptible syscall
     * handler), on_interrupt()'s timer branch calls schedule(), which
     * would save *this* mid-construction, not-yet-`iretq`'d %rsp into
     * rsp_table[] as if it were a legitimate suspension point --
     * switch_context()'s `ret`-based resume protocol assumes whatever
     * it saved was itself produced by a prior switch_context() call,
     * which a raw push sequence is not, so resuming into it later
     * jumps to garbage (confirmed by boot: an all-zero-register crash
     * a few bytes past the freshly exec'd entry point, exactly what
     * landing mid-construction looks like). `cli` for the duration
     * closes the window; `iretq` below loads RFLAGS with IF already
     * set, so interrupts come back on by themselves the instant the
     * privilege transition completes -- no matching `sti` needed. */
    "cli\n"
    "pushq $0x23\n"      /* ss  = USER_DATA_SEL */
    "push %r13\n"        /* rsp = user stack top */
    "pushq $0x202\n"     /* rflags: reserved bit1 | IF */
    "pushq $0x2B\n"      /* cs  = USER_CODE_SEL */
    "push %r12\n"        /* rip = elf entry point */
    "xor %eax, %eax\n"
    "xor %ebx, %ebx\n"
    "xor %ecx, %ecx\n"
    "xor %edx, %edx\n"
    "xor %esi, %esi\n"
    "xor %edi, %edi\n"
    "xor %ebp, %ebp\n"
    "xor %r8, %r8\n"
    "xor %r9, %r9\n"
    "xor %r10, %r10\n"
    "xor %r11, %r11\n"
    "xor %r12, %r12\n"
    "xor %r13, %r13\n"
    "xor %r14, %r14\n"
    "xor %r15, %r15\n"
    "iretq\n"
);

/* fork_regs_table: MAX_THREADS (see PythonOS/kernel/threads.py) slots,
 * 128 bytes each, one per thread -- filled in by SYS_FORK (see
 * PythonOS/kernel/syscall.py) with a *copy* of the parent's own syscall
 * register block (see syscall_entry.cpp's own layout comment; the
 * offsets here match exactly, plus one more field this table alone
 * needs: the parent's RSP at syscall time, which an ordinary syscall
 * never has to save anywhere since SYSRET just resumes with whatever
 * RSP already is). Read back exactly once, by fork_child_trampoline
 * below, the first time the new thread is ever scheduled -- there's no
 * other way to hand a *freshly created* thread a full arbitrary
 * register set the way switch_context()'s own 6-register smuggling
 * capacity (see _init_ring3_thread_stack()'s r12/r13 trick) isn't
 * enough for. */
#define FORK_REGS_SLOT_SIZE 128
#define FORK_REGS_MAX_THREADS 8
extern "C" {
uint8_t fork_regs_table[FORK_REGS_MAX_THREADS * FORK_REGS_SLOT_SIZE] __attribute__((aligned(16)));
}

extern "C" uint64_t _pyck_fork_regs_table_addr() {
    return (uint64_t)(uintptr_t)fork_regs_table;
}

extern "C" void fork_child_trampoline();

extern "C" uint64_t _pyck_fork_child_trampoline_addr() {
    return (uint64_t)(uintptr_t)fork_child_trampoline;
}

/* Launches a fork()ed child for the very first time -- same idea as
 * ring3_trampoline above (a hand-built iretq frame is the only way to
 * reach ring3 other than sysretq), but restoring a *complete* register
 * set from fork_regs_table instead of just RIP/RSP, since the child
 * needs to look exactly like the parent's own syscall return (down to
 * every general-purpose register), just with RAX=0 (SYS_FORK already
 * wrote that into the table -- this trampoline doesn't special-case it
 * at all).
 *
 * r12 arrives holding this thread's own tid (smuggled through
 * switch_context()'s pop sequence, via threads.py's
 * _init_fork_child_stack() -- same trick ring3_trampoline's r12/r13
 * use, just one value instead of two, since everything else needed
 * lives in the table already). It's used as the table slot's base
 * address for the entire function, so it can only be overwritten with
 * its own *final* value (the child's real r12) as the very last load,
 * after every other register has already been read out. */
asm(
    ".text\n"
    ".global fork_child_trampoline\n"
    "fork_child_trampoline:\n"
    /* Same hazard, same fix, as ring3_trampoline above: a timer tick
     * partway through this hand-built frame would have schedule()
     * save a not-yet-`iretq`'d %rsp as this thread's resume point,
     * which switch_context()'s `ret`-based protocol can't actually
     * resume correctly later. */
    "cli\n"
    "imul $128, %r12, %r12\n"
    "lea fork_regs_table(%rip), %rax\n"
    "add %rax, %r12\n"
    /* Build the iretq frame first, on whatever stack is currently
     * active (this thread's own dedicated kernel stack -- see
     * threads.py's _init_fork_child_stack()) -- nothing below may
     * touch %rsp again until iretq itself loads it fresh from this
     * frame's own rsp field (offset 120). */
    "pushq $0x23\n"            /* ss  = USER_DATA_SEL */
    "push 120(%r12)\n"         /* rsp (see syscall_entry.cpp: old rsp) */
    "push 56(%r12)\n"          /* rflags (r11 slot) */
    "pushq $0x2B\n"            /* cs  = USER_CODE_SEL */
    "push 64(%r12)\n"          /* rip (rcx slot) */
    "mov 8(%r12), %rdi\n"
    "mov 16(%r12), %rsi\n"
    "mov 24(%r12), %rdx\n"
    "mov 32(%r12), %r10\n"
    "mov 40(%r12), %r8\n"
    "mov 48(%r12), %r9\n"
    "mov 72(%r12), %r15\n"
    "mov 80(%r12), %r14\n"
    "mov 88(%r12), %r13\n"
    "mov 104(%r12), %rbp\n"
    "mov 112(%r12), %rbx\n"
    "mov 0(%r12), %rax\n"
    "mov 96(%r12), %r12\n"     /* must be last: r12 is still in use as the table base until now */
    "iretq\n"
);

/* execve() (see syscall.py's SYS_EXECVE) replaces the *calling*
 * thread's own image -- there is no syscall return to make (the old
 * RCX/user RSP belong to a binary that no longer exists), so instead
 * of returning normally, on_syscall() jumps here as the very last thing
 * it does. This just loads (entry, rsp) into r12/r13 and falls straight
 * into ring3_trampoline -- the exact same "build an iretq frame,
 * zero every other register, launch into ring3" logic a brand-new
 * process already needs, reused as-is (real execve() resets all
 * registers too, so there's nothing execve-specific to add here). Runs
 * on the shared syscall kernel stack (see syscall_entry.cpp) like
 * everything else in a syscall handler -- safe here for the same
 * reason ring3_trampoline's use of *any* transient stack always is:
 * iretq abandons it in favor of the frame's own RSP field before
 * anything could ever need to "return" through it. */
extern "C" void _pyck_exec_into_ring3(unsigned long entry, unsigned long rsp);

asm(
    ".text\n"
    ".global _pyck_exec_into_ring3\n"
    "_pyck_exec_into_ring3:\n"
    "mov %rdi, %r12\n"
    "mov %rsi, %r13\n"
    "jmp ring3_trampoline\n"
);

extern "C" void _pyck_switch_context(unsigned long old_rsp_addr, unsigned long new_rsp);

asm(
    ".text\n"
    ".global _pyck_switch_context\n"
    "_pyck_switch_context:\n"
    "pushf\n"
    "push %rbp\n"
    "push %rbx\n"
    "push %r12\n"
    "push %r13\n"
    "push %r14\n"
    "push %r15\n"
    "mov %rsp, (%rdi)\n"
    "mov %rsi, %rsp\n"
    "pop %r15\n"
    "pop %r14\n"
    "pop %r13\n"
    "pop %r12\n"
    "pop %rbx\n"
    "pop %rbp\n"
    "popf\n"
    "ret\n"
);
