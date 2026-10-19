from gdt import KERNEL_CODE_SEL
from satanic import print_satanic, int_to_str
from memory import unsigned64
from lapic import lapic_eoi
from threads import schedule, mark_current_exited, wake_expired_polls, take_need_resched
from ps2 import on_keyboard_irq
from xhci import on_xhci_irq

IDT_ENTRIES = 256
SLOT_SOFTWARE = 32
SLOT_TIMER = 33
SLOT_KEYBOARD = 34
SLOT_XHCI = 35
VECTOR_SOFTWARE = 0x80
# The LAPIC timer this kernel arms (see lapic.py's calibrate_and_start_
# timer) is programmed to fire on this vector.
VECTOR_TIMER = 0x40
# The PS/2 keyboard's IRQ1 line, redirected here by the I/O APIC (see
# main.py's ioapic_set_redirection() call).
VECTOR_KEYBOARD = 0x41
# XHCI (USB 3.0) interrupt vector
VECTOR_XHCI = 0x42

# A real module-level global: on_interrupt() below declares `global
# tick_count` to bump it on every timer tick. Unlike LAPIC_VADDR/
# IOAPIC_VADDR this needs no fixed hand-picked address, just a plain
# PyObject* PyCk already keeps at file scope. `global` is what lets a
# function whose signature can't take an extra parameter (it's called
# from hand-written C++, see idt_stubs.cpp) still reach it.
tick_count = 0

# Byte offsets into the register block a fired interrupt hands
REG_R15 = 0
REG_R14 = 8
REG_R13 = 16
REG_R12 = 24
REG_R11 = 32
REG_R10 = 40
REG_R9 = 48
REG_R8 = 56
REG_RBP = 64
REG_RDI = 72
REG_RSI = 80
REG_RDX = 88
REG_RCX = 96
REG_RBX = 104
REG_RAX = 112
REG_VECTOR = 120
REG_ERROR_CODE = 128
REG_RIP = 136
REG_CS = 144
REG_RFLAGS = 152
REG_RSP = 160
REG_SS = 168

def came_from_userspace(regs: int):
    return (peek64(regs + REG_CS) & 3) != 0

class Idt:
    def __init__(self, virtual_allocator):
        self.idt_vaddr = virtual_allocator.alloc(4096)
        self.idtr_vaddr = virtual_allocator.alloc(4096)

        i = 0
        while i < IDT_ENTRIES:
            self._clear_gate(i)
            i = i + 1

        v = 0
        while v < 32:
            self._set_gate(v, v)
            v = v + 1
        self._set_gate(VECTOR_SOFTWARE, SLOT_SOFTWARE)
        self._set_gate(VECTOR_TIMER, SLOT_TIMER)
        self._set_gate(VECTOR_KEYBOARD, SLOT_KEYBOARD)
        self._set_gate(VECTOR_XHCI, SLOT_XHCI)

        poke16(self.idtr_vaddr, IDT_ENTRIES * 16 - 1)
        poke64(self.idtr_vaddr + 2, self.idt_vaddr)

    def _entry_addr(self, vector: int):
        return self.idt_vaddr + vector * 16

    def _clear_gate(self, vector: int):
        entry = self._entry_addr(vector)
        poke64(entry, 0)
        poke64(entry + 8, 0)

    def _set_gate(self, vector: int, stub_slot: int):
        addr = _ISR_STUB(stub_slot)
        entry = self._entry_addr(vector)
        poke16(entry, addr & 0xFFFF)
        poke16(entry + 2, KERNEL_CODE_SEL)
        poke8(entry + 4, 0)
        poke8(entry + 5, 0x8E)
        poke16(entry + 6, (addr >> 16) & 0xFFFF)
        poke32(entry + 8, (addr >> 32) & 0xFFFFFFFF)
        poke32(entry + 12, 0)

    def load(self):
        _ASM1("lidt (%0)", self.idtr_vaddr)

