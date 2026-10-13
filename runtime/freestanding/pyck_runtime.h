/* Freestanding runtime for PyCk.
 *
 * Provides PyObject struct, refcounting, bignum (int), float, bool,
 * allocator hooks, recursion guard, and print stub.
 */
#ifndef PYCK_RUNTIME_H
#define PYCK_RUNTIME_H

#include <stdint.h>
#include <stddef.h>
#include <sys/types.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Freestanding Py_ssize_t definition */
#ifndef Py_ssize_t
typedef ssize_t Py_ssize_t;
#endif

// ---------------------------------------------------------------------------
// Forward declarations
// ---------------------------------------------------------------------------

typedef struct PyTypeObject PyTypeObject;
typedef struct PyObject PyObject;
typedef struct PyVarObject PyVarObject;

// ---------------------------------------------------------------------------
// PyObject layout — mirrors CPython exactly
// ---------------------------------------------------------------------------

struct PyObject {
    intptr_t ob_refcnt;
    PyTypeObject* ob_type;
};

struct PyVarObject {
    PyObject ob_base;
    Py_ssize_t ob_size;  // number of items for var-size types
};

// ---------------------------------------------------------------------------
// Type object (simplified — we only need a few fields)
// ---------------------------------------------------------------------------

typedef void (*destructor_fn)(PyObject*);

struct PyTypeObject {
    PyObject ob_base;
    const char* tp_name;
    destructor_fn tp_dealloc;
    // In a real implementation, more fields follow. For our subset, this
    // is sufficient: the codegen only calls tp_dealloc via PY_DECREF.
};

// ---------------------------------------------------------------------------
// Allocator hooks (must be provided by arch support kit or user)
// ---------------------------------------------------------------------------

extern void  init_heap(void* base, size_t size);
extern void* py_alloc(size_t size);
extern void  py_free(void* ptr);

// ---------------------------------------------------------------------------
// Built-in type singletons
// ---------------------------------------------------------------------------

// Bool singletons
extern PyObject _pyck_true_instance;
extern PyObject _pyck_false_instance;
#define _pyck_true  (&_pyck_true_instance)
#define _pyck_false (&_pyck_false_instance)

// None singleton
extern PyObject _pyck_none_instance;
#define _pyck_none  (&_pyck_none_instance)

// Type objects
extern PyTypeObject _pyck_type_int;
extern PyTypeObject _pyck_type_float;
extern PyTypeObject _pyck_type_bool;
extern PyTypeObject _pyck_type_str;

// ---------------------------------------------------------------------------
// Minimal runtime string ("str") object: a length-prefixed ASCII byte
// buffer, immutable once built.
// v1 subset: the only ways to build one are chr() (length 1) and "+"
// concatenating two of them -- no slicing, no indexing, no other string
// methods, and string *literals* remain compile-time-only (print()'s
// codegen resolves those directly to a C string, never through this
// type). See pyck/codegen.py's "chr" and STR+STR handling, and each
// arch's _print_item for how a value of this type gets printed.
// ---------------------------------------------------------------------------

typedef struct {
    PyObject ob_base;
    Py_ssize_t length;
    char data[1];  // flexible array member; actually `length` bytes
} PyStrObject;

PyObject* _pyck_char_from_code(int64_t code);
PyObject* _pyck_str_concat(PyObject* a, PyObject* b);
Py_ssize_t _pyck_str_length(PyObject* obj);
const char* _pyck_str_data(PyObject* obj);

// Boxes a compile-time-known C string literal (codegen passes its exact
// length, so this never needs to scan for a null terminator) into a real
// runtime string value -- what makes a Python string literal usable
// anywhere a str value can be (assigned, concatenated, passed around),
// not just as a print() argument. See pyck/codegen.py's
// _emit_str_lit_boxed.
PyObject* _pyck_str_from_cstr(const char* s, Py_ssize_t len);

