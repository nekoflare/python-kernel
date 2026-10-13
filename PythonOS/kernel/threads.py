from memory import PageAllocator
from vmm import VirtualMemory, VirtualAllocator

# Thread 0 is a silent idle task (thread_entry(), below) -- purely a
# fallback the scheduler always has something to switch to, e.g. once
# every other thread is dead/blocked; without it, schedule() would have
# nothing left to pick and would end up resuming a dead thread's last
# (crashed/exited) context instead. Thread 1 runs the loaded ELF app in
# ring3, in its own address space -- see main.py's init_threads() call.
# Threads 2..MAX_THREADS-1 start out unused (not alive) and only ever
# get claimed by fork() (see alloc_thread_slot() and syscall.py's
# SYS_FORK) -- this kernel has no other way to create a new process.
MAX_THREADS = 8
ELF_THREAD_TID = 1
STACK_SIZE = 16384
# Same MSR number as syscall.py's own IA32_FS_BASE constant -- defined
# again here rather than imported, since syscall.py already imports
# from this file and importing back would be circular.
IA32_FS_BASE = 0xC0000100

# rsp_table/alive_table/... each hold one raw 8-byte slot per thread,
# plus (rsp_table only) one extra throwaway slot at index MAX_THREADS
# (see init_threads()). Deliberately plain peek64/poke64 raw memory, not
# PyCk variables: switch_context()'s hand-written asm writes the
# *current* thread's rsp straight into one of rsp_table's slots, and a
# PyCk variable's own address isn't a stable place for that --
# reassigning one rebinds it to a brand-new boxed object rather than
# overwriting it in place.
rsp_table = 0
# alive_table: 1=alive/0=dead. schedule() skips dead slots; a thread
# marks its own dead via mark_current_exited() (see syscall.py's
# SYS_EXIT/SYS_EXIT_GROUP handling) rather than ever being resumed again.
alive_table = 0
# blocked_table: 1=blocked/0=runnable. Separate from alive_table --
# a blocked thread is still alive, just not schedulable right now (see
# syscall.py's SYS_READ blocking on stdin, or SYS_WAIT4 blocking on a
# child). Cleared by whatever event the thread was waiting for, never by
# the blocked thread itself (it's not running).
blocked_table = 0
# cr3_table: the physical PML4 address to load before resuming each
# thread. Thread 0 (idle) always uses the kernel's own (kernel_cr3,
# captured once at boot); every other live thread has its own separate
# address space's CR3 (process_cr3, or a fork()-duplicated one -- see
# process.py's new_address_space()/duplicate_address_space()).
cr3_table = 0
# parent_table: the tid that fork()'d this thread into existence, or 0
# (thread 0/idle can never actually be a real parent, so 0 doubles as
# "no parent" -- the original init process included, same as real
# Linux's pid 1 having no parent of its own). Read by SYS_WAIT4 to find
# a given thread's children, and by mark_current_exited() to decide
# whether exiting should leave a zombie behind for something to reap.
parent_table = 0
# zombie_table: 1 = this thread has exited but its parent hasn't
# SYS_WAIT4'd it yet -- exit_status_table holds what to hand back once
# it does. A zombie's slot stays unavailable to alloc_thread_slot()
# until reap_child() clears it. A thread with no parent (parent_table
# entry 0) never becomes a zombie at all: nothing could ever reap it, so
# its slot is freed immediately instead, same as before fork() existed.
zombie_table = 0
exit_status_table = 0
# poll_deadline_table: 0 = "not waiting on a timed poll()", otherwise
# the tick_count (see idt.py's get_tick_count()) this thread should be
# unblocked at even if nothing else happened. blocked_table's own
# comment is the general rule ("cleared by whatever event the thread
# was waiting for, never by the blocked thread itself") -- for
# SYS_POLL's timeout (syscall.py), that event is the timer tick
# reaching this deadline, checked and cleared by wake_expired_polls()
# below, called from idt.py's own timer interrupt handler right
# alongside tick_count's increment.
poll_deadline_table = 0
# syscall_stack_top_table: the fixed top address of each thread's own
# dedicated kernel stack (stack_base + STACK_SIZE, set once at thread
# creation, never touched again) -- schedule() hands the current
# thread's own entry to _SET_SYSCALL_STACK_TOP() on every switch, so
# arch/x86_64_shared/syscall_entry.cpp's syscall handler always swaps
# onto *this* thread's stack rather than one shared buffer every thread
# used to collide on (see that file's own comment for the bug this
# fixes). Unlike rsp_table (which schedule() overwrites every switch
# with wherever a thread happened to suspend), this table's values
# never change after the thread is created.
syscall_stack_top_table = 0
# fs_base_table: each thread's own IA32_FS_BASE MSR value (the x86-64
# TLS base) -- saved on the way out of schedule() and restored on the
# way back in, the same "whatever it currently is" treatment rsp_table
# gets, not a write-once value like cr3_table. FS_BASE is a single,
# global-to-the-CPU register that nothing but a thread's own
# arch_prctl(ARCH_SET_FS, ...) call (see syscall.py) ever changes, and
# a fork()ed child inherits the exact same TLS *virtual address* as its
# parent (duplicate_address_space() copies it byte-for-byte) -- so
# going without a per-thread save/restore here was invisible for this
# entire kernel's lifetime up to the point a forked child's own
# execve() first gave it a genuinely *different* TLS address than its
# parent's. From then on the raw MSR silently stayed pointed at
# whichever thread had called arch_prctl most recently, so the next
# thread to dereference %fs (any TLS access -- confirmed by boot: the
# parent shell's own next write() call faulted reading its own TCB,
# because %fs was still the exited child's TLS address, not its own).
fs_base_table = 0
current_tid = 0
# Idle loop callback: set by main.py to poll USB HID while idle
_idle_callback = 0

