# The 8253/8254 Programmable Interval Timer -- port I/O only, no memory
# mapping needed. Used here purely as a known-good time reference to
# calibrate the LAPIC timer against (see lapic.py): the LAPIC's own
# tick rate depends on the CPU's bus frequency, which isn't known ahead
# of time, but the PIT always runs at this fixed, documented frequency
# regardless of CPU.
PIT_CHANNEL0 = 0x40
PIT_COMMAND = 0x43
PIT_FREQUENCY = 1193182

def pit_set_one_shot(count: int):
    # Channel 0, lobyte/hibyte access, mode 0 (interrupt on terminal
    # count -- but nothing here waits for that interrupt; the count is
    # just read back directly, see pit_read_count()), binary (not BCD).
    outb(PIT_COMMAND, 0x30)
    outb(PIT_CHANNEL0, count & 0xFF)
    outb(PIT_CHANNEL0, (count >> 8) & 0xFF)

def pit_read_count():
    # The "counter latch" command (0x00) freezes channel 0's current
    # count so the two-byte read below can't race the counter changing
    # mid-read; it doesn't affect the counter's actual countdown.
    outb(PIT_COMMAND, 0x00)
    lo = inb(PIT_CHANNEL0)
    hi = inb(PIT_CHANNEL0)
    return (hi << 8) | lo
