from satanic import print_satanic, int_to_str
from cpu import CPU
from memory import PageAllocator, unsigned64
from vmm import VirtualMemory, VirtualAllocator
from gdt import init_gdt
from idt import init_idt, VECTOR_TIMER, VECTOR_KEYBOARD, VECTOR_XHCI
from acpi import acpi_demo, get_lapic_phys, get_first_ioapic_phys
from lapic import init_lapic, calibrate_and_start_timer, lapic_id
from ioapic import init_ioapic, ioapic_demo, ioapic_set_redirection
from graphics import pentagram_demo, crosses_demo
from cpio import cpio_demo, find_module_file
from elf import load_elf
from process import new_address_space, setup_user_stack, _char_to_ascii
from syscall import init_syscalls, init_brk, init_kernel_vm
from threads import init_threads, ELF_THREAD_TID
from ps2 import init_ps2
from pci import (pci_scan_all, pci_find_class, pci_get_interrupt_line,
                 pci_found_bus, pci_found_slot, pci_found_func)
from xhci import init_xhci, enumerate_usb_devices
from usb_hid import init_usb_hid

def self_test():
    print_satanic("--- self tests ---")
    passed = 0
    failed = 0

    # Test 1: page allocator free/reuse
    pa = PageAllocator()
    p1 = pa.alloc_page()
    p2 = pa.alloc_page()
    poke64(p1, 0xAAAA)
    poke64(p2, 0xBBBB)
    pa.free_page(p1)
    p3 = pa.alloc_page()
    if p3 == p1:
        passed = passed + 1
        print_satanic("TEST PASS: page free/reuse")
    else:
        failed = failed + 1
        print_satanic("TEST FAIL: page free/reuse (p3 != p1)")
    # Freed page should be zeroed
    if peek64(p3) == 0:
        passed = passed + 1
        print_satanic("TEST PASS: freed page zeroed on reuse")
    else:
        failed = failed + 1
        print_satanic("TEST FAIL: freed page not zeroed")
    # p2 should be untouched
    if peek64(p2) == 0xBBBB:
        passed = passed + 1
        print_satanic("TEST PASS: unrelated page untouched")
    else:
        failed = failed + 1
        print_satanic("TEST FAIL: unrelated page corrupted")

    # Test 2: _char_to_ascii uppercase
    if _char_to_ascii("A") == 65:
        passed = passed + 1
        print_satanic("TEST PASS: char_to_ascii('A')=65")
    else:
        failed = failed + 1
        print_satanic("TEST FAIL: char_to_ascii('A')!=" + int_to_str(_char_to_ascii("A")))
    if _char_to_ascii("Z") == 90:
        passed = passed + 1
        print_satanic("TEST PASS: char_to_ascii('Z')=90")
    else:
        failed = failed + 1
        print_satanic("TEST FAIL: char_to_ascii('Z')!=" + int_to_str(_char_to_ascii("Z")))
    if _char_to_ascii("a") == 97:
        passed = passed + 1
        print_satanic("TEST PASS: char_to_ascii('a')=97")
    else:
        failed = failed + 1
        print_satanic("TEST FAIL: char_to_ascii('a')!=" + int_to_str(_char_to_ascii("a")))
    if _char_to_ascii("0") == 48:
        passed = passed + 1
        print_satanic("TEST PASS: char_to_ascii('0')=48")
    else:
        failed = failed + 1
        print_satanic("TEST FAIL: char_to_ascii('0')!=" + int_to_str(_char_to_ascii("0")))
    if _char_to_ascii(" ") == 32:
        passed = passed + 1
        print_satanic("TEST PASS: char_to_ascii(' ')=32")
    else:
        failed = failed + 1
        print_satanic("TEST FAIL: char_to_ascii(' ')!=" + int_to_str(_char_to_ascii(" ")))
    if _char_to_ascii(":") == 58:
        passed = passed + 1
        print_satanic("TEST PASS: char_to_ascii(':')=58")
    else:
        failed = failed + 1
        print_satanic("TEST FAIL: char_to_ascii(':')!=" + int_to_str(_char_to_ascii(":")))

    # Test 3: unsigned64
    if unsigned64(-1) == 0xFFFFFFFFFFFFFFFF:
        passed = passed + 1
        print_satanic("TEST PASS: unsigned64(-1)")
    else:
        failed = failed + 1
        print_satanic("TEST FAIL: unsigned64(-1)")
    if unsigned64(42) == 42:
        passed = passed + 1
        print_satanic("TEST PASS: unsigned64(42)")
    else:
        failed = failed + 1
        print_satanic("TEST FAIL: unsigned64(42)")

    print_satanic("--- self tests done ---")
    print_satanic("passed: " + int_to_str(passed) + " failed: " + int_to_str(failed))
    if failed == 0:
        print_satanic("ALL TESTS PASSED")
    else:
        print_satanic("SOME TESTS FAILED")

