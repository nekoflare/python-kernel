from satanic import print_satanic, int_to_str
from gdt import KERNEL_CODE_SEL, SYSRET_BASE_SEL
from threads import (mark_current_blocked, mark_current_exited,
                      mark_thread_unblocked, schedule, alloc_thread_slot,
                      setup_fork_child, get_current_tid, get_parent, is_zombie,
                      find_zombie_child, has_any_child, reap_child, set_current_cr3,
                      set_current_poll_deadline, clear_current_poll_deadline,
                      MAX_THREADS)
from vmm import VirtualMemory, PAGE_WRITABLE
from memory import PageAllocator, unsigned64
from ps2 import (stdin_available, stdin_read, tty_get_icanon, tty_get_echo,
                  tty_set_icanon, tty_set_echo)
from process import (new_address_space, duplicate_address_space, setup_user_stack,
                      setup_user_stack_argv, lookup_phys_alias, _char_to_ascii)
from elf import load_elf
from cpio import (find_module_file, find_module, entry_name_at,
                   _read_hex8, _read_name, _align4, HEADER_SIZE)
from idt import get_tick_count

def _debugcon_str(text: str):
    # Straight to QEMU's debugcon (port 0xE9) only -- unlike
    # print_satanic()/print(), this never reaches the graphical
    # framebuffer console (see xhci.py's own debugcon/debugcon_hex for
    # the same direct-port-write pattern). The syscall stubs below fire
    # constantly and were flooding the framebuffer console right off
    # the visible screen; they're still worth having in the debugcon
    # log, just not there.
    n = len(text)
    i = 0
    while i < n:
        outb(0xE9, _char_to_ascii(text[i]))
        i = i + 1
    outb(0xE9, 10)  # newline

REG_RAX = 0
REG_RDI = 8
REG_RSI = 16
REG_RDX = 24
REG_R10 = 32
REG_R8 = 40
REG_R9 = 48
REG_R11 = 56
REG_RCX = 64
# The next six only exist because fork() needs them (see SYS_CLONE) --
# every *other* syscall this kernel handles never needed rbx/rbp/r12-r15
# at all, since the ordinary x86-64 ABI already guarantees they survive
# a syscall unchanged with zero help from this kernel. Offsets match
# syscall_entry.cpp's own layout comment exactly.
REG_R15 = 72
REG_R14 = 80
REG_R13 = 88
REG_R12 = 96
REG_RBP = 104
REG_RBX = 112
REG_RSP = 120

SYS_READ = 0
SYS_WRITE = 1
SYS_OPEN = 2
SYS_POLL = 7
POLLIN = 0x0001
POLLOUT = 0x0004
POLLFD_SIZE = 8
# LAPIC timer period this kernel arms (see main.py's
# calibrate_and_start_timer(VECTOR_TIMER, 500)) -- SYS_POLL's timeout
# and SYS_NANOSLEEP's requested duration are both real time units, but
# idt.py's tick_count only advances once per this many ms, so either
# has to be rounded up to a whole number of ticks. Coarse (up to
# ~500ms late), but that only means a wait finishes a bit later than
# asked, never that it hangs forever -- consistent with everything
# else this kernel stubs "honestly enough" rather than not at all.
TIMER_TICK_MS = 500
SYS_NANOSLEEP = 35
SYS_BRK = 12
SYS_GETRANDOM = 318
SYS_ARCH_PRCTL = 158
SYS_SET_TID_ADDRESS = 218
SYS_SET_ROBUST_LIST = 273
SYS_GET_ROBUST_LIST = 274
SYS_RSEQ = 334
SYS_PRLIMIT64 = 302
SYS_READLINKAT = 267
SYS_READLINK = 89
SYS_ACCESS = 21
SYS_STATX = 332
# struct statx (Linux ABI, 256 bytes) field offsets this kernel
# actually fills in -- everything else (uid/gid/timestamps/mount id/
# ...) stays zeroed, same honest-subset approach as STAT_SIZE's own
# fields above.
STATX_SIZE = 256
STX_MASK_OFFSET = 0
STX_BLKSIZE_OFFSET = 4
STX_NLINK_OFFSET = 16
STX_MODE_OFFSET = 28
STX_SIZE_OFFSET = 40
# STATX_BASIC_STATS: every field this reports is one of the "basic"
# ones (type/mode/nlink/size/blksize, ...) -- setting exactly this
# bitmask in stx_mask is what tells a caller checking specific bits
# (rather than just trusting every field) that these are honestly
# filled in, not left as unrequested zeros.
STATX_BASIC_STATS = 0x7FF
SYS_NEWFSTATAT = 262
SYS_GETRESUID = 118
SYS_GETRESGID = 120
SYS_GETUID = 102
SYS_GETGID = 104
SYS_GETEUID = 107
SYS_GETEGID = 108
SYS_GETTIMEOFDAY = 96
SYS_CLOCK_GETTIME = 228
SYS_UNAME = 63
# struct utsname (Linux ABI): six fixed 65-byte fields. sysname="linux"
# (lowercase -- _char_to_ascii can't encode 'L', and glibc/bash only
# ever compare it case-sensitively against "Linux" for OS detection
# logic that doesn't apply here anyway) is the one field callers might
# actually branch on; the rest are just plausible, honestly-fake
# values for a kernel that isn't really any of these things.
UTSNAME_FIELD_SIZE = 65
SYS_GETCWD = 79
SYS_GETPID = 39
SYS_GETPPID = 110
SYS_GETPGRP = 111
SYS_SETPGID = 109
SYS_GETPGID = 121
SYS_DUP = 32
SYS_DUP2 = 33
SYS_FCNTL = 72
F_DUPFD = 0
F_GETFD = 1
F_SETFD = 2
F_GETFL = 3
F_SETFL = 4
F_DUPFD_CLOEXEC = 1030
# fd_addr_table sentinels for a real fd (>=3, via _alloc_fd()) that
# dup()/dup2() made an alias of stdin or the console, rather than a
# real initramfs file -- guaranteed distinct from a real file's addr
# (an HHDM-mapped physical address, always far larger than 2) and from
# 0 (a free slot).
FD_ALIAS_STDIN = 1
FD_ALIAS_CONSOLE = 2

# fd_shared_from_table: per-fd-slot, indexed same as fd_addr_table.
# 0 means this fd owns its own offset (fd_offset_table[slot] is the
# real offset). Non-zero means "this fd is a dup; the real offset
# lives in fd_offset_table[source_fd_slot]" -- every read/write on
# this fd goes through that slot instead, so dup'd fds truly share
# one open-file-description offset, matching POSIX semantics.
fd_shared_from_table = 0

# fd_refcount_table: per-SOURCE-fd-slot reference count. Starts at 1
# when the file is first opened (SYS_OPENAT). Each dup/dup2 that
# creates a new alias increments it; each close decrements it. When
# the source fd's refcount hits 0, its fd_offset_table entry is
# freed (reset to 0). Dangling dups (shared_from pointing at a
# closed source) are harmless: reads/writes on them return EBADF.
fd_refcount_table = 0
# Fake uid/gid this kernel reports everywhere identity is asked about
# -- root (0), the simplest answer for a kernel with no real user
# accounts, permission bits, or multi-user concept at all.
FAKE_UID = 0
SYS_CLOSE = 3
SYS_STAT = 4
SYS_LSTAT = 6
SYS_OPENAT = 257
SYS_MPROTECT = 10
SYS_FSTAT = 5
SYS_IOCTL = 16
SYS_EXIT = 60
SYS_EXIT_GROUP = 231
SYS_CLONE = 56
SYS_FORK = 57
SYS_EXECVE = 59
SYS_WAIT4 = 61
SYS_RT_SIGPROCMASK = 14
SYS_RT_SIGACTION = 13
# sizeof(struct kernel_sigaction): handler + flags + restorer + an
# 8-byte sigset_t -- the real x86-64 kernel ABI layout glibc's own
# rt_sigaction() wrapper reads/writes, not the smaller libc-facing
# struct sigaction.
SIGACTION_SIZE = 32
SYS_WRITEV = 20
SYS_MMAP = 9
SYS_MUNMAP = 11
IOVEC_SIZE = 16
MAP_ANONYMOUS = 0x20
# A fixed, hand-picked base for anonymous mmap() -- well clear of the
# ELF image (~0x400000-0x800000ish), the heap (brk, growing up from
# wherever the image ends), and the fixed user stack (0x700000-
# 0x720000) -- bump-allocated forward from here, same "never frees,
# never reuses" philosophy as brk()'s own page mapping. Real mmap()
# lets the kernel place anonymous mappings anywhere; a single fixed
# region is simplest for a kernel that has no general VMA bookkeeping
# to place them more cleverly.
MMAP_VADDR_BASE = 0x10000000

# CLONE_VM (share the caller's address space, i.e. a real pthread-style
# thread rather than a separate process) -- the one clone() flag this
# kernel actively rejects; everything fork() itself asks for (see
# glibc's __fork -> _Fork, confirmed by disassembling a real binary
# calling fork(): flags = SIGCHLD | CLONE_CHILD_SETTID |
# CLONE_CHILD_CLEARTID, all fine here) creates a genuinely separate
# address space, which duplicate_address_space() (see process.py)
# already handles.
CLONE_VM = 0x100
EAGAIN = 11
ECHILD = 10
ENOEXEC = 8

# TCGETS2 -- what this specific glibc build's isatty()/tcgetattr() path
# actually issues (confirmed via the per-syscall arg dump: request
# 0x802c542a). Real Linux computes this via _IOR('T', 0x2A,
# struct termios2): direction=READ(2)<<30 | size(0x2C)<<16 |
# type('T'=0x54)<<8 | nr(0x2A).
TCGETS2 = 0x802C542A
# struct termios2 field offsets this kernel fills in -- c_iflag/c_oflag/
# c_cflag/c_lflag (4 bytes each), then c_line/c_cc/etc, none of which
# anything here reads back (see the comment above ENOTTY's old use for
# why: isatty() only checks the ioctl's *return value*, not the struct
# contents) -- but c_lflag is filled in anyway since a real terminal's
# tcgetattr() would report exactly this canonical/echoing mode, and it
# costs nothing to be honest here instead of just zeroing the struct.
TERMIOS2_SIZE = 44
C_LFLAG_OFFSET = 12
ISIG = 1
ICANON = 2
ECHO = 8

# TCSETS2/TCSETSW2/TCSETSF2 -- the "set" counterparts to TCGETS2 above
# (real Linux: _IOW('T', 0x2B/0x2C/0x2D, struct termios2), i.e. the same
# direction|size|type bits with WRITE(1)<<30 instead of READ(2)<<30, and
# nr one/two/three past TCGETS2's 0x2A). This is what a program's own
# tcsetattr(fd, TCSANOW/TCSADRAIN/TCSAFLUSH, &raw) call turns into --
# treated identically here since there's no separate pending-input queue
# to drain or flush differently between the three: this kernel only has
# the one immediate keyboard-IRQ path (see ps2.py's tty_input_char()),
# not a real input queue a "flush" could discard anything from.
TCSETS2 = 0x402C542B
TCSETSW2 = 0x402C542C
TCSETSF2 = 0x402C542D