def print_register(name: str, regs: int, offset: int):
    print_satanic(name + hex(unsigned64(peek64(regs + offset))))

def get_tick_count():
    # SYS_GETTIMEOFDAY (see syscall.py) uses this as a coarse,
    # monotonic-ish stand-in for real wall-clock time. There's no RTC
    # read here, only this free-running timer-tick counter.
    return tick_count

def on_interrupt(vector: int, regs: int):
    if vector == VECTOR_TIMER:
        global tick_count
        tick_count = tick_count + 1
        # Unblock any SYS_POLL() callers whose timeout has now elapsed
        # -- must happen before schedule() picks the next thread to
        # run, or a just-expired one wouldn't be eligible yet.
        wake_expired_polls(tick_count)
        lapic_eoi()
        schedule()
    elif vector == VECTOR_KEYBOARD:
        on_keyboard_irq()
        lapic_eoi()
        # A keypress that woke a blocked reader should run it now, not
        # at the next 500ms timer tick (the timer branch above is the
        # only other place that calls schedule() from an interrupt).
        if take_need_resched() != 0:
            schedule()
    elif vector == VECTOR_XHCI:
        on_xhci_irq()
        lapic_eoi()
        # Same for USB HID keyboards: on_xhci_irq() -> on_hid_input() ->
        # tty_input_char() -> wake_all_blocked().
        if take_need_resched() != 0:
            schedule()
    else:
        if vector == VECTOR_SOFTWARE:
            print_satanic("Interrupt: software int 0x80")
        else:
            print_satanic("Interrupt: exception vector " + int_to_str(vector))
            print_satanic("  error code: " + hex(unsigned64(peek64(regs + REG_ERROR_CODE))))
            # Only meaningful for vector 14 (#PF, the faulting address),
            # but harmless to print for anything else (just whatever CR2
            # last held).
            print_satanic("  cr2: " + hex(unsigned64(read_cr2())))
            print_register("  rip: ", regs, REG_RIP)
            print_register("  rax: ", regs, REG_RAX)
            print_register("  rbx: ", regs, REG_RBX)
            print_register("  rcx: ", regs, REG_RCX)
            print_register("  rdx: ", regs, REG_RDX)
            print_register("  rsi: ", regs, REG_RSI)
            print_register("  rdi: ", regs, REG_RDI)
            print_register("  rbp: ", regs, REG_RBP)
            print_register("  r8:  ", regs, REG_R8)
            print_register("  r9:  ", regs, REG_R9)
            print_register("  r10: ", regs, REG_R10)
            print_register("  r11: ", regs, REG_R11)
            print_register("  r12: ", regs, REG_R12)
            print_register("  r13: ", regs, REG_R13)
            print_register("  r14: ", regs, REG_R14)
            print_register("  r15: ", regs, REG_R15)
            if came_from_userspace(regs):
                print_satanic("  (from userspace)")
                print_register("  rsp: ", regs, REG_RSP)
                print_register("  ss:  ", regs, REG_SS)
                # Returning normally here would iretq straight back to the
                # instruction that just faulted; with nothing about the
                # underlying cause fixed, it just faults again forever
                # (this is exactly how the earlier #UD-on-vpxor and
                # #PF-in-memset crashes kept printing the same message on
                # a loop instead of stopping). Kill the process instead,
                # same as an unhandled syscall or a real exit(): one crash
                # report, then it's retired for good.
                #
                # mark_current_exited(), not mark_current_dead(): a
                # crashed process still owes its parent a wait() status
                # the same way a clean exit() does, or a parent already
                # blocked in wait4() never wakes back up (see threads.py's
                # mark_current_exited()).
                mark_current_exited(1)
                schedule()
            else:
                print_satanic("  (from kernel)")
                print_register("  rsp: ", regs, REG_RSP)
                print_register("  ss:  ", regs, REG_SS)

def init_idt(va):
    idt = Idt(va)
    idt.load()
    _ASM("int $0x0\n")
    _ASM("int3\n")
    _ASM("int $0x80\n")
