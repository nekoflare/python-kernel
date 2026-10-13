from vmm import VirtualMemory, PAGE_PRESENT, PAGE_WRITABLE, PAGE_USER, PAGE_ADDR_MASK
from memory import unsigned64

PAGE_SIZE = 4096

# Fixed, hand-picked low address for the loaded app's own user-mode
# stack -- there's only ever one such process right now, so (like the
# LAPIC/IOAPIC MMIO addresses before VirtualAllocator.reserve() existed)
# a single fixed address is simplest; nothing else lives down here.
USER_STACK_VADDR = 0x700000
USER_STACK_SIZE = 131072

# Auxiliary vector types a real glibc CRT reads off the initial stack
# (see setup_user_stack()) -- glibc's own _start/__libc_start_main use
# these for TLS setup (AT_PHDR/AT_PHENT/AT_PHNUM, to re-find its own
# PT_TLS segment), the stack-protector canary (AT_RANDOM), and a few
# other startup checks. Nowhere near the full set the real Linux kernel
# provides -- more get added here as glibc's own startup path turns out
# to need them.
AT_NULL = 0
AT_PHDR = 3
AT_PHENT = 4
AT_PHNUM = 5
AT_PAGESZ = 6
AT_BASE = 7
AT_ENTRY = 9
AT_HWCAP = 16
AT_SECURE = 23
AT_RANDOM = 25

def new_address_space(pa, kernel_vm):
    # A fresh PML4 for a real, separate address space -- but every PML4
    # index >= 256 covers virtual addresses >= 0xffff800000000000, which
    # for this kernel's own layout is entirely the HHDM identity map,
    # the higher-half kernel image, and the vmalloc/heap range
    # VirtualAllocator hands out from -- copying those entries wholesale
    # is what lets kernel code (interrupt handlers, syscall_dispatch,
    # the scheduler itself) keep working no matter which process's PML4
    # happens to be loaded when they run. Everything below index 256 is
    # left zero: unmapped until this process's own segments/stack are
    # mapped into it below, and never shared with anything else.
    new_pml4 = pa.alloc_page()
    i = 0
    while i < 512:
        poke64(new_pml4 + i * 8, 0)
        i = i + 1
    i = 256
    while i < 512:
        poke64(new_pml4 + i * 8, peek64(kernel_vm.pml4 + i * 8))
        i = i + 1

    vm = VirtualMemory(pa)
    vm.pml4 = new_pml4
    return vm

def duplicate_address_space(pa, kernel_vm, source_vm):
    # A real fork(): everything new_address_space() already does (a
    # fresh PML4 sharing the kernel's own upper half), plus an eager,
    # full copy of every page the SOURCE process has mapped in its own
    # lower half -- walking all four page-table levels by hand (PyCk
    # has no higher-level "list every mapping" API; vmm.py's own
    # VirtualMemory only ever builds mappings, never enumerates them)
    # and, for each present leaf page, allocating a fresh physical page,
    # copying its contents, and mapping that copy at the *same* virtual
    # address in the new address space with the same permissions. No
    # copy-on-write: this kernel's own philosophy elsewhere (the never-
    # frees bump allocator, brk() that only ever grows) is "correct and
    # simple over efficient", and a real process image here is small
    # enough (a few hundred KB) that eagerly copying it is genuinely
    # cheap, not just expedient.
    new_vm = new_address_space(pa, kernel_vm)
    i = 0
    while i < 256:
        pml4e = unsigned64(peek64(source_vm.pml4 + i * 8))
        if pml4e & PAGE_PRESENT != 0:
            pdpt_virt = (pml4e & PAGE_ADDR_MASK) + source_vm.hhdm
            j = 0
            while j < 512:
                pdpte = unsigned64(peek64(pdpt_virt + j * 8))
                if pdpte & PAGE_PRESENT != 0:
                    pd_virt = (pdpte & PAGE_ADDR_MASK) + source_vm.hhdm
                    k = 0
                    while k < 512:
                        pde = unsigned64(peek64(pd_virt + k * 8))
                        if pde & PAGE_PRESENT != 0:
                            pt_virt = (pde & PAGE_ADDR_MASK) + source_vm.hhdm
                            l = 0
                            while l < 512:
                                pte = unsigned64(peek64(pt_virt + l * 8))
                                if pte & PAGE_PRESENT != 0:
                                    vaddr = (i << 39) | (j << 30) | (k << 21) | (l << 12)
                                    src_page_alias = (pte & PAGE_ADDR_MASK) + source_vm.hhdm
                                    new_phys_page = pa.alloc_page()
                                    raw_memcpy(new_phys_page, src_page_alias, PAGE_SIZE)
                                    map_flags = 0
                                    if pte & PAGE_WRITABLE != 0:
                                        map_flags = PAGE_WRITABLE
                                    is_user = pte & PAGE_USER != 0
                                    new_vm.map(vaddr, new_phys_page - new_vm.hhdm, map_flags, is_user)
                                l = l + 1
                        k = k + 1
                j = j + 1
        i = i + 1
    return new_vm