# The *original* (non-"2") termios ioctls -- plain fixed numbers, not
# computed via the _IOC direction|size|type|nr encoding the "2" family
# above uses. glibc's tcgetattr()/tcsetattr() prefer the termios2 ioctls
# (TCGETS2/TCSETS2, for their extra out-of-band baud rate precision),
# but musl's -- confirmed via boot: busybox vi's own tcsetattr() never
# actually cleared ICANON/ECHO under only the termios2 handling below,
# because it was never issuing that request at all -- use these older
# ones instead, against the plain (smaller, no baud rate fields) kernel
# struct termios: c_iflag/c_oflag/c_cflag/c_lflag (4 bytes each, same
# order and same offsets as termios2's first 16 bytes) then c_line (1
# byte) and c_cc[19] (19 bytes) = 36 bytes total, not termios2's 44.
TCGETS = 0x5401
TCSETS = 0x5402
TCSETSW = 0x5403
TCSETSF = 0x5404
TERMIOS_SIZE = 36

# There's no real tty *configuration* layer here (tcsetattr()/raw mode
# aren't implemented -- ps2.py's canonical-line-buffering-with-echo is
# unconditional, not something a program can turn off), but this
# console genuinely is an interactive terminal now that ps2.py exists,
# and glibc's own C-standard-mandated behavior specifically depends on
# knowing that: reading from an interactive stdin is supposed to flush
# pending stdout first (so a prompt printed right before a blocking
# read actually shows up before the read blocks, rather than sitting in
# stdio's buffer indefinitely) -- claiming ENOTTY here suppressed that
# flush entirely, which is what made a program's own prompt appear to
# print *after* the user had already started typing.
ENOTTY = 25

# struct stat (x86-64 Linux ABI, 144 bytes) field offsets this kernel
# actually fills in for SYS_FSTAT -- everything else stays zeroed.
ST_NLINK_OFFSET = 16
ST_MODE_OFFSET = 24
ST_SIZE_OFFSET = 48
ST_BLKSIZE_OFFSET = 56
STAT_SIZE = 144
# S_IFCHR (character device) -- the honest answer for fd 1/2 here (this
# console), and also what makes glibc's stdio pick line-buffering for
# printf the same way it would for a real terminal.
S_IFCHR = 0x2000
# S_IFREG (regular file) -- what SYS_FSTAT reports for a fd opened
# against a real initramfs entry (see SYS_OPENAT), so tools that check
# "is this a regular file, and how big" (bash's own source/rcfile
# reads included) get an honest answer instead of the char-device fake
# every other fd here reports.
S_IFREG = 0x8000
# S_IFDIR (directory) -- what SYS_OPENAT/SYS_NEWFSTATAT/SYS_FSTAT report
# for an initramfs entry whose own cpio mode has the directory bit set
# (see cpio.py's CpioFile.mode, now that find_file() reads the real
# mode field instead of the c_uid field a stale offset used to alias).
S_IFDIR = 0x4000
S_IFMT = 0xF000
EBADF = 9
EMFILE = 24
ENOTDIR = 20
SYS_GETDENTS64 = 217
# struct linux_dirent64 (x86-64 Linux ABI) field offsets: d_ino(8) +
# d_off(8) + d_reclen(2) + d_type(1) + d_name[] (NUL-terminated,
# variable length) starting at 19.
DIRENT_NAME_OFFSET = 19
DT_UNKNOWN = 0
DT_DIR = 4
DT_REG = 8
# ioctl(TIOCGWINSZ) -- struct winsize {ws_row; ws_col; ws_xpixel;
# ws_ypixel;} (4 uint16_t fields, 8 bytes). Real Linux computes this
# request code via _IOR('t', 104, struct winsize): READ(2)<<30 |
# size(8)<<16 | type('t'=0x74)<<8 | nr(104=0x68).
TIOCGWINSZ = 0x5413
WINSIZE_SIZE = 8
# A plausible fixed terminal size -- there's no real resize event
# machinery here, just an honest, unsurprising default so tools that
# lay out columned output (like ls) get *some* answer instead of
# falling back to whatever they guess when this ioctl fails.
CONSOLE_ROWS = 24
CONSOLE_COLS = 80

# RLIM_INFINITY: the one real value SYS_PRLIMIT64 writes back through
# old_limit (when asked for one) -- both fields of a 16-byte struct
# rlimit {rlim_cur; rlim_max;}. Leaving old_limit untouched (a plain
# "return 0" no-op) looked harmless but wasn't: whatever garbage
# already happened to be on the caller's own stack at that address got
# read back as this process's "current stack rlimit" by glibc, which
# used it for real downstream bookkeeping -- corrupting a computed
# pointer used later during exit-handler dispatch (call *rax jumping to
# address 4). "Unlimited" is honest for a kernel with no resource-limit
# enforcement at all, and is what glibc otherwise treats as a sane
# default in the low-level codepaths that read this back.
RLIM_INFINITY = 0xFFFFFFFFFFFFFFFF

# There's no real filesystem/VFS here -- any path readlinkat() is asked
# to resolve (e.g. glibc probing "/proc/self/exe") genuinely doesn't
# exist, so the honest answer is the real errno for that, not a faked
# success glibc would then read link-target bytes out of nowhere for.
ENOENT = 2

# The real errno for a bad pointer -- specifically, a NULL path
# argument. Real programs do occasionally build one (e.g. from an
# unset $HOME with no NULL check before handing it to stat()/open()),
# and real Linux just returns this rather than doing anything with the
# pointer; every syscall below that reads a path string checks for
# this before ever calling _read_user_cstring(), whose own
# peek8(0)-on-a-NULL-addr would otherwise fault inside the kernel with
# nothing to recover it -- confirmed via boot: busybox vi triggered
# exactly that hang before this check existed.
EFAULT = 14

# Extended attributes don't exist here (no real filesystem to store
# them in) -- ENOTSUP is the honest, standard answer a real xattr-less
# filesystem gives, and it's what makes `ls -l` (checking for ACLs, via
# GNU coreutils' acl-internal.c) quietly skip the "+" indicator instead
# of erroring out.
ENOTSUP = 95
SYS_LGETXATTR = 192
SYS_LLISTXATTR = 195

# Real Linux updates a registered rseq struct's cpu_id on every context
# switch/migration, letting libc's fast paths detect an interrupted
# critical section and retry -- nothing here does that bookkeeping.
# Returning -ENOSYS (not success) is what makes this a safe no-op: it's
# the exact response a kernel genuinely built without CONFIG_RSEQ gives,
# and glibc already handles that by disabling its own rseq usage and
# falling back, rather than assuming a registration that never actually
# gets serviced.
ENOSYS = 38

# There's no clone()/multi-threaded-process support here, so
# set_tid_address()'s pointer (for CLONE_CHILD_CLEARTID -- clearing and
# futex-waking it on thread exit) has nothing to actually do; only its
# return value (the caller's own thread ID) matters to glibc's startup,
# which just stashes it. One process, so one fixed fake TID is fine.
FAKE_TID = 1

PAGE_SIZE = 4096

IA32_EFER = 0xC0000080
IA32_STAR = 0xC0000081
IA32_LSTAR = 0xC0000082
IA32_FMASK = 0xC0000084
IA32_FS_BASE = 0xC0000100
IA32_GS_BASE = 0xC0000101

EFER_SCE = 1

# arch_prctl(2) codes -- glibc's static CRT calls ARCH_SET_FS very early
# (TLS_INIT_TP, before main()) to point %fs at its just-built TLS block;
# everything from the stack-protector canary (read via %fs:0x28) to
# errno onward depends on that being set correctly.
ARCH_SET_GS = 0x1001
ARCH_SET_FS = 0x1002
ARCH_GET_FS = 0x1003
ARCH_GET_GS = 0x1004

brk_pa = PageAllocator()
# brk_cur_table/brk_mapped_end_table: each thread's own heap cursor and
# how far it's actually mapped -- per-thread (real allocation happens
# in init_kernel_vm() below, same reason fd_addr_table's does), not the
# single globals these used to be. A forked child briefly has its own
# heap needs too (glibc's static CRT calls brk() well before
# execve()), and with one shared brk_cur/brk_mapped_end, the child's
# own growth advanced state the *parent* would later read back as
# "already mapped" -- for memory that only ever existed in the child's
# own, separate address space. Real symptom, not theoretical: glibc's
# malloc reported "FATAL: out of memory" for the parent (bash) after a
# forked child (also bash, briefly, pre-exec) grew its own heap first,
# confirmed via boot. Same bug class this session already found for
# brk_vm/the syscall-entry stack/FS_BASE -- one more piece of "this
# thread's own state" that still needs its own table.
brk_cur_table = 0
brk_mapped_end_table = 0
mmap_next = MMAP_VADDR_BASE

# kernel_vm: the ONE kernel address space main.py built at boot --
# every process's own PML4 copies its upper half wholesale (see
# process.py's new_address_space()), so fork() (SYS_CLONE) and execve()
# (SYS_EXECVE) both need a real reference to it, not just the physical
# CR3 value main.py already threads through init_threads(). Same
# real-placeholder-instance trick as brk_pa above; replaced for real
# by init_kernel_vm() before any process could possibly fork/exec.
kernel_vm = VirtualMemory(brk_pa)

def init_kernel_vm(pa, vm):
    # pa (not just vm) matters here: this is called from main.py before
    # init_brk() ever runs (init_brk needs elf_info.brk_base, which
    # doesn't exist until well after this point), so brk_pa is still
    # its own placeholder PageAllocator() instance, not main.py's real
    # one -- a *separate* bump allocator counting physical pages from
    # the same base address, with no way to know what the real one has
    # already handed out. The five alloc_page() calls below used to go
    # through that placeholder, silently handing out physical pages the
    # real allocator would later *also* consider free and give out for
    # something else entirely -- confirmed by boot: one of these landed
    # on the exact physical page backing the TSS's own page-table leaf,
    # and something writing through fd_addr_table later corrupted that
    # PTE in place, so any real interrupt arriving while a user process
    # was active (needing to read TSS.RSP0 through its own page tables)
    # page-faulted on a garbage, flag-less "entry" -- intermittent only
    # because it depended on an interrupt actually landing during that
    # specific window. Setting brk_pa to the real allocator here, before
    # any of these run, is what closes the collision for good.
    global brk_pa
    global kernel_vm
    global fd_addr_table
    global fd_size_table
    global fd_offset_table
    global fd_is_dir_table
    global fd_shared_from_table
    global fd_refcount_table
    global brk_cur_table
    global brk_mapped_end_table
    brk_pa = pa
    kernel_vm = vm
    fd_addr_table = brk_pa.alloc_page()
    fd_size_table = brk_pa.alloc_page()
    fd_offset_table = brk_pa.alloc_page()
    fd_is_dir_table = brk_pa.alloc_page()
    fd_shared_from_table = brk_pa.alloc_page()
    fd_refcount_table = brk_pa.alloc_page()
    brk_cur_table = brk_pa.alloc_page()
    brk_mapped_end_table = brk_pa.alloc_page()