def main():
    print_satanic("Welcome to hell")
    print_satanic("This shit is written in Python")

    cpu = CPU()
    cpu.print_info()

    madt_addr = acpi_demo()

    cpio_demo()
    # "bin/sh", not "bin/init": the initramfs packages Alpine's static
    # busybox there (see PythonOS/build.py's build_initramfs()), and
    # busybox is a multi-call binary that picks its applet from argv[0]'s
    # basename -- it has to be exec'd as "sh" specifically to run the
    # ash shell rather than busybox's own (unrelated) "init" applet.
    init_file = find_module_file("bin/sh")

    pa = PageAllocator()
    vm = VirtualMemory(pa)
    va = VirtualAllocator(pa, vm)
    init_gdt(va)
    init_idt(va)
    init_ps2(va)
    init_kernel_vm(pa, vm)

    pentagram_demo()
    crosses_demo()

    kernel_cr3 = vm.pml4 - vm.hhdm

    elf_entry = 0
    user_rsp = 0
    process_cr3 = 0
    ioapic_phys = 0
    if init_file.addr != 0:
        process_vm = new_address_space(pa, vm)
        elf_info = load_elf(pa, process_vm, init_file.addr, True)
        elf_entry = elf_info.entry
        if elf_entry != 0:
            user_rsp = setup_user_stack(pa, process_vm, elf_info, "/bin/sh")
            process_cr3 = process_vm.pml4 - process_vm.hhdm
            init_brk(pa, process_vm, elf_info.brk_base, ELF_THREAD_TID)

    if madt_addr != 0:
        init_lapic(va, get_lapic_phys(madt_addr))
        ioapic_phys = get_first_ioapic_phys(madt_addr)
        if ioapic_phys != 0:
            init_ioapic(va, ioapic_phys)
            ioapic_demo()
            # IRQ1 (PS/2 keyboard) has no Interrupt Source Override in this
            # machine's MADT (unlike IRQ0/5/9/10/11, see acpi_demo()'s
            # printout), so identity mapping (ISA IRQ n == GSI n) applies.
            ioapic_set_redirection(1, VECTOR_KEYBOARD, lapic_id(), False)
        init_syscalls()
        calibrate_and_start_timer(VECTOR_TIMER, 500)

    # PCI scan and XHCI initialization (after interrupts are set up)
    pci_scan_all()
    pci_find_class(0x0C, 0x03)
    xhci_bus = pci_found_bus
    xhci_slot = pci_found_slot
    xhci_func = pci_found_func
    xhci_irq = 0
    if xhci_bus != -1:
        xhci_irq = pci_get_interrupt_line(xhci_bus, xhci_slot, xhci_func)
        print_satanic("XHCI found at IRQ " + int_to_str(xhci_irq))
        xhci_result = init_xhci(va, pa, xhci_bus, xhci_slot, xhci_func)
        if xhci_result != 0:
            # Enumerate first, route the IRQ after: enumeration's command
            # and control-transfer waits busy-poll the event ring
            # directly, so routing the xHCI IRQ any earlier would let
            # on_xhci_irq() race them for the very same completion events.
            enumerate_usb_devices(pa, va)
            init_usb_hid(va)
            if madt_addr != 0 and ioapic_phys != 0:
                # Level-triggered: see ioapic_set_redirection()'s own
                # comment -- this machine's MADT override for this
                # GSI confirms it, and it's what actually let the xHC
                # keep signaling new HID reports past the first one.
                ioapic_set_redirection(xhci_irq, VECTOR_XHCI, lapic_id(), True)


    if madt_addr != 0:
        init_threads(va, kernel_cr3, elf_entry, user_rsp, process_cr3)

main()