def lookup_phys_alias(vm, vaddr: int):
    # Read-only 4-level page-table walk for exactly one virtual address
    # -- returns the HHDM alias of the physical byte backing it (not
    # just the page), or 0 if unmapped. VirtualMemory's own _step()
    # (see vmm.py) always *creates* missing tables, which is wrong for
    # a lookup; this never writes anything.
    #
    # SYS_CLONE (see syscall.py) is the one caller: real Linux fork()
    # with CLONE_CHILD_SETTID writes the child's own new tid into a
    # user-supplied pointer *in the child's own memory*, before the
    # child ever runs a single instruction -- glibc's post-fork child
    # bookkeeping reads that value back and depends on it being
    # correct. duplicate_address_space() has already built the child's
    # address space by the time SYS_CLONE needs to do this write, but
    # it isn't the *active* one yet (write_cr3() hasn't run), so this
    # is the same "write through the HHDM alias, not the raw vaddr"
    # requirement setup_user_stack()/elf.py's _load_segment() already
    # have for exactly the same reason.
    i = (vaddr >> 39) & 0x1FF
    pml4e = unsigned64(peek64(vm.pml4 + i * 8))
    if pml4e & PAGE_PRESENT == 0:
        return 0
    j = (vaddr >> 30) & 0x1FF
    pdpte = unsigned64(peek64((pml4e & PAGE_ADDR_MASK) + vm.hhdm + j * 8))
    if pdpte & PAGE_PRESENT == 0:
        return 0
    k = (vaddr >> 21) & 0x1FF
    pde = unsigned64(peek64((pdpte & PAGE_ADDR_MASK) + vm.hhdm + k * 8))
    if pde & PAGE_PRESENT == 0:
        return 0
    l = (vaddr >> 12) & 0x1FF
    pte = unsigned64(peek64((pde & PAGE_ADDR_MASK) + vm.hhdm + l * 8))
    if pte & PAGE_PRESENT == 0:
        return 0
    return (pte & PAGE_ADDR_MASK) + vm.hhdm + (vaddr % PAGE_SIZE)

def _alias(vaddr: int, page_vaddr: int, page_alias: int):
    return page_alias + (vaddr - page_vaddr)

