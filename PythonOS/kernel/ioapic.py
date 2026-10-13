from vmm import PAGE_WRITABLE, PAGE_PCD
from satanic import print_satanic, int_to_str

# Same reasoning as lapic.py's lapic_vaddr: a real VirtualAllocator-
# reserved address, set once by init_ioapic() (which needs `global` to
# reassign it). ioapic_read/write below only ever read it, which needs
# no declaration at all; `global` is only required at an assignment.
ioapic_vaddr = 0

IOAPIC_REGSEL = 0x00
IOAPIC_IOWIN = 0x10

IOAPIC_REG_ID = 0x00
IOAPIC_REG_VERSION = 0x01
IOAPIC_REG_REDTBL_BASE = 0x10

def ioapic_read(reg: int):
    poke32(ioapic_vaddr + IOAPIC_REGSEL, reg)
    return peek32(ioapic_vaddr + IOAPIC_IOWIN)

def ioapic_write(reg: int, value: int):
    poke32(ioapic_vaddr + IOAPIC_REGSEL, reg)
    poke32(ioapic_vaddr + IOAPIC_IOWIN, value)

def init_ioapic(va, ioapic_phys: int):
    global ioapic_vaddr
    ioapic_vaddr = va.reserve(4096)
    va.vm.map(ioapic_vaddr, ioapic_phys, PAGE_WRITABLE | PAGE_PCD, False)

def ioapic_set_redirection(irq: int, vector: int, dest_apic_id: int, level_triggered: bool):
    # Each of the up to 24 redirection entries is 64 bits across two
    # 32-bit registers: low = vector/delivery-mode/polarity/trigger/
    # mask, high = destination APIC ID (bits 24-31). Fixed delivery
    # mode, physical destination, active-high, unmasked -- all zero in
    # every field but the vector and (when requested) the trigger mode.
    #
    # Trigger mode (bit 15: 0=edge, 1=level) actually matters and isn't
    # just a PIC-style default everywhere: a PCI device's INTx line is
    # held asserted until software explicitly acknowledges it (xHCI's
    # IMAN.IP here, cleared by on_xhci_irq()'s _rt_write(0x20, 3)) --
    # that's level semantics. Programming that as edge-triggered only
    # catches the very first 0->1 transition; once the line is already
    # asserted and a *second* event arrives before the first is fully
    # cleared, there's no new rising edge for an edge-triggered entry
    # to catch, so the CPU never hears about it even though the device
    # keeps signaling. This machine's own MADT confirms it for the
    # xHCI's GSI (see acpi_demo()'s printout: "irq=11 gsi=11 flags=
    # 0xd" decodes to active-high, *level*-triggered) -- and matches
    # the actual observed symptom: exactly one interrupt ever serviced,
    # then silence while the xHC keeps completing transfers underneath.
    # IRQ1 (PS/2 keyboard) has no such override and genuinely is a
    # PIC-style edge-triggered line, so it correctly passes False here.
    low_reg = IOAPIC_REG_REDTBL_BASE + irq * 2
    high_reg = low_reg + 1
    ioapic_write(high_reg, (dest_apic_id & 0xFF) << 24)
    low = vector & 0xFF
    if level_triggered:
        low = low | (1 << 15)
    ioapic_write(low_reg, low)

def ioapic_mask(irq: int):
    low_reg = IOAPIC_REG_REDTBL_BASE + irq * 2
    ioapic_write(low_reg, ioapic_read(low_reg) | (1 << 16))

def ioapic_demo():
    id_reg = ioapic_read(IOAPIC_REG_ID)
    version_reg = ioapic_read(IOAPIC_REG_VERSION)
    print_satanic("I/O APIC ID:")
    print_satanic(hex((id_reg >> 24) & 0xF))
    print_satanic("I/O APIC version:")
    print_satanic(hex(version_reg & 0xFF))
    print_satanic("I/O APIC redirection entries:")
    print_satanic(int_to_str(((version_reg >> 16) & 0xFF) + 1))