# _va: the SAME VirtualAllocator main.py already built (see
# init_threads()) -- needed again later, whenever fork() (see
# syscall.py's SYS_CLONE) allocates a brand-new thread's own kernel
# stack, which init_threads() itself only had a *local* reference to.
# Real instance, not a placeholder int: PyCk requires one consistent
# type across every assignment to a given global, including a later
# `global x; x = ...` reassignment inside a function -- so, same trick
# as syscall.py's brk_pa/brk_vm, this throwaway VirtualAllocator gets
# fully replaced in init_threads() before anything could ever read it
# for real.
_alloc_pa = PageAllocator()
_alloc_vm = VirtualMemory(_alloc_pa)
_va = VirtualAllocator(_alloc_pa, _alloc_vm)

def _init_thread_stack(stack_base: int, entry_addr: int):
    # stack_base + STACK_SIZE is page-aligned (so 16-aligned too), but
    # the SysV ABI expects RSP == 8 (mod 16) at a function's *entry*
    # (right after a `call` pushes its return address onto a
    # previously-16-aligned stack). Starting the fake frame 8 bytes
    # below that top is what makes entry_addr's own RSP land on that
    # same footing once switch_context() first `ret`s into it, even
    # though nothing ever actually `call`ed it. 8 pushed values now
    # (was 7): switch_context() saves/restores RFLAGS as well as the
    # six GPRs (see its own comment on why), so this fake frame needs a
    # matching fake-RFLAGS slot in the right position or its pop/popf
    # sequence reads everything after it one slot off.
    sp = stack_base + STACK_SIZE - 8
    # Push order has to match switch_context()'s own save order
    # (rflags, rbp, rbx, r12, r13, r14, r15) so its pop/popf sequence
    # reads these back in the right places -- the actual values don't
    # matter (0x202 = IF set, same as every other fake initial RFLAGS
    # this kernel hands a fresh context -- see ring3_trampoline's own),
    # a fresh thread has no prior register state to restore.
    sp = sp - 8
    poke64(sp, entry_addr)
    sp = sp - 8
    poke64(sp, 0x202)
    sp = sp - 8
    poke64(sp, 0)
    sp = sp - 8
    poke64(sp, 0)
    sp = sp - 8
    poke64(sp, 0)
    sp = sp - 8
    poke64(sp, 0)
    sp = sp - 8
    poke64(sp, 0)
    sp = sp - 8
    poke64(sp, 0)
    return sp