def _char_to_ascii(c: str):
    # PyCk has chr() (int -> single-char str) but not the reverse. This
    # handles the full printable ASCII range so that any path, string
    # literal, or uname field can be faithfully round-tripped through
    # userspace without silently becoming '?'.
    # Lowercase letters
    letters = "abcdefghijklmnopqrstuvwxyz"
    i = 0
    while i < 26:
        if letters[i] == c:
            return 97 + i
        i = i + 1
    # Uppercase letters
    letters_upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    i = 0
    while i < 26:
        if letters_upper[i] == c:
            return 65 + i
        i = i + 1
    # Digits
    digits = "0123456789"
    i = 0
    while i < 10:
        if digits[i] == c:
            return 48 + i
        i = i + 1
    # Punctuation and special characters
    if c == "/":
        return 47
    if c == ".":
        return 46
    if c == "_":
        return 95
    if c == "-":
        return 45
    if c == " ":
        return 32
    if c == ":":
        return 58
    if c == ",":
        return 44
    if c == "=":
        return 61
    if c == "+":
        return 43
    if c == "(":
        return 40
    if c == ")":
        return 41
    if c == "[":
        return 91
    if c == "]":
        return 93
    if c == "{":
        return 123
    if c == "}":
        return 125
    if c == "<":
        return 60
    if c == ">":
        return 62
    if c == "!":
        return 33
    if c == "?":
        return 63
    if c == "#":
        return 35
    if c == "%":
        return 37
    if c == "@":
        return 64
    if c == "'":
        return 39
    if c == '"':
        return 34
    if c == "`":
        return 96
    if c == "~":
        return 126
    if c == "^":
        return 94
    if c == "&":
        return 38
    if c == "*":
        return 42
    if c == "|":
        return 124
    if c == "\\":
        return 92
    if c == ";":
        return 59
    if c == "\n":
        return 10
    if c == "\t":
        return 9
    return 63  # '?' -- truly unknown

def setup_user_stack(pa, vm, elf_info, argv0: str):
    # Builds a real Linux-ABI initial stack (argc/argv/envp/auxv, not
    # just a bare pointer) and returns its top (initial RSP) -- a real
    # glibc CRT's _start reads all of this before main() ever runs, and
    # a page of zeros in its place reads as argc=0 with a NULL AT_PHDR,
    # AT_RANDOM, etc., which crashes glibc's own startup (e.g. reading
    # 16 "random" bytes for the stack-protector canary from address 0)
    # long before anything reaches an actual syscall.
    top = USER_STACK_VADDR + USER_STACK_SIZE
    page = USER_STACK_VADDR
    top_page_alias = 0
    while page < top:
        phys_page = pa.alloc_page()
        phys = phys_page - vm.hhdm
        vm.map(page, phys, PAGE_WRITABLE, True)
        raw_memzero(phys_page, PAGE_SIZE)
        if page + PAGE_SIZE == top:
            top_page_alias = phys_page
        page = page + PAGE_SIZE

    # Every write below lands in this same top page -- the whole
    # argv0 string + AT_RANDOM bytes + argc/argv/envp/auxv arrays are
    # a few hundred bytes, comfortably inside one 4096-byte page --
    # so one HHDM alias covers all of it. Always through that alias,
    # never through the plain vaddr: `vm` maps a *separate* address
    # space here (see new_address_space()), not necessarily the
    # currently active page tables, so the low vaddr isn't guaranteed
    # reachable yet (see elf.py's load_elf() for the same reasoning).
    top_page_vaddr = top - PAGE_SIZE

    cursor = top

    cursor = cursor - (len(argv0) + 1)
    argv0_vaddr = cursor
    i = 0
    while i < len(argv0):
        poke8(_alias(argv0_vaddr + i, top_page_vaddr, top_page_alias), _char_to_ascii(argv0[i]))
        i = i + 1
    poke8(_alias(argv0_vaddr + len(argv0), top_page_vaddr, top_page_alias), 0)

    # Not cryptographically random -- glibc only needs *some* readable
    # bytes here (stack-protector canary, a few internal checks), not
    # real entropy, and this kernel has no entropy source yet anyway.
    cursor = cursor - 16
    random_vaddr = cursor
    i = 0
    while i < 16:
        poke8(_alias(random_vaddr + i, top_page_vaddr, top_page_alias), 0x42)
        i = i + 1

    cursor = cursor - (cursor % 16)

    # From here down: argc, argv[0], argv NULL terminator, envp NULL
    # terminator (no env vars), then the auxv array, then AT_NULL --
    # 8 + 16 + 8 + 10*16 = 192 bytes, itself a multiple of 16, so
    # aligning `cursor` above is what keeps final_rsp (== argc's own
    # address, which the psABI requires be 16-aligned at process
    # entry) properly aligned too.
    final_rsp = cursor - 192

    poke64(_alias(final_rsp, top_page_vaddr, top_page_alias), 1)
    poke64(_alias(final_rsp + 8, top_page_vaddr, top_page_alias), argv0_vaddr)
    poke64(_alias(final_rsp + 16, top_page_vaddr, top_page_alias), 0)
    poke64(_alias(final_rsp + 24, top_page_vaddr, top_page_alias), 0)

    auxv = final_rsp + 32
    poke64(_alias(auxv + 0, top_page_vaddr, top_page_alias), AT_PHDR)
    poke64(_alias(auxv + 8, top_page_vaddr, top_page_alias), elf_info.phdr)
    poke64(_alias(auxv + 16, top_page_vaddr, top_page_alias), AT_PHENT)
    poke64(_alias(auxv + 24, top_page_vaddr, top_page_alias), elf_info.phentsize)
    poke64(_alias(auxv + 32, top_page_vaddr, top_page_alias), AT_PHNUM)
    poke64(_alias(auxv + 40, top_page_vaddr, top_page_alias), elf_info.phnum)
    poke64(_alias(auxv + 48, top_page_vaddr, top_page_alias), AT_PAGESZ)
    poke64(_alias(auxv + 56, top_page_vaddr, top_page_alias), PAGE_SIZE)
    poke64(_alias(auxv + 64, top_page_vaddr, top_page_alias), AT_BASE)
    poke64(_alias(auxv + 72, top_page_vaddr, top_page_alias), 0)
    poke64(_alias(auxv + 80, top_page_vaddr, top_page_alias), AT_ENTRY)
    poke64(_alias(auxv + 88, top_page_vaddr, top_page_alias), elf_info.entry)
    poke64(_alias(auxv + 96, top_page_vaddr, top_page_alias), AT_HWCAP)
    poke64(_alias(auxv + 104, top_page_vaddr, top_page_alias), 0)
    poke64(_alias(auxv + 112, top_page_vaddr, top_page_alias), AT_SECURE)
    poke64(_alias(auxv + 120, top_page_vaddr, top_page_alias), 0)
    poke64(_alias(auxv + 128, top_page_vaddr, top_page_alias), AT_RANDOM)
    poke64(_alias(auxv + 136, top_page_vaddr, top_page_alias), random_vaddr)
    poke64(_alias(auxv + 144, top_page_vaddr, top_page_alias), AT_NULL)
    poke64(_alias(auxv + 152, top_page_vaddr, top_page_alias), 0)

    return final_rsp