// Indexing/slicing use Python's negative-index convention (index += length
// if negative); out-of-range single-element indexing is a fatal error
// (there's no exception mechanism in this runtime -- see py_fatal_error),
// while slicing clamps out-of-range bounds instead, matching Python slice
// semantics.
PyObject* _pyck_str_getitem(PyObject* s, int64_t index);
// Full extended-slice semantics (step included): a zero step is a fatal
// error (there's no exception mechanism here -- see py_fatal_error),
// matching Python's ValueError for the same case.
PyObject* _pyck_str_slice(PyObject* s, int64_t start, int64_t stop, int64_t step,
                          int has_start, int has_stop, int has_step);
int _pyck_str_cmp(PyObject* a, PyObject* b);  // strcmp-style: <0, 0, >0
PyObject* _pyck_str_repeat(PyObject* s, int64_t n);

// ---------------------------------------------------------------------------
// Bignum (int) operations — adapted from CPython longobject.c
// ---------------------------------------------------------------------------

// A bignum digit (same as CPython)
typedef uint32_t digit;
typedef int32_t sdigit;
#define PyLong_SHIFT 30
#define PyLong_BASE ((digit)1 << PyLong_SHIFT)
#define PyLong_MASK (PyLong_BASE - 1)

// A PyLongObject (simplified from CPython)
typedef struct {
    PyObject ob_base;
    Py_ssize_t ob_size;   // number of digits, sign = ob_size sign
    digit ob_digit[1];    // variable-length
} PyLongObject;

// A PyFloatObject (boxed double)
typedef struct {
    PyObject ob_base;
    double ob_fval;
} PyFloatObject;

// Int construction
PyObject* _pyck_long_from_long(int64_t value);
PyObject* _pyck_long_from_string(const char* str);  // for parsing
int64_t   _pyck_long_as_long(PyObject* obj);
int       _pyck_int_is_negative(PyObject* obj);
int       _pyck_int_is_zero(PyObject* obj);
int       _pyck_int_sign(PyObject* obj);  // -1, 0, 1

// hex(n) -> a "0x..."/"-0x..." runtime string, matching real Python's
// hex(). Operates on the native int64_t range (like every other int
// operation that unboxes via _pyck_long_as_long), not true bignum
// precision.
PyObject* _pyck_int_to_hex(PyObject* obj);

// Int arithmetic
PyObject* _pyck_bignum_add(PyObject* a, PyObject* b);
PyObject* _pyck_bignum_sub(PyObject* a, PyObject* b);
PyObject* _pyck_bignum_mul(PyObject* a, PyObject* b);
PyObject* _pyck_bignum_truediv(PyObject* a, PyObject* b);
PyObject* _pyck_bignum_floordiv(PyObject* a, PyObject* b);
PyObject* _pyck_bignum_mod(PyObject* a, PyObject* b);
PyObject* _pyck_bignum_pow(PyObject* a, PyObject* b);
PyObject* _pyck_bignum_neg(PyObject* obj);
PyObject* _pyck_bignum_and(PyObject* a, PyObject* b);
PyObject* _pyck_bignum_or(PyObject* a, PyObject* b);
PyObject* _pyck_bignum_xor(PyObject* a, PyObject* b);
PyObject* _pyck_bignum_lshift(PyObject* a, PyObject* b);
PyObject* _pyck_bignum_rshift(PyObject* a, PyObject* b);

// Bool check
int _pyck_is_true(PyObject* obj);

// Float operations
PyObject* _pyck_float_from_double(double value);
double     _pyck_float_as_double(PyObject* obj);
PyObject* _pyck_float_add(PyObject* a, PyObject* b);
PyObject* _pyck_float_sub(PyObject* a, PyObject* b);
PyObject* _pyck_float_mul(PyObject* a, PyObject* b);
int       _pyck_float_lt(PyObject* a, PyObject* b);
int       _pyck_float_lte(PyObject* a, PyObject* b);
int       _pyck_float_gt(PyObject* a, PyObject* b);
int       _pyck_float_gte(PyObject* a, PyObject* b);
int       _pyck_float_eq(PyObject* a, PyObject* b);
int       _pyck_float_neq(PyObject* a, PyObject* b);

// ---------------------------------------------------------------------------
// Tuple (used by print())
// ---------------------------------------------------------------------------

typedef struct {
    PyObject ob_base;
    Py_ssize_t ob_size;
    PyObject* ob_item[1];  // variable-length array of pointers
} PyTupleObject;