def _init_ring3_thread_stack(stack_base: int, elf_entry: int, user_rsp: int):
    # Same fake-frame shape as _init_thread_stack(), except the "return
    # address" is ring3_trampoline instead of _pyck_thread_trampoline,
    # and the r12/r13 slots (normally just zeroed placeholders) carry
    # elf_entry/user_rsp instead -- switch_context()'s own pop sequence
    # loads them into the real r12/r13 registers right before `ret`ing
    # into the trampoline, which reads them from there (see
    # arch/x86_64_shared/thread_switch.cpp's ring3_trampoline). There's
    # no other way to pass this trampoline "arguments": it's reached by
    # a bare `ret`, not a call with a defined calling convention.
    trampoline = _RING3_TRAMPOLINE_ADDR()
    sp = stack_base + STACK_SIZE - 8
    sp = sp - 8
    poke64(sp, trampoline)
    sp = sp - 8
    poke64(sp, 0x202)       # rflags -- see _init_thread_stack()'s comment
    sp = sp - 8
    poke64(sp, 0)           # rbp
    sp = sp - 8
    poke64(sp, 0)           # rbx
    sp = sp - 8
    poke64(sp, elf_entry)   # r12 -- read by ring3_trampoline
    sp = sp - 8
    poke64(sp, user_rsp)    # r13 -- read by ring3_trampoline
    sp = sp - 8
    poke64(sp, 0)           # r14
    sp = sp - 8
    poke64(sp, 0)           # r15
    return sp

def _init_fork_child_stack(stack_base: int, child_tid: int):
    # Same fake-frame shape again, landing on fork_child_trampoline this
    # time -- it only needs *one* smuggled value (which tid it is, to
    # find its own saved registers in fork_regs_table), unlike
    # ring3_trampoline's two, since SYS_FORK (see syscall.py) already
    # filled that whole table slot in before this thread is ever
    # scheduled. See arch/x86_64_shared/thread_switch.cpp.
    trampoline = _FORK_CHILD_TRAMPOLINE_ADDR()
    sp = stack_base + STACK_SIZE - 8
    sp = sp - 8
    poke64(sp, trampoline)
    sp = sp - 8
    poke64(sp, 0x202)       # rflags -- see _init_thread_stack()'s comment
    sp = sp - 8
    poke64(sp, 0)           # rbp
    sp = sp - 8
    poke64(sp, 0)           # rbx
    sp = sp - 8
    poke64(sp, child_tid)   # r12 -- read by fork_child_trampoline
    sp = sp - 8
    poke64(sp, 0)           # r13
    sp = sp - 8
    poke64(sp, 0)           # r14
    sp = sp - 8
    poke64(sp, 0)           # r15
    return sp