def _cstr_len_at(addr: int):
    n = 0
    while peek8(addr + n) != 0:
        n = n + 1
    return n

def setup_user_stack_argv(pa, vm, elf_info, argv_ptr: int):
    # Same real Linux-ABI initial stack as setup_user_stack() above,
    # but for execve() (see SYS_EXECVE in syscall.py): copies the
    # *actual* argv[] the calling process passed -- a NULL-terminated
    # array of char* still live in the OLD address space's own page
    # tables at this point (this always runs before CR3 switches to
    # the new process) -- instead of synthesizing a single-element
    # argv0. Before this existed, every execve() silently discarded
    # argv[1:] and handed the new process argc=1, so e.g. `ls /bin`
    # ran with the exact same (empty) argument list as plain `ls`,
    # confirmed via boot: both printed identical output.
    #
    # PyCk has no list type to stash each string's own new-stack
    # address in for a later pass, so this is two passes over the
    # caller's argv instead: first just total up how many bytes all
    # the strings need (so where the argv[]/envp[]/auxv arrays
    # themselves land -- which depends on that total -- is known
    # before anything is actually written), then copy each string's
    # raw bytes (not through _char_to_ascii -- these already are
    # ASCII/UTF-8 bytes sitting in the old process's own memory, no
    # need to round-trip them through a Python str) and fill in its
    # argv[] slot in the same pass.
    argc = 0
    while peek64(argv_ptr + argc * 8) != 0:
        argc = argc + 1

    top = USER_STACK_VADDR + USER_STACK_SIZE
    page = USER_STACK_VADDR
    top_page_alias = 0
    while page < top:
        phys_page = pa.alloc_page()
        phys = phys_page - vm.hhdm
        vm.map(page, phys, PAGE_WRITABLE, True)
        raw_memzero(phys_page, PAGE_SIZE)
        if page + PAGE_SIZE == top:
            top_page_alias = phys_page
        page = page + PAGE_SIZE

    top_page_vaddr = top - PAGE_SIZE

    total_str_bytes = 0
    j = 0
    while j < argc:
        old_ptr = peek64(argv_ptr + j * 8)
        total_str_bytes = total_str_bytes + _cstr_len_at(old_ptr) + 1
        j = j + 1

    strings_base = top - total_str_bytes
    cursor = strings_base - 16
    random_vaddr = cursor
    cursor = cursor - (cursor % 16)
    header_size = 8 * argc + 184
    final_rsp = cursor - header_size
    argv_table = final_rsp + 8

    i = 0
    while i < 16:
        poke8(_alias(random_vaddr + i, top_page_vaddr, top_page_alias), 0x42)
        i = i + 1

    offset = 0
    j = 0
    while j < argc:
        old_ptr = peek64(argv_ptr + j * 8)
        slen = _cstr_len_at(old_ptr)
        str_vaddr = strings_base + offset
        k = 0
        while k < slen:
            poke8(_alias(str_vaddr + k, top_page_vaddr, top_page_alias), peek8(old_ptr + k))
            k = k + 1
        poke8(_alias(str_vaddr + slen, top_page_vaddr, top_page_alias), 0)
        poke64(_alias(argv_table + j * 8, top_page_vaddr, top_page_alias), str_vaddr)
        offset = offset + slen + 1
        j = j + 1

    poke64(_alias(final_rsp, top_page_vaddr, top_page_alias), argc)
    poke64(_alias(argv_table + argc * 8, top_page_vaddr, top_page_alias), 0)
    envp_null = argv_table + (argc + 1) * 8
    poke64(_alias(envp_null, top_page_vaddr, top_page_alias), 0)

    auxv = envp_null + 8
    poke64(_alias(auxv + 0, top_page_vaddr, top_page_alias), AT_PHDR)
    poke64(_alias(auxv + 8, top_page_vaddr, top_page_alias), elf_info.phdr)
    poke64(_alias(auxv + 16, top_page_vaddr, top_page_alias), AT_PHENT)
    poke64(_alias(auxv + 24, top_page_vaddr, top_page_alias), elf_info.phentsize)
    poke64(_alias(auxv + 32, top_page_vaddr, top_page_alias), AT_PHNUM)
    poke64(_alias(auxv + 40, top_page_vaddr, top_page_alias), elf_info.phnum)
    poke64(_alias(auxv + 48, top_page_vaddr, top_page_alias), AT_PAGESZ)
    poke64(_alias(auxv + 56, top_page_vaddr, top_page_alias), PAGE_SIZE)
    poke64(_alias(auxv + 64, top_page_vaddr, top_page_alias), AT_BASE)
    poke64(_alias(auxv + 72, top_page_vaddr, top_page_alias), 0)
    poke64(_alias(auxv + 80, top_page_vaddr, top_page_alias), AT_ENTRY)
    poke64(_alias(auxv + 88, top_page_vaddr, top_page_alias), elf_info.entry)
    poke64(_alias(auxv + 96, top_page_vaddr, top_page_alias), AT_HWCAP)
    poke64(_alias(auxv + 104, top_page_vaddr, top_page_alias), 0)
    poke64(_alias(auxv + 112, top_page_vaddr, top_page_alias), AT_SECURE)
    poke64(_alias(auxv + 120, top_page_vaddr, top_page_alias), 0)
    poke64(_alias(auxv + 128, top_page_vaddr, top_page_alias), AT_RANDOM)
    poke64(_alias(auxv + 136, top_page_vaddr, top_page_alias), random_vaddr)
    poke64(_alias(auxv + 144, top_page_vaddr, top_page_alias), AT_NULL)
    poke64(_alias(auxv + 152, top_page_vaddr, top_page_alias), 0)

    return final_rsp
