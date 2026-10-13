from satanic import print_satanic, int_to_str

PCI_CONFIG_ADDR = 0xCF8
PCI_CONFIG_DATA = 0xCFC

PCI_VENDOR_ID = 0x00
PCI_DEVICE_ID = 0x02
PCI_COMMAND = 0x04
PCI_CLASS = 0x0B
PCI_SUBCLASS = 0x0A
PCI_PROG_IF = 0x09
PCI_BAR0 = 0x10
PCI_BAR1 = 0x14
PCI_HEADER_TYPE = 0x0E
PCI_INTERRUPT_LINE = 0x3C

PCI_COMMAND_MEMORY = 0x0002
PCI_COMMAND_BUS_MASTER = 0x0004

# Module-level output for pci_find_class
pci_found_bus = 0
pci_found_slot = 0
pci_found_func = 0

def _config_address(bus: int, slot: int, func: int, offset: int):
    return (1 << 31) | (bus << 16) | (slot << 11) | (func << 8) | (offset & 0xFC)

def pci_config_read_long(bus: int, slot: int, func: int, offset: int):
    outl(PCI_CONFIG_ADDR, _config_address(bus, slot, func, offset))
    return inl(PCI_CONFIG_DATA)

def pci_config_write_long(bus: int, slot: int, func: int, offset: int, value: int):
    outl(PCI_CONFIG_ADDR, _config_address(bus, slot, func, offset))
    outl(PCI_CONFIG_DATA, value)

def pci_config_read_word(bus: int, slot: int, func: int, offset: int):
    long_val = pci_config_read_long(bus, slot, func, offset)
    shift = (offset & 2) * 8
    return (long_val >> shift) & 0xFFFF

def pci_config_read_byte(bus: int, slot: int, func: int, offset: int):
    long_val = pci_config_read_long(bus, slot, func, offset)
    shift = (offset & 3) * 8
    return (long_val >> shift) & 0xFF

def pci_get_vendor(bus: int, slot: int, func: int):
    return pci_config_read_word(bus, slot, func, PCI_VENDOR_ID)

def pci_get_device_id(bus: int, slot: int, func: int):
    return pci_config_read_word(bus, slot, func, PCI_DEVICE_ID)

def pci_get_class(bus: int, slot: int, func: int):
    return pci_config_read_byte(bus, slot, func, PCI_CLASS)

def pci_get_subclass(bus: int, slot: int, func: int):
    return pci_config_read_byte(bus, slot, func, PCI_SUBCLASS)

def pci_get_interrupt_line(bus: int, slot: int, func: int):
    return pci_config_read_byte(bus, slot, func, PCI_INTERRUPT_LINE)

def pci_get_bar0(bus: int, slot: int, func: int):
    return pci_config_read_long(bus, slot, func, PCI_BAR0)

def pci_enable_bus_master(bus: int, slot: int, func: int):
    cmd = pci_config_read_word(bus, slot, func, PCI_COMMAND)
    cmd = cmd | PCI_COMMAND_BUS_MASTER
    pci_config_write_long(bus, slot, func, PCI_COMMAND, cmd)

def pci_enable_memory_space(bus: int, slot: int, func: int):
    cmd = pci_config_read_word(bus, slot, func, PCI_COMMAND)
    cmd = cmd | PCI_COMMAND_MEMORY
    pci_config_write_long(bus, slot, func, PCI_COMMAND, cmd)

def pci_has_multi_function(bus: int, slot: int):
    return (pci_config_read_byte(bus, slot, 0, PCI_HEADER_TYPE) & 0x80) != 0

def pci_read_bar(bar_val: int):
    if bar_val == 0:
        return 0
    if (bar_val & 1) != 0:
        return bar_val & 0xFFFFFFFC
    return bar_val & 0xFFFFFFF0

def pci_is_mmio(bar_val: int):
    return (bar_val & 1) == 0

def pci_find_class(class_id: int, subclass_id: int):
    global pci_found_bus, pci_found_slot, pci_found_func
    pci_found_bus = -1
    pci_found_slot = -1
    pci_found_func = -1
    bus = 0
    while bus < 256:
        slot = 0
        while slot < 32:
            v = pci_get_vendor(bus, slot, 0)
            if v != 0xFFFF:
                c = pci_get_class(bus, slot, 0)
                s = pci_get_subclass(bus, slot, 0)
                if c == class_id and s == subclass_id:
                    pci_found_bus = bus
                    pci_found_slot = slot
                    pci_found_func = 0
                    return 1
                if pci_has_multi_function(bus, slot):
                    func = 1
                    while func < 8:
                        v2 = pci_get_vendor(bus, slot, func)
                        if v2 != 0xFFFF:
                            c2 = pci_get_class(bus, slot, func)
                            s2 = pci_get_subclass(bus, slot, func)
                            if c2 == class_id and s2 == subclass_id:
                                pci_found_bus = bus
                                pci_found_slot = slot
                                pci_found_func = func
                                return 1
                        func = func + 1
            slot = slot + 1
        bus = bus + 1
    return 0

def pci_print_device(bus: int, slot: int, func: int):
    v = pci_get_vendor(bus, slot, func)
    d = pci_get_device_id(bus, slot, func)
    c = pci_get_class(bus, slot, func)
    s = pci_get_subclass(bus, slot, func)
    print_satanic("PCI " + int_to_str(bus) + ":" + int_to_str(slot) + "." + int_to_str(func))
    print_satanic("  vendor=" + hex(v) + " device=" + hex(d))
    print_satanic("  class=" + hex(c) + " subclass=" + hex(s))

def pci_scan_all():
    print_satanic("--- PCI device scan ---")
    found = 0
    bus = 0
    while bus < 256:
        slot = 0
        while slot < 32:
            v = pci_get_vendor(bus, slot, 0)
            if v != 0xFFFF:
                pci_print_device(bus, slot, 0)
                found = found + 1
                if pci_has_multi_function(bus, slot):
                    func = 1
                    while func < 8:
                        v2 = pci_get_vendor(bus, slot, func)
                        if v2 != 0xFFFF:
                            pci_print_device(bus, slot, func)
                            found = found + 1
                        func = func + 1
            slot = slot + 1
        bus = bus + 1
    print_satanic("PCI scan complete: " + int_to_str(found) + " devices")
