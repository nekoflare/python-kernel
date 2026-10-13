from memory import unsigned64, PAGE_SIZE
from satanic import print_satanic

PAGE_PRESENT = 0x1
PAGE_WRITABLE = 0x2
PAGE_USER = 0x4
PAGE_PCD = 0x10
PAGE_ADDR_MASK = 0x000FFFFFFFFFF000

class VirtualMemory:
    def __init__(self, page_allocator):
        self.pa = page_allocator
        self.hhdm = page_allocator.hhdm
        pml4_phys = unsigned64(read_cr3()) & PAGE_ADDR_MASK
        self.pml4 = pml4_phys + self.hhdm

    def _zero_page(self, virt: int):
        i = 0
        while i < 512:
            poke64(virt + i * 8, 0)
            i = i + 1

    def _index(self, vaddr: int, shift: int):
        return (vaddr >> shift) & 0x1FF

    def _step(self, table_virt: int, index: int, user: bool):
        entry_addr = table_virt + index * 8
        entry = unsigned64(peek64(entry_addr))
        if entry & PAGE_PRESENT == 0:
            child_virt = self.pa.alloc_page()
            self._zero_page(child_virt)
            child_phys = child_virt - self.hhdm
            flags = PAGE_PRESENT | PAGE_WRITABLE
            if user:
                flags = flags | PAGE_USER
            poke64(entry_addr, child_phys | flags)
            return child_virt
        if user and entry & PAGE_USER == 0:
            poke64(entry_addr, entry | PAGE_USER)
        return (entry & PAGE_ADDR_MASK) + self.hhdm

    def map(self, vaddr: int, paddr: int, flags: int, user: bool):
        pdpt = self._step(self.pml4, self._index(vaddr, 39), user)
        pd = self._step(pdpt, self._index(vaddr, 30), user)
        pt = self._step(pd, self._index(vaddr, 21), user)
        entry_addr = pt + self._index(vaddr, 12) * 8
        final_flags = flags | PAGE_PRESENT
        if user:
            final_flags = final_flags | PAGE_USER
        poke64(entry_addr, (paddr & PAGE_ADDR_MASK) | final_flags)

KERNEL_VBASE = 0xffffffff80000000

def find_vmalloc_base(mm, hhdm: int):
    highest = 0
    i = 0
    while i < mm.count:
        e = mm.entry(i)
        end = e.base + e.length
        if end > highest:
            highest = end
        i = i + 1
    rem = highest % PAGE_SIZE
    if rem != 0:
        highest = highest + (PAGE_SIZE - rem)
    return hhdm + highest

class VirtualAllocator:
    def __init__(self, page_allocator, virtual_memory):
        self.pa = page_allocator
        self.vm = virtual_memory
        self.next_vaddr = find_vmalloc_base(page_allocator.mm, virtual_memory.hhdm)

    def alloc(self, size: int):
        pages = size // PAGE_SIZE
        if size % PAGE_SIZE != 0:
            pages = pages + 1
        base_vaddr = self.next_vaddr
        i = 0
        while i < pages:
            phys_page = self.pa.alloc_page()
            phys = phys_page - self.vm.hhdm
            self.vm.map(self.next_vaddr, phys, PAGE_WRITABLE, False)
            self.next_vaddr = self.next_vaddr + PAGE_SIZE
            i = i + 1
        return base_vaddr

    def reserve(self, size: int):
        # Like alloc(), but only bumps the virtual-address pointer; it
        # doesn't back the range with a freshly-allocated physical page.
        # For MMIO (LAPIC, I/O APIC, ...) the physical address is already
        # fixed by hardware, so alloc()'s "also get me a new physical
        # frame" would be wrong; the caller maps the real address itself
        # via self.vm.map().
        pages = size // PAGE_SIZE
        if size % PAGE_SIZE != 0:
            pages = pages + 1
        base_vaddr = self.next_vaddr
        self.next_vaddr = self.next_vaddr + pages * PAGE_SIZE
        return base_vaddr

def vmalloc_demo(va):
    print_satanic("vmalloc arena free space:")
    print_satanic(hex(KERNEL_VBASE - va.next_vaddr))

    p1 = va.alloc(4096)
    p2 = va.alloc(8192)

    poke64(p1, 0x1111111111111111)
    poke64(p2, 0x2222222222222222)
    poke64(p2 + 4096, 0x3333333333333333)

    print_satanic("Allocation 1 (4096 bytes) at:")
    print_satanic(hex(p1))
    print_satanic(hex(peek64(p1)))
    print_satanic("Allocation 2 (8192 bytes) at:")
    print_satanic(hex(p2))
    print_satanic(hex(peek64(p2)))
    print_satanic("Allocation 2's second page:")
    print_satanic(hex(peek64(p2 + 4096)))

def vmm_demo(pa, vm):
    data_page = pa.alloc_page()
    data_phys = data_page - vm.hhdm

    test_vaddr = 0x0000700000000000
    vm.map(test_vaddr, data_phys, PAGE_WRITABLE, False)

    poke64(test_vaddr, 0xcafebabe)
    value = peek64(test_vaddr)

    print_satanic("Virtual memory test address:")
    print_satanic(hex(test_vaddr))
    print_satanic("Mapped to physical page:")
    print_satanic(hex(data_phys))
    print_satanic("Wrote and read back through the new mapping:")
    print_satanic(hex(value))