def init_threads(va, kernel_cr3: int, elf_entry: int, user_rsp: int, process_cr3: int):
    global rsp_table
    global alive_table
    global blocked_table
    global cr3_table
    global parent_table
    global zombie_table
    global exit_status_table
    global syscall_stack_top_table
    global fs_base_table
    global poll_deadline_table
    global current_tid
    global _va
    _va = va
    rsp_table = va.alloc(4096)
    alive_table = va.alloc(4096)
    blocked_table = va.alloc(4096)
    cr3_table = va.alloc(4096)
    parent_table = va.alloc(4096)
    zombie_table = va.alloc(4096)
    exit_status_table = va.alloc(4096)
    syscall_stack_top_table = va.alloc(4096)
    fs_base_table = va.alloc(4096)
    poll_deadline_table = va.alloc(4096)
    i = 0
    while i < MAX_THREADS:
        poke64(parent_table + i * 8, 0)
        poke64(zombie_table + i * 8, 0)
        poke64(exit_status_table + i * 8, 0)
        poke64(fs_base_table + i * 8, 0)
        poke64(poll_deadline_table + i * 8, 0)
        if i < 2:
            stack_base = va.alloc(STACK_SIZE)
            poke64(syscall_stack_top_table + i * 8, stack_base + STACK_SIZE)
            if i == ELF_THREAD_TID and elf_entry != 0:
                initial_rsp = _init_ring3_thread_stack(stack_base, elf_entry, user_rsp)
                poke64(cr3_table + i * 8, process_cr3)
            else:
                initial_rsp = _init_thread_stack(stack_base, _THREAD_TRAMPOLINE_ADDR())
                poke64(cr3_table + i * 8, kernel_cr3)
            poke64(rsp_table + i * 8, initial_rsp)
            poke64(alive_table + i * 8, 1)
        else:
            # Unclaimed slot -- alloc_thread_slot() (see SYS_FORK) is
            # the only thing that ever brings one of these to life.
            poke64(rsp_table + i * 8, 0)
            poke64(alive_table + i * 8, 0)
        poke64(blocked_table + i * 8, 0)
        i = i + 1
    current_tid = 0
    # This call never returns to its caller: it switches away from
    # main()'s own stack (saved into a slot nothing ever reads back) and
    # into thread 0, and every switch after this one happens only
    # between currently-alive threads, driven by the timer interrupt's
    # schedule() below. CR3 is already kernel_cr3 (thread 0's own) at
    # this point, so no write_cr3() is needed for this first switch.
    discard_slot = rsp_table + MAX_THREADS * 8
    switch_context(discard_slot, peek64(rsp_table + 0))

def alloc_thread_slot():
    # Returns the first tid (>=1; 0/idle is permanent, never reused)
    # that's neither alive nor a pending zombie, or -1 if every slot is
    # taken -- see syscall.py's SYS_FORK, the only caller.
    i = 1
    while i < MAX_THREADS:
        if peek64(alive_table + i * 8) == 0 and peek64(zombie_table + i * 8) == 0:
            return i
        i = i + 1
    return -1

def setup_fork_child(child_tid: int, child_cr3: int, parent_tid: int, parent_fs_base: int):
    # Finishes what SYS_CLONE (see syscall.py) starts: allocates the
    # new thread's own dedicated kernel stack (for switch_context()'s
    # ordinary bookkeeping -- separate from fork_regs_table, which is
    # what fork_child_trampoline actually restores its *user-visible*
    # registers from) and fills in every table entry needed for
    # schedule() to treat it as a real, runnable thread from here on.
    #
    # parent_fs_base seeds fs_base_table[child_tid] (see that table's
    # own comment): a forked child starts out at the exact same TLS
    # *virtual address* as its parent (duplicate_address_space() copies
    # the whole address space byte-for-byte), so it needs that same
    # FS_BASE value from its very first instruction, before it's ever
    # had a chance to call arch_prctl() itself -- leaving this at the
    # table's default of 0 pointed %fs at address 0x10 instead of the
    # real TCB, faulting on the child's own first TLS access inside
    # _Fork() (confirmed by boot).
    stack_base = _va.alloc(STACK_SIZE)
    poke64(fs_base_table + child_tid * 8, parent_fs_base)
    initial_rsp = _init_fork_child_stack(stack_base, child_tid)
    poke64(rsp_table + child_tid * 8, initial_rsp)
    poke64(syscall_stack_top_table + child_tid * 8, stack_base + STACK_SIZE)
    poke64(cr3_table + child_tid * 8, child_cr3)
    poke64(alive_table + child_tid * 8, 1)
    poke64(blocked_table + child_tid * 8, 0)
    poke64(poll_deadline_table + child_tid * 8, 0)
    poke64(parent_table + child_tid * 8, parent_tid)

def get_current_tid():
    return current_tid

def get_parent(tid: int):
    return peek64(parent_table + tid * 8)

def is_zombie(tid: int):
    return peek64(zombie_table + tid * 8) != 0

def set_current_cr3(cr3: int):
    # SYS_EXECVE (see syscall.py) replacing the calling thread's own
    # image needs this: the thread doesn't change identity, only which
    # address space it runs in from now on.
    poke64(cr3_table + current_tid * 8, cr3)

