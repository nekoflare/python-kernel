from satanic import print_satanic

KERNEL_CODE_SEL = 0x08
KERNEL_DATA_SEL = 0x10
# Deliberately unused as a real segment (see Gdt.__init__'s own
# comment). It exists only so IA32_STAR's SYSRET field (see
# syscall.py's init_syscalls()) can be set to this value, making
# SYSRET's fixed base+8/base+16 formula land on USER_DATA_SEL/
# USER_CODE_SEL right after it.
#
# The +3 here (not a plain 0x18) is load-bearing, confirmed the hard
# way by booting: sysretq forces CS's RPL to 3 on its own, but does
# NOT force SS's RPL; SS is loaded as the raw base+8 value. With
# base=0x18, that raw value is 0x20 (RPL 0), so SS silently ends up
# wrong (RPL 0 instead of 3) after every syscall return. This stays
# invisible until an unrelated async interrupt (e.g. the timer) later
# captures that bad SS on the stack, and iretq rejects the RPL
# mismatch against CS (0x2B, RPL 3) with a #GP referencing selector
# 0x20. Baking the +3 into the base itself makes base+8 land exactly
# on 0x23 (USER_DATA_SEL, RPL already 3) without relying on hardware
# to fix it up, and base+16 still lands on 0x2B (USER_CODE_SEL) same
# as before.
SYSRET_BASE_SEL = 0x18 + 3
USER_DATA_SEL = 0x20 | 3
USER_CODE_SEL = 0x28 | 3
TSS_SEL = 0x30

# TSS64 field offsets (Intel SDM); only RSP0 and the I/O map base are
# ever actually set here. x86 doesn't require these to be 8-byte
# aligned, so poke64 at +4 (not +8) is intentional, not a bug.
TSS_RSP0_OFFSET = 4
TSS_IOPB_OFFSET = 102
TSS_SIZE = 0x68
TSS_LIMIT = TSS_SIZE - 1

def make_descriptor(access: int, flags: int):
    return (access << 40) | (flags << 52)

def _write_tss_descriptor(addr: int, base: int, limit: int):
    # A TSS descriptor is a 16-byte "system" descriptor (unlike a plain
    # code/data descriptor, S=0 in its access byte), needed because a
    # TSS's base address doesn't fit the same packed layout otherwise:
    # base[63:32] gets its own separate qword, with the next qword after
    # that reserved (must be zero).
    low = limit & 0xFFFF
    low = low | ((base & 0xFFFFFF) << 16)
    low = low | (0x89 << 40)                # P=1 DPL=0 S=0 type=1001 (avail 64-bit TSS)
    low = low | (((limit >> 16) & 0xF) << 48)
    low = low | (((base >> 24) & 0xFF) << 56)
    high = (base >> 32) & 0xFFFFFFFF
    poke64(addr, low)
    poke64(addr + 8, high)

class Gdt:
    def __init__(self, virtual_allocator):
        self.gdt_vaddr = virtual_allocator.alloc(4096)
        self.gdtr_vaddr = self.gdt_vaddr + 64
        self.tss_vaddr = virtual_allocator.alloc(4096)
        # A dedicated kernel stack the CPU switches to by itself (via
        # this TSS's RSP0 field) whenever an interrupt/exception arrives
        # while running at CPL3. The user stack can't be trusted for
        # that, and isr_common's own pushes need somewhere safe to land
        # before PyCk code (on_interrupt) ever runs.
        self.rsp0_stack = virtual_allocator.alloc(16384) + 16384

        null_desc = 0
        kcode_desc = make_descriptor(0x9A, 0x2)  # P=1 DPL=0 S=1 exec=1 rw=1, L=1
        kdata_desc = make_descriptor(0x92, 0x0)  # P=1 DPL=0 S=1 exec=0 rw=1
        # SYSRET (64-bit) computes SS = STAR[63:48]+8, CS = STAR[63:48]+16.
        # This is a fixed hardware formula, not a convention this kernel
        # could pick differently. Satisfying it needs the GDT ordered [[some
        # selector base V], data, code] with V+8 landing on the data
        # descriptor and V+16 on the code one; the slot at V itself
        # (0x18 below) is never actually loaded into a segment register
        # by sysretq, so its *content* doesn't matter, only that it
        # exists to make the arithmetic land right. See syscall.py's
        # init_syscalls() for the STAR value that picks V=0x18.
        u32_placeholder_desc = 0
        udata_desc = make_descriptor(0xF2, 0x0)  # P=1 DPL=3 S=1 exec=0 rw=1
        ucode_desc = make_descriptor(0xFA, 0x2)  # P=1 DPL=3 S=1 exec=1 rw=1, L=1

        poke64(self.gdt_vaddr + 0x00, null_desc)
        poke64(self.gdt_vaddr + 0x08, kcode_desc)
        poke64(self.gdt_vaddr + 0x10, kdata_desc)
        poke64(self.gdt_vaddr + 0x18, u32_placeholder_desc)
        poke64(self.gdt_vaddr + 0x20, udata_desc)
        poke64(self.gdt_vaddr + 0x28, ucode_desc)
        _write_tss_descriptor(self.gdt_vaddr + 0x30, self.tss_vaddr, TSS_LIMIT)

        i = 0
        while i < TSS_SIZE:
            poke8(self.tss_vaddr + i, 0)
            i = i + 1
        poke64(self.tss_vaddr + TSS_RSP0_OFFSET, self.rsp0_stack)
        # An I/O bitmap base left at 0 (from the zeroing above) would read
        # as "there's a bitmap right at the start of the TSS": since
        # that's actually the reserved field (all zero bits), the CPU
        # would treat every low-numbered port as permitted for CPL3 code.
        # Pointing it past the TSS's own limit is the standard idiom for
        # "no bitmap, deny all port I/O at CPL3". This process only ever
        # uses syscall/write/exit, never raw port I/O, so this should
        # never actually get exercised, but it's the correct default to
        # leave it at regardless.
        poke16(self.tss_vaddr + TSS_IOPB_OFFSET, TSS_SIZE)

        poke16(self.gdtr_vaddr, 8 * 8 - 1)
        poke64(self.gdtr_vaddr + 2, self.gdt_vaddr)

    def load(self):
        _ASM1("lgdt (%0)", self.gdtr_vaddr)
        _ASM(
            "mov $0x10, %ax\n"
            "mov %ax, %ds\n"
            "mov %ax, %es\n"
            "mov %ax, %fs\n"
            "mov %ax, %gs\n"
            "mov %ax, %ss\n"
            "pushq $0x08\n"
            "leaq 1f(%rip), %rax\n"
            "pushq %rax\n"
            "lretq\n"
            "1:\n"
        )
        _ASM(
            "mov $0x30, %ax\n"
            "ltr %ax\n"
        )

def init_gdt(va):
    # `va` is shared with init_idt() (see main.py) rather than each one
    # making its own PageAllocator/VirtualAllocator: unlike the actual
    # throwaway demos (vmm_demo, vmalloc_demo, ...), the GDT stays live in
    # the CPU's GDTR register for the rest of execution. This is real
    # kernel init, not a demo, so its virtual address must never get
    # silently reassigned to something else by a later, independently-
    # based allocator.
    gdt = Gdt(va)
    gdt.load()
    print_satanic("GDT init... OK")
