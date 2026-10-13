from satanic import print_satanic
from vmm import PAGE_WRITABLE

ELF_MAGIC0 = 0x7f
PT_LOAD = 1

E_ENTRY_OFFSET = 24
E_PHOFF_OFFSET = 32
E_PHENTSIZE_OFFSET = 54
E_PHNUM_OFFSET = 56

# Elf64_Phdr field offsets (56 bytes total).
PH_TYPE = 0
PH_OFFSET = 8
PH_VADDR = 16
PH_FILESZ = 32
PH_MEMSZ = 40

PAGE_SIZE = 4096

def _align_down(n: int, align: int):
    return n - (n % align)

def _align_up(n: int, align: int):
    rem = n % align
    if rem == 0:
        return n
    return n + (align - rem)

def _load_segment(pa, vm, file_addr: int, vaddr: int, filesz: int, memsz: int, user: bool):
    start = _align_down(vaddr, PAGE_SIZE)
    end = _align_up(vaddr + memsz, PAGE_SIZE)
    page = start
    while page < end:
        phys_page = pa.alloc_page()
        phys = phys_page - vm.hhdm
        vm.map(page, phys, PAGE_WRITABLE, user)

        raw_memzero(phys_page, PAGE_SIZE)

        seg_start = page - vaddr
        copy_start = seg_start
        if copy_start < 0:
            copy_start = 0
        copy_end = seg_start + PAGE_SIZE
        if copy_end > filesz:
            copy_end = filesz
        if copy_end > copy_start:
            raw_memcpy(phys_page + (copy_start - seg_start), file_addr + copy_start,
                       copy_end - copy_start)

        page = page + PAGE_SIZE

class ElfInfo:
    def __init__(self, entry: int, phdr: int, phentsize: int, phnum: int, brk_base: int):
        self.entry = entry
        self.phdr = phdr
        self.phentsize = phentsize
        self.phnum = phnum
        self.brk_base = brk_base

def load_elf(pa, vm, data_addr: int, user: bool):
    magic0 = peek8(data_addr)
    if magic0 != ELF_MAGIC0:
        print_satanic("Not an ELF file")
        return ElfInfo(0, 0, 0, 0, 0)

    entry = peek64(data_addr + E_ENTRY_OFFSET)
    phoff = peek64(data_addr + E_PHOFF_OFFSET)
    phentsize = peek16(data_addr + E_PHENTSIZE_OFFSET)
    phnum = peek16(data_addr + E_PHNUM_OFFSET)
    phdr_vaddr = 0
    brk_base = 0

    i = 0
    while i < phnum:
        phdr = data_addr + phoff + i * phentsize
        p_type = peek32(phdr + PH_TYPE)
        if p_type == PT_LOAD:
            p_offset = peek64(phdr + PH_OFFSET)
            p_vaddr = peek64(phdr + PH_VADDR)
            p_filesz = peek64(phdr + PH_FILESZ)
            p_memsz = peek64(phdr + PH_MEMSZ)
            _load_segment(pa, vm, data_addr + p_offset, p_vaddr, p_filesz, p_memsz, user)
            if p_offset == 0:
                phdr_vaddr = p_vaddr + phoff
            seg_end = _align_up(p_vaddr + p_memsz, PAGE_SIZE)
            if seg_end > brk_base:
                brk_base = seg_end
        i = i + 1

    print_satanic("Loaded ELF, entry point:")
    print_satanic(hex(entry))
    return ElfInfo(entry, phdr_vaddr, phentsize, phnum, brk_base)