def schedule():
    global current_tid
    if alive_table == 0:
        return
    old_tid = current_tid
    next_tid = old_tid
    tries = 0
    found = 0
    while tries < MAX_THREADS and found == 0:
        next_tid = next_tid + 1
        if next_tid >= MAX_THREADS:
            next_tid = 0
        if peek64(alive_table + next_tid * 8) != 0 and peek64(blocked_table + next_tid * 8) == 0:
            found = 1
        tries = tries + 1
    # Save the outgoing thread's current TLS base before doing
    # anything else -- see fs_base_table's own comment above.
    poke64(fs_base_table + old_tid * 8, rdmsr(IA32_FS_BASE))
    current_tid = next_tid
    old_slot = rsp_table + old_tid * 8
    new_rsp = peek64(rsp_table + next_tid * 8)
    # Loading CR3 is safe *before* the stack switch below: the kernel
    # code/stack currently executing (this function, on-interrupt,
    # isr_common, ...) all live in the shared upper-half range every
    # process's PML4 copies verbatim (see process.py's
    # new_address_space()), so it stays mapped identically regardless
    # of which PML4 is active -- only once switch_context() actually
    # `ret`s into the next thread's own low-address code does the new
    # CR3's *other* (process-specific) half start to matter.
    write_cr3(peek64(cr3_table + next_tid * 8))
    # Same idea, for the *next* syscall this thread makes rather than
    # for its userspace memory: point syscall_entry.cpp's kernel-stack
    # swap at this thread's own dedicated stack (see
    # syscall_stack_top_table's own comment above).
    _SET_SYSCALL_STACK_TOP(peek64(syscall_stack_top_table + next_tid * 8))
    # Restore the incoming thread's own TLS base (see fs_base_table's
    # comment above) -- 0 for a thread that's never called arch_prctl
    # yet, harmless since nothing dereferences %fs before it does.
    wrmsr(IA32_FS_BASE, peek64(fs_base_table + next_tid * 8))
    switch_context(old_slot, new_rsp)

def mark_current_dead():
    # Kept for anything that really does just want "stop scheduling
    # this thread, no zombie bookkeeping" (e.g. idt.py's userspace
    # crash-report path) -- mark_current_exited() below is what
    # SYS_EXIT/SYS_EXIT_GROUP actually use, since a crashed process
    # still owes its parent a wait() status same as a clean exit does.
    poke64(alive_table + current_tid * 8, 0)

def mark_current_exited(status: int):
    poke64(alive_table + current_tid * 8, 0)
    poke64(exit_status_table + current_tid * 8, status)
    parent = peek64(parent_table + current_tid * 8)
    if parent != 0:
        poke64(zombie_table + current_tid * 8, 1)
        # The parent may already be sitting in SYS_WAIT4's own blocking
        # loop (see syscall.py) -- without this, it would never get
        # scheduled again to notice this zombie exists.
        poke64(blocked_table + parent * 8, 0)

def find_zombie_child(parent_tid: int):
    # Returns the tid of the first zombie child of parent_tid, or -1 if
    # it has none waiting to be reaped yet -- see syscall.py's
    # SYS_WAIT4.
    i = 1
    while i < MAX_THREADS:
        if peek64(parent_table + i * 8) == parent_tid and peek64(zombie_table + i * 8) != 0:
            return i
        i = i + 1
    return -1

def has_any_child(parent_tid: int):
    # A living *or* zombie child both count -- SYS_WAIT4 needs this to
    # tell "no children exist at all" (real waitpid() fails with
    # ECHILD) apart from "children exist but none have exited yet"
    # (real waitpid() blocks).
    i = 1
    while i < MAX_THREADS:
        if peek64(parent_table + i * 8) == parent_tid:
            if peek64(alive_table + i * 8) != 0 or peek64(zombie_table + i * 8) != 0:
                return True
        i = i + 1
    return False

