from memory import limine_response, unsigned64, hhdm_offset
from satanic import print_satanic, int_to_str

RSDP_MAGIC_A = 0xc5e77b6b397e7b43
RSDP_MAGIC_B = 0x27637845accdcf3c

def read_signature(addr: int, length: int):
    s = ""
    i = 0
    while i < length:
        s = s + chr(peek8(addr + i))
        i = i + 1
    return s

def find_rsdp():
    response = limine_response(RSDP_MAGIC_A, RSDP_MAGIC_B)
    return unsigned64(peek64(response + 8))

class Rsdp:
    def __init__(self, addr: int):
        self.addr = addr
        self.revision = peek8(addr + 15)
        self.rsdt_phys = peek32(addr + 16)
        if self.revision >= 2:
            self.xsdt_phys = unsigned64(peek64(addr + 24))
        else:
            self.xsdt_phys = 0

def find_table(sdt_addr: int, entry_size: int, hhdm: int, signature: str):
    length = peek32(sdt_addr + 4)
    count = (length - 36) // entry_size
    i = 0
    while i < count:
        if entry_size == 4:
            phys = peek32(sdt_addr + 36 + i * 4)
        else:
            phys = unsigned64(peek64(sdt_addr + 36 + i * 8))
        candidate = phys + hhdm
        if read_signature(candidate, 4) == signature:
            return candidate
        i = i + 1
    return 0

def print_madt_entry(entry_addr: int, entry_type: int):
    if entry_type == 0:
        proc_id = peek8(entry_addr + 2)
        apic_id = peek8(entry_addr + 3)
        enabled = peek32(entry_addr + 4) & 1
        print_satanic("  Local APIC: proc=" + int_to_str(proc_id) +
                       " apic_id=" + int_to_str(apic_id) +
                       " enabled=" + int_to_str(enabled))
    else:
        if entry_type == 1:
            ioapic_id = peek8(entry_addr + 2)
            ioapic_addr = peek32(entry_addr + 4)
            gsi_base = peek32(entry_addr + 8)
            print_satanic("  I/O APIC: id=" + int_to_str(ioapic_id) +
                           " addr=" + hex(ioapic_addr) +
                           " gsi_base=" + int_to_str(gsi_base))
        else:
            if entry_type == 2:
                bus = peek8(entry_addr + 2)
                irq = peek8(entry_addr + 3)
                gsi = peek32(entry_addr + 4)
                flags = peek16(entry_addr + 8)
                print_satanic("  Interrupt Source Override: bus=" + int_to_str(bus) +
                               " irq=" + int_to_str(irq) +
                               " gsi=" + int_to_str(gsi) +
                               " flags=" + hex(flags))
            else:
                if entry_type == 4:
                    proc_id = peek8(entry_addr + 2)
                    lint = peek8(entry_addr + 5)
                    print_satanic("  Local APIC NMI: proc=" + int_to_str(proc_id) +
                                   " lint=" + int_to_str(lint))
                else:
                    if entry_type == 5:
                        addr64 = unsigned64(peek64(entry_addr + 4))
                        print_satanic("  Local APIC Address Override: addr=" + hex(addr64))
                    else:
                        if entry_type == 9:
                            x2apic_id = peek32(entry_addr + 4)
                            enabled = peek32(entry_addr + 8) & 1
                            acpi_id = peek32(entry_addr + 12)
                            print_satanic("  Local x2APIC: id=" + hex(x2apic_id) +
                                           " enabled=" + int_to_str(enabled) +
                                           " acpi_id=" + hex(acpi_id))
                        else:
                            print_satanic("  Unknown MADT entry type " + int_to_str(entry_type))

def print_madt(madt_addr: int):
    length = peek32(madt_addr + 4)
    print_satanic("MADT local APIC address:")
    print_satanic(hex(peek32(madt_addr + 36)))
    print_satanic("MADT flags:")
    print_satanic(hex(peek32(madt_addr + 40)))
    offset = 44
    while offset < length:
        entry_addr = madt_addr + offset
        entry_type = peek8(entry_addr)
        entry_len = peek8(entry_addr + 1)
        if entry_len == 0:
            offset = length
        else:
            print_madt_entry(entry_addr, entry_type)
            offset = offset + entry_len

def find_madt():
    hhdm = unsigned64(hhdm_offset())
    rsdp = Rsdp(find_rsdp())
    if rsdp.revision >= 2:
        sdt_addr = rsdp.xsdt_phys + hhdm
        entry_size = 8
    else:
        sdt_addr = rsdp.rsdt_phys + hhdm
        entry_size = 4
    return find_table(sdt_addr, entry_size, hhdm, "APIC")

def get_lapic_phys(madt_addr: int):
    return peek32(madt_addr + 36)

def get_first_ioapic_phys(madt_addr: int):
    length = peek32(madt_addr + 4)
    offset = 44
    while offset < length:
        entry_addr = madt_addr + offset
        entry_type = peek8(entry_addr)
        entry_len = peek8(entry_addr + 1)
        if entry_len == 0:
            return 0
        if entry_type == 1:
            return peek32(entry_addr + 4)
        offset = offset + entry_len
    return 0

def acpi_demo():
    rsdp_addr = find_rsdp()
    print_satanic("RSDP signature:")
    print_satanic(read_signature(rsdp_addr, 8))

    rsdp = Rsdp(rsdp_addr)
    print_satanic("ACPI revision:")
    print_satanic(int_to_str(rsdp.revision))

    madt_addr = find_madt()
    if madt_addr == 0:
        print_satanic("MADT not found")
    else:
        print_satanic("MADT found at:")
        print_satanic(hex(madt_addr))
        print_madt(madt_addr)
    return madt_addr