# Real, read-only files backed by initramfs content -- fd 0-2 stay the
# fixed stdin/console special cases (see SYS_READ/SYS_WRITE), so real
# fds handed out here start at 3. Per-thread (MAX_FDS slots per tid,
# indexed by get_current_tid()*MAX_FDS + slot), not one global table:
# this kernel spent this whole session's fork() work finding bugs from
# exactly this mistake (brk_vm, the syscall-entry stack, FS_BASE) --
# no reason to repeat it for the first genuinely per-process resource
# added since. addr==0 marks a free slot (a real cpio file's content
# address is never 0, the same not-found convention cpio.py's own
# CpioFile already uses) -- fd_size_table/fd_offset_table at that slot
# are meaningless until addr is set.
#
# Known, deliberate gap: a fork()ed child starts with none of its
# parent's open file-backed fds (only the ever-present 0/1/2 "inherit"
# right now, since those aren't per-thread state to begin with) --
# real fork() semantics would duplicate the whole table. Not needed
# for an interactive shell reading its own rcfiles before ever
# fork()ing to run a command; a real documented follow-up if a script
# ever opens a file *then* forks and expects the child to see it.
MAX_FDS = 16
# Real allocation happens in init_kernel_vm() below (a plain 0 here
# can't itself call brk_pa.alloc_page() -- pyck's global type inference
# needs a function body, not a bare module-level method call, to
# resolve what that returns).
fd_addr_table = 0
fd_size_table = 0
fd_offset_table = 0
# 1 for a directory fd (fd_addr_table holds that entry's own cpio
# *header* address, not a data address, for SYS_GETDENTS64 to re-walk
# from -- see SYS_OPENAT below), 0 for a regular file.
fd_is_dir_table = 0

def _fd_slot(fd: int):
    # Userspace-visible fd -> this table's raw slot index, or -1 for
    # an fd this table could never have handed out (0-2, negative, or
    # past MAX_FDS) -- callers still need to separately check
    # fd_addr_table[slot] != 0 for "was this fd actually ever opened".
    if fd < 3:
        return -1
    offset = fd - 3
    if offset >= MAX_FDS:
        return -1
    return get_current_tid() * MAX_FDS + offset

def _alloc_fd():
    # Returns a fresh userspace fd number (3-based) for the current
    # thread, or -1 if its MAX_FDS slots are all in use.
    base = get_current_tid() * MAX_FDS
    i = 0
    while i < MAX_FDS:
        if peek64(fd_addr_table + (base + i) * 8) == 0:
            return i + 3
        i = i + 1
    return -1

def _fd_is_open(fd: int):
    if fd == 0 or fd == 1 or fd == 2:
        return 1
    slot = _fd_slot(fd)
    if slot == -1:
        return 0
    return peek64(fd_addr_table + slot * 8) != 0

def _copy_fd_state(oldfd: int, dest_slot: int):
    # Makes dest_slot (a real fd_*_table slot, from _fd_slot()) an
    # independent copy of whatever oldfd currently refers to -- used by
    # both SYS_DUP and SYS_DUP2. The new fd shares the same underlying
    # open-file-description offset as oldfd (POSIX dup semantics):
    # fd_shared_from_table[dest_slot] is set to the SOURCE fd's slot,
    # and fd_refcount_table[source] is incremented. If oldfd is itself
    # a dup, we chase shared_from to find the real source. Returns 1 on
    # success, 0 if oldfd isn't a valid fd (caller reports EBADF).
    if oldfd == 0:
        poke64(fd_addr_table + dest_slot * 8, FD_ALIAS_STDIN)
        poke64(fd_size_table + dest_slot * 8, 0)
        poke64(fd_offset_table + dest_slot * 8, 0)
        poke64(fd_is_dir_table + dest_slot * 8, 0)
        poke64(fd_shared_from_table + dest_slot * 8, 0)
        poke64(fd_refcount_table + dest_slot * 8, 0)
        return 1
    if oldfd == 1 or oldfd == 2:
        poke64(fd_addr_table + dest_slot * 8, FD_ALIAS_CONSOLE)
        poke64(fd_size_table + dest_slot * 8, 0)
        poke64(fd_offset_table + dest_slot * 8, 0)
        poke64(fd_is_dir_table + dest_slot * 8, 0)
        poke64(fd_shared_from_table + dest_slot * 8, 0)
        poke64(fd_refcount_table + dest_slot * 8, 0)
        return 1
    old_slot = _fd_slot(oldfd)
    if old_slot == -1:
        return 0
    old_addr = peek64(fd_addr_table + old_slot * 8)
    if old_addr == 0:
        return 0
    # Chase shared_from to find the real source fd if oldfd is itself a dup
    source_slot = old_slot
    if peek64(fd_shared_from_table + old_slot * 8) != 0:
        source_slot = peek64(fd_shared_from_table + old_slot * 8)
    poke64(fd_addr_table + dest_slot * 8, old_addr)
    poke64(fd_size_table + dest_slot * 8, peek64(fd_size_table + source_slot * 8))
    poke64(fd_is_dir_table + dest_slot * 8, peek64(fd_is_dir_table + old_slot * 8))
    poke64(fd_shared_from_table + dest_slot * 8, source_slot)
    poke64(fd_refcount_table + dest_slot * 8, 0)
    # Bump refcount on the source
    rc = peek64(fd_refcount_table + source_slot * 8)
    poke64(fd_refcount_table + source_slot * 8, rc + 1)
    return 1

def _fd_resolve_offset(fd: int):
    # Returns the actual slot index where this fd's offset lives.
    # For a source fd (fd_shared_from[slot]==0), that's its own slot.
    # For a dup fd, that's the source slot it points to.
    # Returns -1 for fd 0/1/2 (console, no real offset) or bad fd.
    if fd < 3:
        return -1
    slot = _fd_slot(fd)
    if slot == -1:
        return -1
    if peek64(fd_addr_table + slot * 8) == 0:
        return -1
    shared = peek64(fd_shared_from_table + slot * 8)
    if shared != 0:
        return shared
    return slot

def _fd_get_offset(fd: int):
    # Read the current offset for an fd, following shared_from indirection.
    slot = _fd_resolve_offset(fd)
    if slot == -1:
        return 0
    return peek64(fd_offset_table + slot * 8)

def _fd_set_offset(fd: int, new_offset: int):
    # Write a new offset for an fd, following shared_from indirection.
    slot = _fd_resolve_offset(fd)
    if slot != -1:
        poke64(fd_offset_table + slot * 8, new_offset)

def _fd_close(fd: int):
    # Close an fd: clears its addr slot, and if it's the SOURCE fd
    # (owns the offset), decrements refcount. If refcount hits 0,
    # the offset slot is freed. If this fd is a dup, the source's
    # refcount is decremented instead. Returns 1 on success.
    if fd < 3:
        return 1
    slot = _fd_slot(fd)
    if slot == -1:
        return 1
    if peek64(fd_addr_table + slot * 8) == 0:
        return 1
    shared = peek64(fd_shared_from_table + slot * 8)
    if shared != 0:
        # This fd is a dup: decrement source's refcount
        rc = peek64(fd_refcount_table + shared * 8)
        if rc > 0:
            poke64(fd_refcount_table + shared * 8, rc - 1)
        poke64(fd_shared_from_table + slot * 8, 0)
    else:
        # This fd is the source: decrement its own refcount
        rc = peek64(fd_refcount_table + slot * 8)
        if rc > 0:
            poke64(fd_refcount_table + slot * 8, rc - 1)
            rc = rc - 1
        if rc == 0:
            poke64(fd_offset_table + slot * 8, 0)
    poke64(fd_addr_table + slot * 8, 0)
    poke64(fd_size_table + slot * 8, 0)
    poke64(fd_is_dir_table + slot * 8, 0)
    return 1

# xorshift32 PRNG backing getrandom() -- there's no real hardware
# entropy source hooked up here, only a deterministic generator (same
# honest tradeoff as setup_user_stack()'s fixed AT_RANDOM bytes). Seed
# just needs to be nonzero for xorshift32 to ever produce anything.
_rng_state = 0x2545F491

def _rng_next():
    global _rng_state
    x = _rng_state
    x = (x ^ (x << 13)) & 0xFFFFFFFF
    x = x ^ (x >> 17)
    x = (x ^ (x << 5)) & 0xFFFFFFFF
    _rng_state = x
    return x

def init_brk(pa, vm, base: int, tid: int):
    # vm is unused -- kept as a parameter since every call site already
    # has a natural "this process's own address space" value on hand
    # (main.py's process_vm at boot, SYS_EXECVE's new_vm) and passing
    # it documents *which* address space base belongs to, even though
    # _brk_grow() below always re-derives the actually-correct one from
    # whatever CR3 is live rather than trusting a stored reference.
    global brk_pa
    global brk_cur_table
    global brk_mapped_end_table
    brk_pa = pa
    poke64(brk_cur_table + tid * 8, base)
    poke64(brk_mapped_end_table + tid * 8, base)

def _brk_grow(new_brk: int):
    tid = get_current_tid()
    mapped_end = peek64(brk_mapped_end_table + tid * 8)
    if new_brk > mapped_end:
        # A fresh VirtualMemory(brk_pa) always targets whatever CR3 is
        # actually loaded right now (see vmm.py's constructor) -- the
        # calling thread's own, since nothing between a syscall firing
        # and this code running ever changes it. Building one here
        # instead of trusting a stored reference is what closed the
        # brk_vm-staleness bug earlier this session; brk_cur_table/
        # brk_mapped_end_table above close the matching bug for *whose*
        # cursor this call should even be advancing.
        cur_vm = VirtualMemory(brk_pa)
        new_end = new_brk
        rem = new_end % PAGE_SIZE
        if rem != 0:
            new_end = new_end + (PAGE_SIZE - rem)
        page = mapped_end
        while page < new_end:
            phys_page = brk_pa.alloc_page()
            phys = phys_page - cur_vm.hhdm
            cur_vm.map(page, phys, PAGE_WRITABLE, True)
            raw_memzero(phys_page, PAGE_SIZE)
            page = page + PAGE_SIZE
        poke64(brk_mapped_end_table + tid * 8, new_end)
    poke64(brk_cur_table + tid * 8, new_brk)