def reap_child(tid: int):
    # Frees a zombie's slot for alloc_thread_slot() to reuse and hands
    # back the exit status SYS_WAIT4's caller asked for.
    status = peek64(exit_status_table + tid * 8)
    poke64(zombie_table + tid * 8, 0)
    poke64(parent_table + tid * 8, 0)
    return status

def mark_current_blocked():
    # Called by the thread on *itself*, right before it calls schedule()
    # to actually yield the CPU (see syscall.py's SYS_READ/SYS_WAIT4) --
    # setting the flag alone doesn't stop this thread from running; it
    # only stops schedule() from picking it again once something else is.
    poke64(blocked_table + current_tid * 8, 1)

def mark_thread_unblocked(tid: int):
    # Called by whatever event a blocked thread was waiting for (e.g.
    # ps2.py's keyboard ISR once a full line is ready, or SYS_EXIT
    # once a waited-on child exits) -- from a *different* thread's
    # context (an interrupt handler, or the exiting child itself, has
    # no "current thread" matching the blocked one), so this takes the
    # target tid explicitly rather than acting on current_tid.
    poke64(blocked_table + tid * 8, 0)

def wake_all_blocked():
    # Same idea as mark_thread_unblocked(), but for an event (new
    # keyboard input -- see ps2.py's tty_input_char()) that doesn't
    # know *which* tid is actually waiting on it. Confirmed via boot:
    # the keyboard ISR used to hardcode mark_thread_unblocked(
    # ELF_THREAD_TID) (tid 1), which is only correct when the process
    # reading stdin still *is* tid 1 -- true for the shell itself, but
    # not for anything the shell fork()s to run (busybox vi included),
    # which lands in whatever tid alloc_thread_slot() handed out. That
    # woke the wrong (or no) thread the instant a forked child was the
    # one actually blocked in SYS_READ/SYS_POLL, so its own keystrokes
    # never reached it -- every key just sat in stdin_buf forever.
    # Waking every blocked thread instead is safe: one that wakes for
    # no real reason of its own just finds its wait condition still
    # unmet and re-blocks itself (see SYS_READ/SYS_POLL's own retry
    # loops), the same tolerance a spurious wakeup already requires.
    i = 0
    while i < MAX_THREADS:
        if peek64(alive_table + i * 8) != 0:
            poke64(blocked_table + i * 8, 0)
        i = i + 1

def set_current_poll_deadline(tick: int):
    # SYS_POLL (see syscall.py) calls this on itself right before
    # blocking, when it has a finite timeout left to wait out --
    # cleared again (via clear_current_poll_deadline()) on every path
    # back out of that wait, same as blocked_table itself.
    poke64(poll_deadline_table + current_tid * 8, tick)

def clear_current_poll_deadline():
    poke64(poll_deadline_table + current_tid * 8, 0)

def wake_expired_polls(now_tick: int):
    # Called from idt.py's timer interrupt handler on *every* tick,
    # right alongside tick_count's own increment -- the timer is the
    # only thing that can know a SYS_POLL timeout has elapsed, the same
    # way the keyboard ISR is the only thing that knows stdin has a
    # line ready. A thread with no deadline set (0) is either not
    # polling at all or polling with an infinite timeout, either way
    # nothing to do here.
    #
    # Same guard as schedule()'s own "if alive_table == 0: return"
    # above, and for the same reason: main.py starts the LAPIC timer
    # (calibrate_and_start_timer()) well before it ever calls
    # init_threads() (PCI/xHCI enumeration alone takes several ticks),
    # so this runs on every tick during that whole window with
    # poll_deadline_table still at its unallocated default of 0 --
    # without this check, peek64(0 + i*8) faulted on the very first
    # tick, crashing partway through xHCI enumeration.
    if poll_deadline_table == 0:
        return
    i = 0
    while i < MAX_THREADS:
        deadline = peek64(poll_deadline_table + i * 8)
        if deadline != 0 and now_tick >= deadline:
            poke64(poll_deadline_table + i * 8, 0)
            mark_thread_unblocked(i)
        i = i + 1


def thread_entry():
    while True:
        _ASM("hlt\n")