PyObject* _pyck_tuple_new(Py_ssize_t size);
void      _pyck_tuple_set(PyObject* tuple, Py_ssize_t index, PyObject* item);
PyObject* _pyck_tuple_get(PyObject* tuple, Py_ssize_t index);
Py_ssize_t _pyck_tuple_size(PyObject* tuple);
void      _pyck_tuple_decref(PyObject* tuple);

// ---------------------------------------------------------------------------
// print() — must be provided by arch support kit in freestanding mode
// ---------------------------------------------------------------------------

extern void py_print(PyObject* args, PyObject* sep, PyObject* end);

// Print a C string literal directly (for string constants in print() args)
extern void _pyck_print_cstr(const char* str);

// ---------------------------------------------------------------------------
// x86 hardware intrinsics: port I/O, MSRs, CPUID, control registers.
// inb/outb are defined per-arch (in each arch's own support.cpp); the rest
// are defined once, shared by every x86 arch, in
// arch/x86_shared/hw_intrinsics.cpp. Only linked in for x86/x86_64/
// x86_64_graphical targets -- see pyck/__main__.py's _build_compile_command.
// ---------------------------------------------------------------------------

extern "C" uint8_t inb(uint16_t port);
extern "C" void outb(uint16_t port, uint8_t val);
extern "C" uint16_t _pyck_inw(uint16_t port);
extern "C" void _pyck_outw(uint16_t port, uint16_t val);
extern "C" uint32_t _pyck_inl(uint16_t port);
extern "C" void _pyck_outl(uint16_t port, uint32_t val);

extern "C" uint64_t _pyck_rdmsr(uint32_t msr);
extern "C" void _pyck_wrmsr(uint32_t msr, uint64_t value);

extern "C" uint32_t _pyck_cpuid_eax(uint32_t leaf);
extern "C" uint32_t _pyck_cpuid_ebx(uint32_t leaf);
extern "C" uint32_t _pyck_cpuid_ecx(uint32_t leaf);
extern "C" uint32_t _pyck_cpuid_edx(uint32_t leaf);

extern "C" unsigned long _pyck_read_cr0(void);
extern "C" void _pyck_write_cr0(unsigned long value);
extern "C" unsigned long _pyck_read_cr2(void);
extern "C" void _pyck_write_cr2(unsigned long value);
extern "C" unsigned long _pyck_read_cr3(void);
extern "C" void _pyck_write_cr3(unsigned long value);
extern "C" unsigned long _pyck_read_cr4(void);
extern "C" void _pyck_write_cr4(unsigned long value);

// ---------------------------------------------------------------------------
// Limine request table (defined once in arch/x86_64_shared/limine_table.cpp
// and linked in for the Limine-booted x86_64/x86_64_graphical targets --
// see pyck/__main__.py's _build_compile_command).
//
// Only the table's own address is exposed as a builtin -- a
// null-terminated array of every declared request's address. PyCk source
// walks it with peek64(), reads each entry's 4-word magic id, and
// compares against the specific feature's known id to find the request
// it wants, then reads that request's response pointer itself (see
// PythonOS/kernel/'s MemoryMap/MemoryMapEntry classes for memmap, the
// first feature built this way). Every Limine request shares the same
// header layout (id[4] at +0, revision at +32, response at +40), which
// is what makes one generic table sufficient for any current or future
// request instead of a dedicated builtin per feature.
// ---------------------------------------------------------------------------

extern "C" uint64_t _pyck_limine_table_addr(void);

// ---------------------------------------------------------------------------
// Interrupt stub table (_ISR_STUB() builtin; see
// arch/x86_64_shared/idt_stubs.cpp). Returns the address of one of a
// small, fixed set of hand-written __attribute__((interrupt))
// trampolines -- PyCk source builds the actual IDT entries and decides
// what each vector does via a top-level `def on_interrupt(vector: int)`.
// ---------------------------------------------------------------------------

extern "C" uint64_t _pyck_isr_stub_addr(uint64_t slot);

