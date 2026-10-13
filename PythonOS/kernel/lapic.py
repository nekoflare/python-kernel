from vmm import PAGE_WRITABLE, PAGE_PCD
from pit import PIT_FREQUENCY, pit_set_one_shot, pit_read_count
from satanic import print_satanic, int_to_str

# A real VirtualAllocator-reserved address, set once by init_lapic().
# `global` is what lets on_interrupt() (see idt.py), called from
# hand-written C++ with a fixed signature with no room for an extra
# parameter, still reach it via this module-level name. 0 means
# "not yet initialized".
lapic_vaddr = 0

LAPIC_REG_ID = 0x20
LAPIC_REG_VERSION = 0x30
LAPIC_REG_EOI = 0xB0
LAPIC_REG_SPURIOUS = 0xF0
LAPIC_REG_LVT_TIMER = 0x320
LAPIC_REG_TIMER_INITIAL_COUNT = 0x380
LAPIC_REG_TIMER_CURRENT_COUNT = 0x390
LAPIC_REG_TIMER_DIVIDE = 0x3E0

LAPIC_DIVIDE_16 = 0x3
LAPIC_TIMER_MASKED = 1 << 16
LAPIC_TIMER_PERIODIC = 1 << 17

def lapic_read(reg: int):
    return peek32(lapic_vaddr + reg)

def lapic_write(reg: int, value: int):
    poke32(lapic_vaddr + reg, value)

def lapic_eoi():
    lapic_write(LAPIC_REG_EOI, 0)

def lapic_id():
    return (lapic_read(LAPIC_REG_ID) >> 24) & 0xFF

def init_lapic(va, lapic_phys: int):
    global lapic_vaddr
    # reserve(), not alloc(): the LAPIC's address is fixed by the
    # hardware (found via the MADT), so this only needs virtual address
    # space to map it at. alloc()'s "also get me a new physical frame"
    # would hand back unrelated RAM instead.
    lapic_vaddr = va.reserve(4096)
    va.vm.map(lapic_vaddr, lapic_phys, PAGE_WRITABLE | PAGE_PCD, False)
    lapic_write(LAPIC_REG_SPURIOUS, lapic_read(LAPIC_REG_SPURIOUS) | 0x1FF)
    print_satanic("LAPIC ID:")
    print_satanic(hex((lapic_read(LAPIC_REG_ID) >> 24) & 0xFF))
    print_satanic("LAPIC version:")
    print_satanic(hex(lapic_read(LAPIC_REG_VERSION) & 0xFF))

def calibrate_and_start_timer(vector: int, period_ms: int):
    # Classic PIT-calibration trick: let the LAPIC timer free-run from its
    # maximum count during a known interval (measured via the PIT, whose
    # frequency is fixed regardless of CPU), then see how far the LAPIC's
    # count fell. That gives real LAPIC-ticks-per-millisecond for this
    # CPU without knowing its bus frequency ahead of time.
    lapic_write(LAPIC_REG_TIMER_DIVIDE, LAPIC_DIVIDE_16)
    lapic_write(LAPIC_REG_LVT_TIMER, LAPIC_TIMER_MASKED)
    lapic_write(LAPIC_REG_TIMER_INITIAL_COUNT, 0xFFFFFFFF)

    calibration_ms = 10
    pit_ticks_wanted = PIT_FREQUENCY * calibration_ms // 1000
    pit_set_one_shot(0xFFFF)
    target = 0xFFFF - pit_ticks_wanted
    while pit_read_count() > target:
        pass

    elapsed = 0xFFFFFFFF - lapic_read(LAPIC_REG_TIMER_CURRENT_COUNT)
    ticks_per_ms = elapsed // calibration_ms

    print_satanic("LAPIC timer calibrated (ticks/ms):")
    print_satanic(int_to_str(ticks_per_ms))

    lapic_write(LAPIC_REG_LVT_TIMER, LAPIC_TIMER_MASKED)
    lapic_write(LAPIC_REG_TIMER_INITIAL_COUNT, ticks_per_ms * period_ms)
    lapic_write(LAPIC_REG_LVT_TIMER, vector | LAPIC_TIMER_PERIODIC)
    # Nothing fires until interrupts are actually turned on: the timer is
    # maskable, and IF has been off this whole boot.
    _ASM("sti\n")
    return ticks_per_ms
