from satanic import print_satanic, int_to_str

def unsigned64(x: int):
    if x < 0:
        return x + 2 ** 64
    return x

def limine_response(magic_a: int, magic_b: int):
    table = _LIMINE()
    result = 0
    i = 0
    entry_addr = peek64(table)
    while entry_addr != 0:
        a = unsigned64(peek64(entry_addr + 16))
        b = unsigned64(peek64(entry_addr + 24))
        if a == magic_a and b == magic_b:
            result = peek64(entry_addr + 40)
        i = i + 1
        entry_addr = peek64(table + i * 8)
    return result

MEMMAP_MAGIC_A = 0x67cf3d9d378a806f
MEMMAP_MAGIC_B = 0xe304acdfc50c3c62
HHDM_MAGIC_A = 0x48dcf1cb8ad2b852
HHDM_MAGIC_B = 0x63984e959a98244b

class MemoryMapEntry:
    def __init__(self, addr: int):
        self.base = peek64(addr)
        self.length = peek64(addr + 8)
        self.type = peek64(addr + 16)
    def is_usable(self):
        return self.type == 0
    def __str__(self):
        if self.is_usable():
            label = "usable"
        else:
            label = "reserved"
        return hex(self.base) + " + " + hex(self.length) + " (" + label + ")"

class MemoryMap:
    def __init__(self):
        response = limine_response(MEMMAP_MAGIC_A, MEMMAP_MAGIC_B)
        self.count = peek64(response + 8)
        self.entries_ptr = peek64(response + 16)
    def entry(self, index: int):
        entry_addr = peek64(self.entries_ptr + index * 8)
        return MemoryMapEntry(entry_addr)
    def total_usable(self):
        total = 0
        i = 0
        while i < self.count:
            e = self.entry(i)
            if e.is_usable():
                total = total + e.length
            i = i + 1
        return total
    def print_all(self):
        print_satanic("Memory map entries:")
        print_satanic(int_to_str(self.count))
        i = 0
        while i < self.count:
            print_satanic(self.entry(i).__str__())
            i = i + 1
        print_satanic("Total usable bytes:")
        print_satanic(hex(self.total_usable()))

def hhdm_offset():
    response = limine_response(HHDM_MAGIC_A, HHDM_MAGIC_B)
    return peek64(response + 8)

def memory_map_demo():
    mm = MemoryMap()
    mm.print_all()
    print_satanic("HHDM offset:")
    print_satanic(hex(unsigned64(hhdm_offset())))

PAGE_SIZE = 4096
# Maximum number of freed pages the free list can track. Each entry is
# one raw 8-byte slot at (free list page's virtual address) + i*8. A page
# whose physical address is stored here is "free" and will be handed out
# by alloc_page() before bumping into fresh memory.
MAX_FREE_PAGES = 512  # 512 * 8 = 4096 = exactly one page

class PageAllocator:
    def __init__(self):
        self.mm = MemoryMap()
        self.hhdm = unsigned64(hhdm_offset())
        self.region_index = 0
        self.next_phys = 0
        self.region_end = 0
        self.allocated = 0
        self._free_count = 0
        self._free_list_page = 0
        self._free_list_virt = 0
        self._free_list_ready = 0
        self._enter_next_usable_region()

    def _ensure_free_list(self):
        # Allocate the free list page on first use, not at __init__ time.
        # This keeps the early allocation stream identical to the
        # original bump-only allocator so GDT/IDT/TSS pages land at the
        # same physical addresses the boot code expects.
        if self._free_list_ready != 0:
            return
        self._free_list_page = self.next_phys
        self._free_list_virt = self._free_list_page + self.hhdm
        self.next_phys = self.next_phys + PAGE_SIZE
        self.allocated = self.allocated + 1
        if self.next_phys + PAGE_SIZE > self.region_end:
            self.region_index = self.region_index + 1
            self._enter_next_usable_region()
        i = 0
        while i < MAX_FREE_PAGES:
            poke64(self._free_list_virt + i * 8, 0)
            i = i + 1
        self._free_list_ready = 1

    def _enter_next_usable_region(self):
        while self.region_index < self.mm.count:
            e = self.mm.entry(self.region_index)
            if e.is_usable():
                start = e.base
                rem = start % PAGE_SIZE
                if rem != 0:
                    start = start + (PAGE_SIZE - rem)
                end = e.base + e.length
                if start + PAGE_SIZE <= end:
                    self.next_phys = start
                    self.region_end = end
                    return
            self.region_index = self.region_index + 1
        # No usable region left: make next_phys/region_end reflect that
        # instead of leaving them pointed at the tail of the last region,
        # so nothing downstream can mistake stale values for real space.
        self.next_phys = 0
        self.region_end = 0

    def alloc_page(self):
        if self._free_count > 0:
            self._ensure_free_list()
            self._free_count = self._free_count - 1
            phys = peek64(self._free_list_virt + self._free_count * 8)
            raw_memzero(phys + self.hhdm, PAGE_SIZE)
            return phys + self.hhdm
        if self.region_index >= self.mm.count:
            return 0
        phys = self.next_phys
        self.next_phys = self.next_phys + PAGE_SIZE
        self.allocated = self.allocated + 1
        if self.next_phys + PAGE_SIZE > self.region_end:
            self.region_index = self.region_index + 1
            self._enter_next_usable_region()
        return phys + self.hhdm

    def free_page(self, virt_addr: int):
        self._ensure_free_list()
        phys = virt_addr - self.hhdm
        if self._free_count >= MAX_FREE_PAGES:
            print_satanic("PageAllocator: free list full, leaking a page")
            return 0
        i = 0
        while i < self._free_count:
            if peek64(self._free_list_virt + i * 8) == phys:
                print_satanic("PageAllocator: double free ignored")
                return 0
            i = i + 1
        poke64(self._free_list_virt + self._free_count * 8, phys)
        self._free_count = self._free_count + 1
        return 1

def page_allocator_demo():
    pa = PageAllocator()
    p1 = pa.alloc_page()
    p2 = pa.alloc_page()
    print_satanic("Page 1:")
    print_satanic(hex(p1))
    print_satanic("Page 2:")
    print_satanic(hex(p2))
    poke64(p1, 0xdeadbeef)
    value = peek64(p1)
    print_satanic("Wrote and read back:")
    print_satanic(hex(value))
    print_satanic("Pages allocated so far:")
    print_satanic(int_to_str(pa.allocated))
    # Test free + reuse
    pa.free_page(p1)
    print_satanic("Freed page 1, free_count:")
    print_satanic(int_to_str(pa._free_count))
    p3 = pa.alloc_page()
    print_satanic("Reused page (should match page 1):")
    print_satanic(hex(p3))
    if p3 == p1:
        print_satanic("PASS: free/reuse works")
    else:
        print_satanic("FAIL: free/reuse mismatch")