// ---------------------------------------------------------------------------
// Kernel thread context switching (switch_context() and
// _THREAD_TRAMPOLINE_ADDR() builtins; see
// arch/x86_64_shared/thread_switch.cpp). switch_context() saves the
// currently-running thread's stack pointer to the raw memory cell at
// old_rsp_addr, switches RSP to new_rsp, and resumes whatever was last
// saved there. _pyck_thread_trampoline_addr() returns the address of the
// fixed entry point a freshly-created thread's fake initial stack points
// its first `ret` at -- PyCk source (PythonOS/kernel/threads.py) builds
// that fake stack, decides which thread runs next, and provides the
// shared `def thread_entry():` every thread actually runs.
// ---------------------------------------------------------------------------

extern "C" void _pyck_switch_context(unsigned long old_rsp_addr, unsigned long new_rsp);
extern "C" uint64_t _pyck_thread_trampoline_addr(void);
extern "C" uint64_t _pyck_ring3_trampoline_addr(void);

// fork()/execve() support -- see arch/x86_64_shared/thread_switch.cpp.
// _pyck_fork_regs_table_addr() is the base of the per-thread saved-
// register table SYS_FORK fills in and fork_child_trampoline reads
// back; _pyck_fork_child_trampoline_addr() is that trampoline's own
// address, for the same "fake initial stack" trick
// _pyck_ring3_trampoline_addr() above already uses. _pyck_exec_into_ring3()
// is SYS_EXECVE's one-way jump into a freshly loaded process image --
// it never returns to its caller.
extern "C" uint64_t _pyck_fork_regs_table_addr(void);
extern "C" uint64_t _pyck_fork_child_trampoline_addr(void);
extern "C" void _pyck_exec_into_ring3(unsigned long entry, unsigned long rsp);

// Points syscall_entry.cpp's kernel-stack swap at the current thread's
// own dedicated stack -- see arch/x86_64_shared/syscall_entry.cpp's own
// comment. threads.py's schedule() calls this every time it switches,
// right alongside write_cr3().
extern "C" void _pyck_set_syscall_stack_top(uint64_t top);

// ---------------------------------------------------------------------------
// Real x86-64 SYSCALL entry (_SYSCALL_ENTRY_ADDR() builtin; see
// arch/x86_64_shared/syscall_entry.cpp). Returns the address `syscall`
// itself jumps to once PyCk source wrmsr's it into IA32_LSTAR -- from
// there, a top-level `def on_syscall(regs: int):` (same
// saved-register-block convention as on_interrupt) is the real syscall
// handler.
// ---------------------------------------------------------------------------

extern "C" uint64_t _pyck_syscall_entry_addr(void);

// ---------------------------------------------------------------------------
// Bulk raw-memory operations (raw_memzero()/raw_memcpy() builtins; see
// runtime/freestanding/py_runtime.cpp, arch-independent so linked in
// for every freestanding target). Gated the same as peek/poke
// (--enable-dangerous-memory-ops) -- same idea, just processing many
// bytes in one call instead of one PyCk-level loop iteration per byte,
// which this runtime's never-frees PyObject heap can't sustain for
// anything much bigger than a few KB.
// ---------------------------------------------------------------------------

extern "C" void _pyck_raw_memzero(unsigned long addr, unsigned long count);
extern "C" void _pyck_raw_memcpy(unsigned long dst, unsigned long src, unsigned long count);

// print_char(code) builtin -- writes one character with no trailing
// newline (unlike print()); see arch/x86_64_graphical/graphics.cpp's
// _pyck_print_char().
extern "C" void _pyck_print_char(int64_t code);

// ---------------------------------------------------------------------------
// Fatal error
// ---------------------------------------------------------------------------

extern void py_fatal_error(const char* message);

// ---------------------------------------------------------------------------
// Recursion depth (set by codegen, checked by runtime)
// ---------------------------------------------------------------------------

extern int64_t _pyck_max_recursion_depth;

// ---------------------------------------------------------------------------
// Reference counting
// ---------------------------------------------------------------------------

#define Py_INCREF(op) do { (op)->ob_refcnt++; } while(0)
#define Py_DECREF(op) do { \
    if (--(op)->ob_refcnt == 0) { \
        if ((op)->ob_type->tp_dealloc) (op)->ob_type->tp_dealloc((PyObject*)(op)); \
    } \
} while(0)

#ifdef __cplusplus
}
#endif

#endif // PYCK_RUNTIME_H