def init_syscalls():
    star = (KERNEL_CODE_SEL * (2 ** 32)) | (SYSRET_BASE_SEL * (2 ** 48))
    wrmsr(IA32_STAR, star)
    wrmsr(IA32_LSTAR, _SYSCALL_ENTRY_ADDR())
    wrmsr(IA32_FMASK, 1 * (2 ** 9))
    efer = rdmsr(IA32_EFER)
    wrmsr(IA32_EFER, efer | EFER_SCE)
    print_satanic("Syscalls enabled")

def _read_user_string(addr: int, count: int):
    s = ""
    i = 0
    while i < count:
        s = s + chr(peek8(addr + i))
        i = i + 1
    return s

def _read_user_cstring(addr: int):
    # Same idea as _read_user_string(), but for a NUL-terminated string
    # (a path, for SYS_EXECVE) whose length isn't known up front instead
    # of one with an explicit byte count (a write() buffer).
    s = ""
    i = 0
    while peek8(addr + i) != 0:
        s = s + chr(peek8(addr + i))
        i = i + 1
    return s

def _normalize_lookup_name(path: str):
    # The initramfs is a flat cpio namespace whose own entries are
    # named "." (root), "bin", "bin/sh", ... (see build_initramfs()
    # in PythonOS/build.py, and cpio.py's find_file()) -- an absolute
    # path's leading "/" is stripped to match, and "/" itself (which
    # strips down to "", not a real entry name) specifically becomes
    # "." to find the root directory's own entry.
    name = path
    if len(name) > 0 and name[0] == "/":
        name = name[1:]
    if len(name) > 1 and name[len(name) - 1] == "/":
        name = name[0:len(name) - 1]
    if name == "":
        name = "."
    return name

def _has_slash(s: str):
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "/":
            return 1
        i = i + 1
    return 0

def _write_cstring(addr: int, s: str):
    # SYS_UNAME's struct utsname fields are the only thing here that
    # writes a string *out* to userspace -- everything else so far only
    # ever reads one. _char_to_ascii (see process.py) already covers
    # everything these fixed, hand-picked strings use.
    i = 0
    while i < len(s):
        poke8(addr + i, _char_to_ascii(s[i]))
        i = i + 1
    poke8(addr + i, 0)

def on_syscall(regs: int):
    # Flat guard-clause dispatch, not nested if/else: this list only
    # ever grows (one syscall at a time, as real glibc binaries turn out
    # to need them -- see this file's own history), and nesting one more
    # level per addition stopped being readable a few syscalls ago.
    global mmap_next
    num = peek64(regs + REG_RAX)

    if num == SYS_READ:
        fd = peek64(regs + REG_RDI)
        buf = peek64(regs + REG_RSI)
        count = peek64(regs + REG_RDX)
        slot = -1
        addr = 0
        if fd >= 3:
            slot = _fd_slot(fd)
            if slot != -1:
                addr = peek64(fd_addr_table + slot * 8)
        if fd == 0 or addr == FD_ALIAS_STDIN:
            # Block until the keyboard ISR has a full line ready --
            # mark_current_blocked() only sets the flag; schedule()
            # actually yields the CPU (to the idle thread, the only
            # other one there is). This call doesn't return until some
            # *later* schedule() call (from a future timer tick, once
            # ps2.py's on_keyboard_irq() has unblocked this thread
            # again) picks this thread back up -- at which point
            # execution resumes right here, mid-loop, exactly like
            # resuming after any other preemption.
            while stdin_available() == 0:
                mark_current_blocked()
                schedule()
            n = stdin_read(buf, count)
            poke64(regs + REG_RAX, n)
        elif addr == FD_ALIAS_CONSOLE:
            # Reading from a dup()'d copy of stdout/stderr -- nothing
            # to read, honestly EOF.
            poke64(regs + REG_RAX, 0)
        else:
            if addr == 0:
                poke64(regs + REG_RAX, unsigned64(0 - EBADF))
            else:
                size = peek64(fd_size_table + slot * 8)
                off = _fd_get_offset(fd)
                remaining = size - off
                n = count
                if n > remaining:
                    n = remaining
                if n > 0:
                    raw_memcpy(buf, addr + off, n)
                    _fd_set_offset(fd, off + n)
                poke64(regs + REG_RAX, n)
        return

    if num == SYS_POLL:
        # int poll(struct pollfd *fds, nfds_t nfds, int timeout) --
        # busybox's vi (see kernel/usb_hid.py's own build.py entry) uses
        # this to wait for the next keypress with a short timeout, to
        # tell a standalone Escape from the start of a longer escape
        # sequence. This kernel's keyboard input (ps2.py/usb_hid.py)
        # never actually emits multi-byte sequences for anything, so
        # that distinction never matters here -- but honoring the
        # timeout for real (rather than just always blocking forever)
        # still matters: an infinite wait after a lone Escape press
        # would otherwise look exactly like vi hanging.
        fds = peek64(regs + REG_RDI)
        nfds = peek64(regs + REG_RSI)
        # `timeout` is a 32-bit signed int; -1 (0xFFFFFFFF once placed
        # in a 64-bit register without sign-extension) means "wait
        # forever."
        timeout_raw = peek64(regs + REG_RDX) & 0xFFFFFFFF
        infinite = timeout_raw == 0xFFFFFFFF
        ticks_needed = 0
        if infinite == 0 and timeout_raw != 0:
            ticks_needed = timeout_raw // TIMER_TICK_MS
            if timeout_raw % TIMER_TICK_MS != 0:
                ticks_needed = ticks_needed + 1
        deadline = get_tick_count() + ticks_needed
        while True:
            ready = 0
            i = 0
            while i < nfds:
                entry = fds + i * POLLFD_SIZE
                pfd = peek32(entry)
                events = peek16(entry + 4)
                revents = 0
                if pfd == 0:
                    # stdin: the only fd this kernel can ever actually
                    # block reading from (see SYS_READ above) -- really
                    # check whether a line is ready.
                    if (events & POLLIN) != 0 and stdin_available() > 0:
                        revents = POLLIN
                elif pfd != 0xFFFFFFFF:
                    # 0xFFFFFFFF (-1 as a signed int32) is poll(2)'s own
                    # "ignore this entry" marker. Every other fd here
                    # (regular initramfs files, console dups, ...) never
                    # actually blocks in SYS_READ/SYS_WRITE either, so
                    # it's honestly always ready for whatever it asked
                    # about.
                    revents = events & (POLLIN | POLLOUT)
                poke16(entry + 6, revents)
                if revents != 0:
                    ready = ready + 1
                i = i + 1
            if ready > 0 or timeout_raw == 0:
                clear_current_poll_deadline()
                poke64(regs + REG_RAX, ready)
                return
            if infinite == 0 and get_tick_count() >= deadline:
                clear_current_poll_deadline()
                poke64(regs + REG_RAX, 0)
                return
            if infinite == 0:
                set_current_poll_deadline(deadline)
            mark_current_blocked()
            schedule()

    if num == SYS_NANOSLEEP:
        # int nanosleep(const struct timespec *req, struct timespec
        # *rem) -- confirmed via boot: busybox vi calls this (likely
        # as part of its own read-with-timeout input loop) right after
        # drawing its first screen, and this kernel's default
        # "Unhandled syscall: N, killing process" response to any
        # syscall with no handler killed it outright the instant it
        # tried -- every keystroke sent afterward just landed on a
        # dead process, silently local-echoed with nothing left to
        # actually edit. struct timespec is {tv_sec, tv_nsec}, 8 bytes
        # each; converted to whole ticks the exact same way SYS_POLL's
        # timeout is (see TIMER_TICK_MS), then blocked the same way,
        # reusing the very same tick-deadline machinery (deliberately
        # -- a sleeping thread and a timed-out poll() are the same
        # "wake this thread up once tick_count reaches N" problem).
        req = peek64(regs + REG_RDI)
        rem = peek64(regs + REG_RSI)
        total_ms = 0
        if req != 0:
            sec = peek64(req)
            nsec = peek64(req + 8)
            total_ms = sec * 1000 + nsec // 1000000
        ticks_needed = 0
        if total_ms > 0:
            ticks_needed = total_ms // TIMER_TICK_MS
            if total_ms % TIMER_TICK_MS != 0:
                ticks_needed = ticks_needed + 1
        if ticks_needed > 0:
            deadline = get_tick_count() + ticks_needed
            while get_tick_count() < deadline:
                set_current_poll_deadline(deadline)
                mark_current_blocked()
                schedule()
            clear_current_poll_deadline()
        if rem != 0:
            poke64(rem, 0)
            poke64(rem + 8, 0)
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_WRITE:
        fd = peek64(regs + REG_RDI)
        buf = peek64(regs + REG_RSI)
        count = peek64(regs + REG_RDX)
        is_console = fd == 1 or fd == 2
        if fd >= 3:
            slot = _fd_slot(fd)
            if slot != -1 and peek64(fd_addr_table + slot * 8) == FD_ALIAS_CONSOLE:
                is_console = True
        if is_console:
            # Byte-for-byte echo of exactly what the program wrote --
            # NOT print(), which unconditionally appends '\n' (see
            # arch/x86_64_graphical/graphics.cpp's py_print()). That
            # stray newline after every write is what made each
            # character busybox ash echoes on a keystroke (ash runs its
            # own line editor with ECHO off, so the shell -- not this
            # kernel -- does the per-key echo; see ps2.py's
            # tty_input_char() raw branch) land on its own line: type
            # 'a', get "a" then a fresh line. print_char() writes one
            # raw byte to both the serial port and the framebuffer
            # console, so ANSI escapes (clear/vi/colored ls) still reach
            # graphics.cpp's ansi_feed() intact -- round-tripping
            # through _char_to_ascii() would turn ESC into '?'.
            # peek8 on the source buffer also skips the chr()/ord()
            # round trip entirely (PyCk has chr() but no ord()), and is
            # cheaper than _read_user_string()'s O(n^2) string concat.
            i = 0
            while i < count:
                print_char(peek8(buf + i))
                i = i + 1
        poke64(regs + REG_RAX, count)
        return

    if num == SYS_BRK:
        req = peek64(regs + REG_RDI)
        tid = get_current_tid()
        cur = peek64(brk_cur_table + tid * 8)
        if req != 0:
            if req > cur:
                _brk_grow(req)
            else:
                poke64(brk_cur_table + tid * 8, req)
            cur = peek64(brk_cur_table + tid * 8)
        poke64(regs + REG_RAX, cur)
        return

    if num == SYS_GETRANDOM:
        buf = peek64(regs + REG_RDI)
        buflen = peek64(regs + REG_RSI)
        i = 0
        while i < buflen:
            poke8(buf + i, _rng_next() & 0xFF)
            i = i + 1
        poke64(regs + REG_RAX, buflen)
        return

    if num == SYS_ARCH_PRCTL:
        code = peek64(regs + REG_RDI)
        addr = peek64(regs + REG_RSI)
        if code == ARCH_SET_FS:
            wrmsr(IA32_FS_BASE, addr)
            poke64(regs + REG_RAX, 0)
            return
        if code == ARCH_SET_GS:
            wrmsr(IA32_GS_BASE, addr)
            poke64(regs + REG_RAX, 0)
            return
        if code == ARCH_GET_FS:
            poke64(addr, rdmsr(IA32_FS_BASE))
            poke64(regs + REG_RAX, 0)
            return
        if code == ARCH_GET_GS:
            poke64(addr, rdmsr(IA32_GS_BASE))
            poke64(regs + REG_RAX, 0)
            return
        print_satanic("Unhandled arch_prctl code, killing process:")
        print_satanic(int_to_str(code))
        mark_current_exited(1)
        schedule()
        return

    if num == SYS_SET_TID_ADDRESS:
        poke64(regs + REG_RAX, FAKE_TID)
        return

    if num == SYS_SET_ROBUST_LIST:
        # No-op: nothing here ever inspects a thread's robust-mutex
        # list (only relevant on abnormal exit while holding a
        # futex-based lock, which doesn't apply without real clone()d
        # threads).
        _debugcon_str("syscall(SET_ROBUST_LIST): stub")
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_GET_ROBUST_LIST:
        _debugcon_str("syscall(GET_ROBUST_LIST): stub")
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_RSEQ:
        _debugcon_str("syscall(RSEQ): stub")
        poke64(regs + REG_RAX, unsigned64(0 - ENOSYS))
        return

    if num == SYS_PRLIMIT64:
        old_limit = peek64(regs + REG_R10)
        if old_limit != 0:
            poke64(old_limit, RLIM_INFINITY)
            poke64(old_limit + 8, RLIM_INFINITY)
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_READLINKAT:
        _debugcon_str("syscall(READLINKAT): stub")
        poke64(regs + REG_RAX, unsigned64(0 - ENOENT))
        return

    if num == SYS_READLINK:
        # Same honest answer as SYS_READLINKAT above: no symlinks (or
        # any other filesystem metadata) exist here to resolve.
        _debugcon_str("syscall(READLINK): stub")
        poke64(regs + REG_RAX, unsigned64(0 - ENOENT))
        return

    if num == SYS_LGETXATTR:
        _debugcon_str("syscall(LGETXATTR): stub")
        poke64(regs + REG_RAX, unsigned64(0 - ENOTSUP))
        return

    if num == SYS_LLISTXATTR:
        _debugcon_str("syscall(LLISTXATTR): stub")
        poke64(regs + REG_RAX, unsigned64(0 - ENOTSUP))
        return

    if num == SYS_ACCESS:
        # No real permission model exists here (see FAKE_UID) -- the
        # only honest thing access() can check is whether the path
        # resolves at all, same initramfs lookup as SYS_OPENAT.
        path_ptr = peek64(regs + REG_RDI)
        if path_ptr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - EFAULT))
            return
        path = _read_user_cstring(path_ptr)
        lookup_name = _normalize_lookup_name(path)
        file = find_module_file(lookup_name)
        if file.addr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - ENOENT))
        else:
            poke64(regs + REG_RAX, 0)
        return

    if num == SYS_STATX:
        # int statx(int dirfd, const char *pathname, int flags,
        # unsigned mask, struct statx *statxbuf) -- dirfd/flags/mask
        # all ignored, same "every path is absolute in this flat
        # namespace, every open is honestly read-only" reasoning as
        # SYS_OPENAT/SYS_NEWFSTATAT.
        path_ptr = peek64(regs + REG_RSI)
        statxbuf = peek64(regs + REG_R8)
        if path_ptr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - EFAULT))
            return
        path = _read_user_cstring(path_ptr)
        lookup_name = _normalize_lookup_name(path)
        file = find_module_file(lookup_name)
        if file.addr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - ENOENT))
            return
        raw_memzero(statxbuf, STATX_SIZE)
        poke32(statxbuf + STX_MASK_OFFSET, STATX_BASIC_STATS)
        poke32(statxbuf + STX_BLKSIZE_OFFSET, 4096)
        poke32(statxbuf + STX_NLINK_OFFSET, 1)
        if (file.mode & S_IFMT) == S_IFDIR:
            poke16(statxbuf + STX_MODE_OFFSET, S_IFDIR | 0o550)
        else:
            poke16(statxbuf + STX_MODE_OFFSET, S_IFREG | 0o440)
        poke64(statxbuf + STX_SIZE_OFFSET, file.size)
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_OPEN:
        # int open(const char *pathname, int flags, mode_t mode) --
        # the legacy 3-arg form SYS_OPENAT superseded (no dirfd; the
        # path is always in REG_RDI here, not REG_RSI). musl still
        # emits this directly on x86_64 rather than always going
        # through openat(AT_FDCWD, ...), so real static binaries (e.g.
        # Alpine's busybox) hit this even though nothing in this
        # kernel's own code called it. Same flat-namespace lookup,
        # same ignored flags/mode, as SYS_OPENAT below -- just
        # duplicated rather than shared, same as SYS_ACCESS/SYS_STATX
        # already are.
        path_ptr = peek64(regs + REG_RDI)
        if path_ptr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - EFAULT))
            return
        path = _read_user_cstring(path_ptr)
        lookup_name = _normalize_lookup_name(path)
        file = find_module_file(lookup_name)
        if file.addr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - ENOENT))
            return
        fd = _alloc_fd()
        if fd == -1:
            poke64(regs + REG_RAX, unsigned64(0 - EMFILE))
            return
        slot = _fd_slot(fd)
        is_dir = (file.mode & S_IFMT) == S_IFDIR
        if is_dir:
            poke64(fd_addr_table + slot * 8, file.header_addr)
            poke64(fd_is_dir_table + slot * 8, 1)
        else:
            poke64(fd_addr_table + slot * 8, file.addr)
            poke64(fd_is_dir_table + slot * 8, 0)
        poke64(fd_size_table + slot * 8, file.size)
        poke64(fd_offset_table + slot * 8, 0)
        poke64(fd_shared_from_table + slot * 8, 0)
        poke64(fd_refcount_table + slot * 8, 1)
        poke64(regs + REG_RAX, fd)
        return

    if num == SYS_OPENAT:
        # dirfd (REG_RDI) is ignored: every path this kernel can
        # possibly resolve is looked up by its own full name in the
        # initramfs (see cpio.py's find_module_file(), the exact same
        # lookup SYS_EXECVE already uses) -- there's no real directory
        # tree to walk relative to, only a flat archive of names, so a
        # relative path resolves exactly the same way an absolute one
        # does. flags/mode (REG_RDX/REG_R10) are ignored too: every
        # open here is inherently read-only, since the initramfs itself
        # is never written to.
        path_ptr = peek64(regs + REG_RSI)
        if path_ptr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - EFAULT))
            return
        path = _read_user_cstring(path_ptr)
        lookup_name = _normalize_lookup_name(path)
        file = find_module_file(lookup_name)
        if file.addr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - ENOENT))
            return
        fd = _alloc_fd()
        if fd == -1:
            poke64(regs + REG_RAX, unsigned64(0 - EMFILE))
            return
        slot = _fd_slot(fd)
        is_dir = (file.mode & S_IFMT) == S_IFDIR
        if is_dir:
            # header_addr, not the data address: SYS_GETDENTS64 needs
            # this entry's own name back (via entry_name_at()) to know
            # which archive entries are its children.
            poke64(fd_addr_table + slot * 8, file.header_addr)
            poke64(fd_is_dir_table + slot * 8, 1)
        else:
            poke64(fd_addr_table + slot * 8, file.addr)
            poke64(fd_is_dir_table + slot * 8, 0)
        poke64(fd_size_table + slot * 8, file.size)
        poke64(fd_offset_table + slot * 8, 0)
        poke64(fd_shared_from_table + slot * 8, 0)
        poke64(fd_refcount_table + slot * 8, 1)
        poke64(regs + REG_RAX, fd)
        return

    if num == SYS_CLOSE:
        fd = peek64(regs + REG_RDI)
        _fd_close(fd)
        # 0/1/2, or an fd never actually opened: still a successful
        # no-op, matching how nothing here tracks whether those were
        # ever "open" to begin with.
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_STAT:
        # int stat(const char *pathname, struct stat *statbuf) -- the
        # legacy 2-arg form SYS_NEWFSTATAT superseded (no dirfd/flags;
        # path is REG_RDI and statbuf is REG_RSI here, not REG_RSI/
        # REG_RDX). musl still emits this directly on x86_64 the same
        # way it does SYS_OPEN over SYS_OPENAT -- same duplicated body
        # as SYS_NEWFSTATAT below, just shifted argument registers.
        path_ptr = peek64(regs + REG_RDI)
        statbuf = peek64(regs + REG_RSI)
        if path_ptr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - EFAULT))
            return
        path = _read_user_cstring(path_ptr)
        lookup_name = _normalize_lookup_name(path)
        file = find_module_file(lookup_name)
        if file.addr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - ENOENT))
            return
        raw_memzero(statbuf, STAT_SIZE)
        poke64(statbuf + ST_NLINK_OFFSET, 1)
        if (file.mode & S_IFMT) == S_IFDIR:
            poke32(statbuf + ST_MODE_OFFSET, S_IFDIR | 0o550)
        else:
            poke32(statbuf + ST_MODE_OFFSET, S_IFREG | 0o440)
        poke64(statbuf + ST_SIZE_OFFSET, file.size)
        poke64(statbuf + ST_BLKSIZE_OFFSET, 4096)
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_LSTAT:
        # int lstat(const char *pathname, struct stat *statbuf) --
        # same args as SYS_STAT (path in REG_RDI, statbuf in REG_RSI);
        # the only real difference is that lstat() reports the link
        # itself rather than following a trailing symlink. This
        # initramfs has no symlinks at all (see SYS_READLINK below,
        # which honestly answers "not a symlink" for every path), so
        # there's no final component for stat() and lstat() to ever
        # disagree about -- identical body to SYS_STAT above.
        path_ptr = peek64(regs + REG_RDI)
        statbuf = peek64(regs + REG_RSI)
        if path_ptr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - EFAULT))
            return
        path = _read_user_cstring(path_ptr)
        lookup_name = _normalize_lookup_name(path)
        file = find_module_file(lookup_name)
        if file.addr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - ENOENT))
            return
        raw_memzero(statbuf, STAT_SIZE)
        poke64(statbuf + ST_NLINK_OFFSET, 1)
        if (file.mode & S_IFMT) == S_IFDIR:
            poke32(statbuf + ST_MODE_OFFSET, S_IFDIR | 0o550)
        else:
            poke32(statbuf + ST_MODE_OFFSET, S_IFREG | 0o440)
        poke64(statbuf + ST_SIZE_OFFSET, file.size)
        poke64(statbuf + ST_BLKSIZE_OFFSET, 4096)
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_NEWFSTATAT:
        # Path-based stat -- same initramfs lookup as SYS_OPENAT/
        # SYS_EXECVE, just reporting metadata instead of opening a fd.
        # AT_EMPTY_PATH (an empty path + a real fd meaning "stat the fd
        # itself") isn't handled: nothing here calls fstatat() that way
        # yet, and getting it wrong silently would be worse than the
        # honest ENOENT a real, unhandled case gets elsewhere.
        path_ptr = peek64(regs + REG_RSI)
        statbuf = peek64(regs + REG_RDX)
        if path_ptr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - EFAULT))
            return
        path = _read_user_cstring(path_ptr)
        lookup_name = _normalize_lookup_name(path)
        file = find_module_file(lookup_name)
        if file.addr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - ENOENT))
            return
        raw_memzero(statbuf, STAT_SIZE)
        poke64(statbuf + ST_NLINK_OFFSET, 1)
        if (file.mode & S_IFMT) == S_IFDIR:
            poke32(statbuf + ST_MODE_OFFSET, S_IFDIR | 0o550)
        else:
            poke32(statbuf + ST_MODE_OFFSET, S_IFREG | 0o440)
        poke64(statbuf + ST_SIZE_OFFSET, file.size)
        poke64(statbuf + ST_BLKSIZE_OFFSET, 4096)
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_GETDENTS64:
        # Re-walks the whole flat cpio archive on every call (see
        # cpio.py's find_file()/CpioFile) looking for entries whose
        # name is "<this dir's own name>/<one more path segment, no
        # further '/'>" -- there's no real directory tree with child
        # pointers to follow, just this one flat list of full names.
        # fd_offset_table[slot] (repurposed here, not a byte offset --
        # directory fds never read() bytes) counts how many *matching*
        # children have already been returned across previous calls,
        # so a real caller's read-until-0 loop (ls included) makes
        # forward progress and eventually sees EOF instead of the same
        # first entries forever.
        fd = peek64(regs + REG_RDI)
        buf = peek64(regs + REG_RSI)
        count = peek64(regs + REG_RDX)
        slot = _fd_slot(fd)
        if slot == -1 or peek64(fd_addr_table + slot * 8) == 0:
            poke64(regs + REG_RAX, unsigned64(0 - EBADF))
            return
        if peek64(fd_is_dir_table + slot * 8) == 0:
            poke64(regs + REG_RAX, unsigned64(0 - ENOTDIR))
            return
        dir_header = peek64(fd_addr_table + slot * 8)
        dirname = entry_name_at(dir_header)
        prefix = ""
        if dirname != ".":
            prefix = dirname + "/"
        prefix_len = len(prefix)
        module_addr = find_module()
        base = 0
        total_size = 0
        if module_addr != 0:
            base = unsigned64(peek64(module_addr + 8))
            total_size = unsigned64(peek64(module_addr + 16))
        already = _fd_get_offset(fd)
        seen = 0
        written = 0
        out = buf
        remaining = count
        stop = 0
        offset = 0
        while offset < total_size and stop == 0:
            header = base + offset
            namesize = _read_hex8(header + 94)
            filesize = _read_hex8(header + 54)
            mode = _read_hex8(header + 14)
            name = _read_name(header + HEADER_SIZE, namesize)
            if name == "TRAILER!!!":
                offset = total_size
            else:
                is_child = 0
                child_name = ""
                if prefix_len == 0:
                    if name != "." and _has_slash(name) == 0:
                        is_child = 1
                        child_name = name
                elif len(name) > prefix_len and name[0:prefix_len] == prefix:
                    rest = name[prefix_len:]
                    if _has_slash(rest) == 0:
                        is_child = 1
                        child_name = rest
                if is_child == 1:
                    if seen >= already:
                        name_len = len(child_name)
                        reclen = DIRENT_NAME_OFFSET + name_len + 1
                        pad = reclen % 8
                        if pad != 0:
                            reclen = reclen + (8 - pad)
                        if remaining >= reclen:
                            d_type = DT_REG
                            if (mode & S_IFMT) == S_IFDIR:
                                d_type = DT_DIR
                            poke64(out + 0, seen + 1)
                            poke64(out + 8, seen + 1)
                            poke16(out + 16, reclen)
                            poke8(out + 18, d_type)
                            i2 = 0
                            while i2 < name_len:
                                poke8(out + DIRENT_NAME_OFFSET + i2,
                                      _char_to_ascii(child_name[i2]))
                                i2 = i2 + 1
                            poke8(out + DIRENT_NAME_OFFSET + name_len, 0)
                            out = out + reclen
                            remaining = remaining - reclen
                            written = written + reclen
                            seen = seen + 1
                        else:
                            stop = 1
                    else:
                        seen = seen + 1
                data_start = _align4(HEADER_SIZE + namesize)
                offset = offset + _align4(data_start + filesize)
        _fd_set_offset(fd, seen)
        poke64(regs + REG_RAX, written)
        return

    if num == SYS_GETRESUID:
        ruid = peek64(regs + REG_RDI)
        euid = peek64(regs + REG_RSI)
        suid = peek64(regs + REG_RDX)
        poke32(ruid, FAKE_UID)
        poke32(euid, FAKE_UID)
        poke32(suid, FAKE_UID)
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_GETRESGID:
        rgid = peek64(regs + REG_RDI)
        egid = peek64(regs + REG_RSI)
        sgid = peek64(regs + REG_RDX)
        poke32(rgid, FAKE_UID)
        poke32(egid, FAKE_UID)
        poke32(sgid, FAKE_UID)
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_GETUID or num == SYS_GETGID or num == SYS_GETEUID or num == SYS_GETEGID:
        poke64(regs + REG_RAX, FAKE_UID)
        return

    if num == SYS_GETTIMEOFDAY:
        tv = peek64(regs + REG_RDI)
        if tv != 0:
            poke64(tv, get_tick_count())
            poke64(tv + 8, 0)
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_CLOCK_GETTIME:
        # clockid (REG_RDI) ignored -- same one fake clock (the LAPIC
        # tick count, seconds-since-boot-ish) answers every clock this
        # kernel is ever asked about, same honest-approximation
        # tradeoff as SYS_GETTIMEOFDAY above.
        tp = peek64(regs + REG_RSI)
        if tp != 0:
            poke64(tp, get_tick_count())
            poke64(tp + 8, 0)
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_UNAME:
        buf = peek64(regs + REG_RDI)
        _write_cstring(buf + 0 * UTSNAME_FIELD_SIZE, "linux")
        _write_cstring(buf + 1 * UTSNAME_FIELD_SIZE, "pythonos")
        _write_cstring(buf + 2 * UTSNAME_FIELD_SIZE, "6.6.6")
        _write_cstring(buf + 3 * UTSNAME_FIELD_SIZE, "1")
        _write_cstring(buf + 4 * UTSNAME_FIELD_SIZE, "x86-64")
        _write_cstring(buf + 5 * UTSNAME_FIELD_SIZE, "none")
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_GETCWD:
        # Fixed at "/" -- no real directory tree exists to have a
        # working directory *in* (the initramfs is one flat namespace,
        # see SYS_OPENAT), so this is the simplest honest answer that
        # isn't outright failure.
        buf = peek64(regs + REG_RDI)
        _write_cstring(buf, "/")
        poke64(regs + REG_RAX, 2)
        return

    if num == SYS_GETPID:
        # The real per-thread tid, not FAKE_TID: unlike
        # SYS_SET_TID_ADDRESS's return value (glibc just stashes that
        # one, never acts on it), bash's own job control genuinely
        # tracks pids by this value -- get_current_tid() is exactly
        # what SYS_WAIT4/SYS_CLONE already treat as this thread's real
        # identity, and a fixed answer here would make every forked
        # child (and the shell itself) look like the same process.
        poke64(regs + REG_RAX, get_current_tid())
        return

    if num == SYS_GETPPID:
        # 0 for the shell itself (parent_table has no entry for it,
        # same "no parent" convention as real Linux's pid 1), the real
        # parent tid for anything fork()ed.
        poke64(regs + REG_RAX, get_parent(get_current_tid()))
        return

    if num == SYS_GETPGRP or num == SYS_GETPGID:
        # No real process-group tracking here -- every thread is
        # honestly its own group (pgid == pid), the simplest answer
        # that's still self-consistent for a kernel with no actual job-
        # control terminal-signal routing to group processes for. 0
        # (SYS_GETPGID's own "the caller itself") means the same thing
        # get_current_tid() would return directly.
        pid = get_current_tid()
        if num == SYS_GETPGID:
            requested = peek64(regs + REG_RDI)
            if requested != 0:
                pid = requested
        poke64(regs + REG_RAX, pid)
        return

    if num == SYS_SETPGID:
        # No-op success: see SYS_GETPGRP/SYS_GETPGID above -- there's
        # no real group membership here to actually change.
        _debugcon_str("syscall(SETPGID): stub")
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_DUP:
        oldfd = peek64(regs + REG_RDI)
        newfd = _alloc_fd()
        if newfd == -1:
            poke64(regs + REG_RAX, unsigned64(0 - EMFILE))
            return
        if _copy_fd_state(oldfd, _fd_slot(newfd)) == 0:
            poke64(regs + REG_RAX, unsigned64(0 - EBADF))
            return
        poke64(regs + REG_RAX, newfd)
        return

    if num == SYS_DUP2:
        oldfd = peek64(regs + REG_RDI)
        newfd = peek64(regs + REG_RSI)
        if oldfd == newfd:
            # POSIX: a no-op that still returns newfd, even for an fd
            # that isn't actually open.
            poke64(regs + REG_RAX, newfd)
            return
        slot = _fd_slot(newfd)
        if slot == -1:
            # newfd < 3 (0/1/2) or >= MAX_FDS: reassigning what the
            # fixed stdin/console fds *mean* isn't supported yet --
            # nothing has needed real redirection (a pipe or file onto
            # fd 0-2) this early in bash's own startup. A real gap,
            # not a silent wrong answer: honestly failing here beats
            # pretending success at a redirect that never actually
            # happens.
            poke64(regs + REG_RAX, unsigned64(0 - EBADF))
            return
        # Close the old newfd first if it was open (POSIX requirement:
        # dup2 atomically closes the target fd before duplicating).
        _fd_close(newfd)
        if _copy_fd_state(oldfd, slot) == 0:
            poke64(regs + REG_RAX, unsigned64(0 - EBADF))
            return
        poke64(regs + REG_RAX, newfd)
        return

    if num == SYS_FCNTL:
        fd = peek64(regs + REG_RDI)
        cmd = peek64(regs + REG_RSI)
        if _fd_is_open(fd) == 0:
            poke64(regs + REG_RAX, unsigned64(0 - EBADF))
            return
        if cmd == F_DUPFD or cmd == F_DUPFD_CLOEXEC:
            newfd = _alloc_fd()
            if newfd == -1:
                poke64(regs + REG_RAX, unsigned64(0 - EMFILE))
                return
            if _copy_fd_state(fd, _fd_slot(newfd)) == 0:
                poke64(regs + REG_RAX, unsigned64(0 - EBADF))
                return
            poke64(regs + REG_RAX, newfd)
            return
        # F_GETFD/F_SETFD/F_GETFL/F_SETFL, or anything else: no real
        # per-fd flag storage here (no CLOEXEC tracking either -- every
        # fd this kernel hands out is already closed the instant its
        # owning process is, real or not, since execve() replaces the
        # image wholesale rather than carrying fds across it) --
        # trivially succeeding with 0 is honest enough for a caller
        # that's only checking "did this fail outright".
        if cmd != F_DUPFD and cmd != F_DUPFD_CLOEXEC:
            _debugcon_str("syscall(FCNTL): stub")
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_MPROTECT:
        # No-op: almost certainly glibc's own RELRO enforcement (making
        # the GOT read-only post-relocation). This kernel never
        # write-protects pages to begin with, so faking success just
        # means that one hardening step doesn't happen.
        _debugcon_str("syscall(MPROTECT): stub")
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_FSTAT:
        fd = peek64(regs + REG_RDI)
        statbuf = peek64(regs + REG_RSI)
        if fd == 0 or fd == 1 or fd == 2:
            raw_memzero(statbuf, STAT_SIZE)
            poke64(statbuf + ST_NLINK_OFFSET, 1)
            poke32(statbuf + ST_MODE_OFFSET, S_IFCHR | 0o620)
            poke64(statbuf + ST_BLKSIZE_OFFSET, 4096)
            poke64(regs + REG_RAX, 0)
        else:
            slot = _fd_slot(fd)
            addr = 0
            if slot != -1:
                addr = peek64(fd_addr_table + slot * 8)
            if addr == 0:
                poke64(regs + REG_RAX, unsigned64(0 - EBADF))
            elif addr == FD_ALIAS_STDIN or addr == FD_ALIAS_CONSOLE:
                # A dup()'d copy of one of the fixed special fds --
                # same char-device report the real fd 0/1/2 get above.
                raw_memzero(statbuf, STAT_SIZE)
                poke64(statbuf + ST_NLINK_OFFSET, 1)
                poke32(statbuf + ST_MODE_OFFSET, S_IFCHR | 0o620)
                poke64(statbuf + ST_BLKSIZE_OFFSET, 4096)
                poke64(regs + REG_RAX, 0)
            else:
                raw_memzero(statbuf, STAT_SIZE)
                poke64(statbuf + ST_NLINK_OFFSET, 1)
                if peek64(fd_is_dir_table + slot * 8) != 0:
                    poke32(statbuf + ST_MODE_OFFSET, S_IFDIR | 0o550)
                else:
                    poke32(statbuf + ST_MODE_OFFSET, S_IFREG | 0o440)
                    poke64(statbuf + ST_SIZE_OFFSET, peek64(fd_size_table + slot * 8))
                poke64(statbuf + ST_BLKSIZE_OFFSET, 4096)
                poke64(regs + REG_RAX, 0)
        return

    if num == SYS_IOCTL:
        fd = peek64(regs + REG_RDI)
        request = peek64(regs + REG_RSI)
        argp = peek64(regs + REG_RDX)
        if (request == TCGETS2 or request == TCGETS) and (fd == 0 or fd == 1 or fd == 2):
            if request == TCGETS2:
                raw_memzero(argp, TERMIOS2_SIZE)
            else:
                raw_memzero(argp, TERMIOS_SIZE)
            lflag = ISIG
            if tty_get_icanon() != 0:
                lflag = lflag | ICANON
            if tty_get_echo() != 0:
                lflag = lflag | ECHO
            poke32(argp + C_LFLAG_OFFSET, lflag)
            poke64(regs + REG_RAX, 0)
        elif (request == TCSETS2 or request == TCSETSW2 or request == TCSETSF2
              or request == TCSETS or request == TCSETSW or request == TCSETSF) and (fd == 0 or fd == 1 or fd == 2):
            # Real tcsetattr(): flip this console's actual echo/line-
            # buffering policy (see ps2.py's tty_input_char()) to
            # whatever the caller's c_lflag asks for -- this is what
            # lets busybox vi (or anything else) actually put the
            # keyboard into raw/cbreak mode instead of TCGETS2/TCGETS
            # forever just describing a canonical terminal no program
            # could ever really change. c_lflag lands at the same
            # offset (12) in both the termios2 and plain termios
            # structs (see TCGETS/TCSETS' own comment above), so both
            # families share this one branch.
            lflag = peek32(argp + C_LFLAG_OFFSET)
            if (lflag & ICANON) != 0:
                tty_set_icanon(1)
            else:
                tty_set_icanon(0)
            if (lflag & ECHO) != 0:
                tty_set_echo(1)
            else:
                tty_set_echo(0)
            poke64(regs + REG_RAX, 0)
        elif request == TIOCGWINSZ and (fd == 0 or fd == 1 or fd == 2):
            poke16(argp, CONSOLE_ROWS)
            poke16(argp + 2, CONSOLE_COLS)
            poke16(argp + 4, 0)
            poke16(argp + 6, 0)
            poke64(regs + REG_RAX, 0)
        else:
            poke64(regs + REG_RAX, unsigned64(0 - ENOTTY))
        return

    if num == SYS_WRITEV:
        fd = peek64(regs + REG_RDI)
        iov = peek64(regs + REG_RSI)
        iovcnt = peek64(regs + REG_RDX)
        is_console = fd == 1 or fd == 2
        if fd >= 3:
            slot = _fd_slot(fd)
            if slot != -1 and peek64(fd_addr_table + slot * 8) == FD_ALIAS_CONSOLE:
                is_console = True
        total = 0
        i = 0
        while i < iovcnt:
            base = peek64(iov + i * IOVEC_SIZE)
            length = peek64(iov + i * IOVEC_SIZE + 8)
            # Same byte-exact echo as SYS_WRITE above: print_char()
            # per byte, never print() (whose unconditional trailing
            # '\n' used to be appended to *each* writev's whole
            # payload).
            if is_console:
                j = 0
                while j < length:
                    print_char(peek8(base + j))
                    j = j + 1
            total = total + length
            i = i + 1
        poke64(regs + REG_RAX, total)
        return

    if num == SYS_MMAP:
        length = peek64(regs + REG_RSI)
        flags = peek64(regs + REG_R10)
        if flags & MAP_ANONYMOUS != 0:
            # Same fix as _brk_grow(): map into whatever CR3 is
            # actually loaded for the thread making this syscall, not
            # the stale brk_vm global (only refreshed by init_brk(),
            # which a forked child never gets called for).
            cur_vm = VirtualMemory(brk_pa)
            aligned_len = length
            rem = aligned_len % PAGE_SIZE
            if rem != 0:
                aligned_len = aligned_len + (PAGE_SIZE - rem)
            base = mmap_next
            page = base
            while page < base + aligned_len:
                phys_page = brk_pa.alloc_page()
                cur_vm.map(page, phys_page - cur_vm.hhdm, PAGE_WRITABLE, True)
                raw_memzero(phys_page, PAGE_SIZE)
                page = page + PAGE_SIZE
            mmap_next = base + aligned_len
            poke64(regs + REG_RAX, base)
        else:
            # File-backed mappings need real file I/O this kernel
            # doesn't have -- the honest failure, not a silently wrong
            # mapping.
            poke64(regs + REG_RAX, unsigned64(0 - ENOSYS))
        return

    if num == SYS_MUNMAP:
        # int munmap(void *addr, size_t length) -- an honest no-op,
        # same shape as SYS_RT_SIGPROCMASK/SYS_RT_SIGACTION below:
        # accept the call and report success without actually undoing
        # anything. SYS_MMAP above is a pure bump allocator (mmap_next
        # only ever grows) that already never reclaims or reuses
        # address space, and process exit already abandons a whole
        # address space at once without walking it to free individual
        # pages (see SYS_EXECVE's own comment on the old image simply
        # leaking) -- so a real munmap() that tore down page-table
        # entries and freed physical frames one mapping at a time would
        # be more machinery than anything else here actually needs or
        # relies on. Callers only ever see this as "the kernel kept the
        # memory around a bit longer than asked," never as a wrong
        # result -- there's no address space reuse afterward for a
        # stale mapping to collide with.
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_RT_SIGACTION:
        # Same honest no-op as SYS_RT_SIGPROCMASK: accept the
        # registration (there's nowhere to actually store it -- no
        # signal is ever delivered here to look it up again), and
        # report "nothing was ever registered" for oldact if asked.
        _debugcon_str("syscall(RT_SIGACTION): stub")
        oldact = peek64(regs + REG_RDX)
        if oldact != 0:
            raw_memzero(oldact, SIGACTION_SIZE)
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_RT_SIGPROCMASK:
        # No-op: there's no signal delivery mechanism in this kernel at
        # all, so no signal could ever arrive to be masked in the first
        # place -- "block these signals" is trivially satisfied, and
        # oldset (if requested) is honestly "nothing was ever blocked".
        _debugcon_str("syscall(RT_SIGPROCMASK): stub")
        oldset = peek64(regs + REG_RDX)
        sigsetsize = peek64(regs + REG_R10)
        if oldset != 0:
            raw_memzero(oldset, sigsetsize)
        poke64(regs + REG_RAX, 0)
        return

    if num == SYS_CLONE:
        flags = peek64(regs + REG_RDI)
        if flags & CLONE_VM != 0:
            # A real pthread-style thread (shared address space) --
            # duplicate_address_space() only knows how to build a
            # genuinely separate one, and nothing here has any other
            # machinery (a shared heap, futexes, ...) a same-address-
            # space thread would actually need to be safe.
            print_satanic("Unhandled clone() flags, killing process:")
            print_satanic(hex(flags))
            mark_current_exited(1)
            schedule()
            return
        child_tid = alloc_thread_slot()
        if child_tid == -1:
            poke64(regs + REG_RAX, unsigned64(0 - EAGAIN))
            return
        # Duplicate the *actually currently loaded* address space, not
        # the possibly-stale brk_vm global (see _brk_grow()'s comment)
        # -- correct today either way since the immediate caller here
        # is always freshly exec'd, but this keeps working if a child
        # ever forks again itself without calling brk()/execve() first.
        parent_vm = VirtualMemory(brk_pa)
        child_vm = duplicate_address_space(brk_pa, kernel_vm, parent_vm)
        child_cr3 = child_vm.pml4 - child_vm.hhdm
        # The child's heap starts out byte-identical to the parent's
        # (duplicate_address_space() just copied every page), so its
        # brk cursor/mapped-end need to start there too -- leaving them
        # at this table's default of 0 made the child's very first
        # brk(0) query (glibc's static CRT always makes one) look like
        # "grow from nothing," letting it map fresh pages for addresses
        # already backed by the copied parent heap.
        poke64(brk_cur_table + child_tid * 8, peek64(brk_cur_table + get_current_tid() * 8))
        poke64(brk_mapped_end_table + child_tid * 8, peek64(brk_mapped_end_table + get_current_tid() * 8))
        # A full copy of this exact syscall's own register block into
        # the child's slot of fork_regs_table -- fork_child_trampoline
        # (see arch/x86_64_shared/thread_switch.cpp) reads it back the
        # first time the child is ever scheduled, restoring every
        # register the child needs to look exactly like this syscall
        # returning a second time, with one difference: RAX=0.
        slot = _FORK_REGS_TABLE_ADDR() + child_tid * 128
        poke64(slot + REG_RAX, 0)
        poke64(slot + REG_RDI, peek64(regs + REG_RDI))
        poke64(slot + REG_RSI, peek64(regs + REG_RSI))
        poke64(slot + REG_RDX, peek64(regs + REG_RDX))
        poke64(slot + REG_R10, peek64(regs + REG_R10))
        poke64(slot + REG_R8, peek64(regs + REG_R8))
        poke64(slot + REG_R9, peek64(regs + REG_R9))
        poke64(slot + REG_R11, peek64(regs + REG_R11))
        poke64(slot + REG_RCX, peek64(regs + REG_RCX))
        poke64(slot + REG_R15, peek64(regs + REG_R15))
        poke64(slot + REG_R14, peek64(regs + REG_R14))
        poke64(slot + REG_R13, peek64(regs + REG_R13))
        poke64(slot + REG_R12, peek64(regs + REG_R12))
        poke64(slot + REG_RBP, peek64(regs + REG_RBP))
        poke64(slot + REG_RBX, peek64(regs + REG_RBX))
        # +8, not the raw captured value: syscall_entry.cpp's very
        # first instruction (a "push %rbx", before it swaps onto the
        # kernel stack) transiently uses 8 bytes of the *user's own*
        # stack as scratch space to stash the real rbx, so the "old
        # rsp" it captures into regs[] is 8 bytes lower than the
        # user's actual rsp at the moment of the syscall instruction.
        # Invisible for a normal syscall return (the matching final
        # "pop %rbx" in that same file's epilogue restores the true
        # value before sysretq runs) -- but the forked child never
        # executes that epilogue, jumping straight into
        # fork_child_trampoline's own hand-built iretq instead, so it
        # needs the correction applied here or it inherits a stack
        # pointer permanently 8 bytes short of the real one (this is
        # exactly what made the child's own stack-protector canary
        # check read the wrong slot and fail).
        poke64(slot + REG_RSP, peek64(regs + REG_RSP) + 8)
        # CLONE_CHILD_SETTID (see this glibc build's own __fork -> _Fork,
        # confirmed via disassembly: flags always include it for a plain
        # fork()) asks the kernel to write the child's own new tid into
        # *child_tidptr, in the CHILD's own memory, before it runs a
        # single instruction. Skipping this looked like a harmless
        # perf-only optimization to skip -- it wasn't: glibc's post-fork
        # child bookkeeping reads that value back and depends on it
        # being correct, not the parent's old tid duplicated verbatim
        # along with everything else.
        child_tidptr = peek64(regs + REG_R10)
        if child_tidptr != 0:
            alias = lookup_phys_alias(child_vm, child_tidptr)
            if alias != 0:
                poke32(alias, child_tid)
        setup_fork_child(child_tid, child_cr3, get_current_tid(), rdmsr(IA32_FS_BASE))
        poke64(regs + REG_RAX, child_tid)
        return

    if num == SYS_FORK:
        # pid_t fork(void) -- no arguments at all, unlike SYS_CLONE
        # above: REG_RDI is just whatever the caller last left there,
        # not a flags word, and there's no child_tidptr in REG_R10
        # either (CLONE_CHILD_SETTID is a clone()-only flag; a real
        # fork() syscall doesn't take one, and musl's own post-fork
        # bookkeeping re-reads its tid itself rather than expecting the
        # kernel to have written it anywhere). musl calls this
        # directly on x86_64 rather than clone() -- same reason
        # SYS_OPEN/SYS_STAT exist alongside SYS_OPENAT/SYS_NEWFSTATAT
        # -- so this is exactly SYS_CLONE's non-CLONE_VM path with
        # those two clone-specific pieces dropped.
        child_tid = alloc_thread_slot()
        if child_tid == -1:
            poke64(regs + REG_RAX, unsigned64(0 - EAGAIN))
            return
        parent_vm = VirtualMemory(brk_pa)
        child_vm = duplicate_address_space(brk_pa, kernel_vm, parent_vm)
        child_cr3 = child_vm.pml4 - child_vm.hhdm
        poke64(brk_cur_table + child_tid * 8, peek64(brk_cur_table + get_current_tid() * 8))
        poke64(brk_mapped_end_table + child_tid * 8, peek64(brk_mapped_end_table + get_current_tid() * 8))
        slot = _FORK_REGS_TABLE_ADDR() + child_tid * 128
        poke64(slot + REG_RAX, 0)
        poke64(slot + REG_RDI, peek64(regs + REG_RDI))
        poke64(slot + REG_RSI, peek64(regs + REG_RSI))
        poke64(slot + REG_RDX, peek64(regs + REG_RDX))
        poke64(slot + REG_R10, peek64(regs + REG_R10))
        poke64(slot + REG_R8, peek64(regs + REG_R8))
        poke64(slot + REG_R9, peek64(regs + REG_R9))
        poke64(slot + REG_R11, peek64(regs + REG_R11))
        poke64(slot + REG_RCX, peek64(regs + REG_RCX))
        poke64(slot + REG_R15, peek64(regs + REG_R15))
        poke64(slot + REG_R14, peek64(regs + REG_R14))
        poke64(slot + REG_R13, peek64(regs + REG_R13))
        poke64(slot + REG_R12, peek64(regs + REG_R12))
        poke64(slot + REG_RBP, peek64(regs + REG_RBP))
        poke64(slot + REG_RBX, peek64(regs + REG_RBX))
        # Same +8 correction as SYS_CLONE above, for the same reason
        # (syscall_entry.cpp's transient "push %rbx" scratch use of the
        # user's own stack before the child ever reaches the matching
        # "pop %rbx").
        poke64(slot + REG_RSP, peek64(regs + REG_RSP) + 8)
        setup_fork_child(child_tid, child_cr3, get_current_tid(), rdmsr(IA32_FS_BASE))
        poke64(regs + REG_RAX, child_tid)
        return

    if num == SYS_EXECVE:
        path_ptr = peek64(regs + REG_RDI)
        argv_ptr = peek64(regs + REG_RSI)
        if path_ptr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - EFAULT))
            return
        path = _read_user_cstring(path_ptr)
        lookup_name = _normalize_lookup_name(path)
        file = find_module_file(lookup_name)
        if file.addr == 0:
            poke64(regs + REG_RAX, unsigned64(0 - ENOENT))
            return
        new_vm = new_address_space(brk_pa, kernel_vm)
        elf_info = load_elf(brk_pa, new_vm, file.addr, True)
        if elf_info.entry == 0:
            poke64(regs + REG_RAX, unsigned64(0 - ENOEXEC))
            return
        # argv_ptr (REG_RSI): the caller's own char*[] array, still
        # live in the OLD address space (this runs before CR3 ever
        # switches -- see setup_user_stack_argv() in process.py) --
        # what the new process's argv[] actually reports, which need
        # not match `path` (RDI just says *which file* to load; RSI
        # says what the new program is told argv[0].. was).
        user_rsp = setup_user_stack_argv(brk_pa, new_vm, elf_info, argv_ptr)
        new_cr3 = new_vm.pml4 - new_vm.hhdm
        set_current_cr3(new_cr3)
        init_brk(brk_pa, new_vm, elf_info.brk_base, get_current_tid())
        # The rest of this thread's own image (the OLD address space's
        # pages) is simply abandoned, never freed -- free_page() now
        # exists (see memory.py) but walking the old page tables to
        # reclaim individual pages is a separate follow-up; for now
        # the old address space leaks until the process exits.
        write_cr3(new_cr3)
        _EXEC_INTO_RING3(elf_info.entry, user_rsp)
        # Never reached: _EXEC_INTO_RING3 jumps straight into the new
        # process image and never returns here, same as how SYS_EXIT's
        # schedule() call never truly returns for a thread that just
        # died.
        return

    if num == SYS_WAIT4:
        pid = peek64(regs + REG_RDI)
        status_ptr = peek64(regs + REG_RSI)
        me = get_current_tid()
        if pid > 0:
            if get_parent(pid) != me:
                poke64(regs + REG_RAX, unsigned64(0 - ECHILD))
                return
            while not is_zombie(pid):
                mark_current_blocked()
                schedule()
            status = reap_child(pid)
            if status_ptr != 0:
                poke32(status_ptr, (status & 0xFF) * 256)
            poke64(regs + REG_RAX, pid)
            return
        else:
            if not has_any_child(me):
                poke64(regs + REG_RAX, unsigned64(0 - ECHILD))
                return
            child = find_zombie_child(me)
            while child == -1:
                mark_current_blocked()
                schedule()
                child = find_zombie_child(me)
            status = reap_child(child)
            if status_ptr != 0:
                poke32(status_ptr, (status & 0xFF) * 256)
            poke64(regs + REG_RAX, child)
            return

    if num == SYS_EXIT:
        mark_current_exited(peek64(regs + REG_RDI))
        schedule()
        return

    if num == SYS_EXIT_GROUP:
        # exit_group differs from exit(2) only in killing every thread
        # in the calling process's thread group, not just the caller --
        # with no real clone()-based multi-threading here, there's only
        # ever the one ring3 thread per process anyway, so retiring it
        # is the whole of the observable difference.
        mark_current_exited(peek64(regs + REG_RDI))
        schedule()
        return

    print_satanic("Unhandled syscall, killing process:")
    print_satanic(int_to_str(num))
    # mark_current_exited(), not mark_current_dead(): a parent that's
    # already sitting in wait4() (see SYS_WAIT4 above) needs this to
    # show up as a zombie the same way a clean exit() would, or it
    # blocks forever -- exactly what happened before this used
    # mark_current_exited() here (confirmed via boot: the shell never
    # got its prompt back after a forked child hit this path).
    mark_current_exited(1)
    schedule()
