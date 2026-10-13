"""Whole-program type inference pass for PyCk.

Two-pass design:
  Pass 1: Collect function definitions and annotations, infer param/return
          types from call sites.
  Pass 2: Walk each function body + module top-level, infer local types,
          validate annotation consistency, build IR.

If types disagree across call sites, or a function/global is never called/read
and lacks an annotation, that's a transpile-time error.
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from typing import Optional

from .types import PyType, PyTypeKind, PYINT, PYFLOAT, PYBOOL, PYSTRING, PYNONE, PYUNKNOWN, unify
from . import ir as IR


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

@dataclass
class CompileError(Exception):
    filename: str
    line: int
    col: int
    message: str

    def __str__(self) -> str:
        return f"{self.filename}:{self.line}:{self.col}: error: {self.message}"


def _error(filename: str, node: ast.AST, msg: str) -> CompileError:
    # When multiple files get merged into one AST for compilation (see
    # pyck/imports.py), each node is tagged with the file it actually
    # came from -- preferred here over the checker's own single
    # constructor-time filename so an error in gdt.py doesn't get blamed
    # on main.py just because main.py was the entry point.
    fname = getattr(node, "_pyck_file", filename)
    return CompileError(fname, node.lineno, node.col_offset, msg)


# ---------------------------------------------------------------------------
# Hardware intrinsics (x86 port I/O, MSRs, CPUID, control registers).
# name -> (expected arg count, return type). Shared with ast_to_ir.py, which
# imports this same registry so both stages agree on arity and result type.
# Gated behind --enable-dangerous-io-ops (see TypeChecker.dangerous_io_ops)
# and freestanding-only -- codegen.py rejects them in hosted mode, since
# ring-3 userspace has no I/O privilege to execute these instructions.
# ---------------------------------------------------------------------------

HARDWARE_INTRINSICS: dict[str, tuple[int, PyType]] = {
    "inb": (1, PYINT), "outb": (2, PYNONE),
    "inw": (1, PYINT), "outw": (2, PYNONE),
    "inl": (1, PYINT), "outl": (2, PYNONE),
    "rdmsr": (1, PYINT), "wrmsr": (2, PYNONE),
    "cpuid_eax": (1, PYINT), "cpuid_ebx": (1, PYINT),
    "cpuid_ecx": (1, PYINT), "cpuid_edx": (1, PYINT),
    "read_cr0": (0, PYINT), "write_cr0": (1, PYNONE),
    "read_cr2": (0, PYINT), "write_cr2": (1, PYNONE),
    "read_cr3": (0, PYINT), "write_cr3": (1, PYNONE),
    "read_cr4": (0, PYINT), "write_cr4": (1, PYNONE),
    # switch_context(old_rsp_addr, new_rsp): saves the currently-running
    # kernel thread's stack pointer to the raw memory cell at
    # old_rsp_addr, then switches RSP to new_rsp and resumes whatever was
    # last saved there -- see arch/x86_64_shared/thread_switch.cpp and
    # PythonOS/kernel/threads.py. Both arguments are plain integers (a
    # raw address and a raw saved-RSP value, from peek64/poke64 on a
    # dedicated table -- never the address of a PyCk variable itself,
    # since reassigning one rebinds it to a new boxed object rather than
    # overwriting it in place).
    "switch_context": (2, PYNONE),
    # print_char(code): writes one raw character (see
    # arch/x86_64_graphical/graphics.cpp's _pyck_print_char()) with no
    # trailing newline, unlike print() -- which always appends one and
    # has no way not to (its own sep/end parameters are accepted but
    # never actually used). Needed for echoing keystrokes one at a time
    # as they're typed (see PythonOS/kernel/ps2.py) without every
    # character landing on its own line.
    "print_char": (1, PYNONE),
    # _EXEC_INTO_RING3(entry, rsp): SYS_EXECVE's (see
    # PythonOS/kernel/syscall.py) one-way jump into a freshly loaded
    # process image -- never returns to its caller, same as how
    # SYS_EXIT's schedule() call never truly returns for a thread that
    # just died. See arch/x86_64_shared/thread_switch.cpp.
    "_EXEC_INTO_RING3": (2, PYNONE),
    # _SET_SYSCALL_STACK_TOP(top): points syscall_entry.cpp's kernel-
    # stack swap at the given thread's own dedicated stack -- see
    # arch/x86_64_shared/syscall_entry.cpp's own comment on why this
    # has to be per-thread. threads.py's schedule() calls this every
    # time it switches, right alongside write_cr3().
    "_SET_SYSCALL_STACK_TOP": (1, PYNONE),
}


# ---------------------------------------------------------------------------
# Limine request table (Limine-booted x86_64/x86_64_graphical targets only
# -- see arch/x86_64_shared/limine_table.cpp). name -> (expected arg
# count, return type), same shape as HARDWARE_INTRINSICS. Unlike the
# hardware intrinsics above, this is read-only environment info (not I/O/
# memory manipulation), so it is NOT gated behind --enable-dangerous-io-ops
# -- always available in freestanding mode.
#
# _LIMINE() is the *only* builtin: it returns the address of a
# null-terminated table of every Limine request this kernel declared.
# PyCk source walks that table itself with peek64() (gated behind
# --enable-dangerous-memory-ops, like any other raw memory read), finds
# the request it wants by matching its unique magic id, and decodes
# everything past that (response pointer, and whatever fields that
# specific response has) at raw offsets matching the Limine protocol
# spec -- see PythonOS/kernel/'s MemoryMap/MemoryMapEntry classes for
# memmap, the first feature built this way. There is no compiler-level
# type for any of this; adding a new Limine feature later needs no new
# builtin, just a new request declared in limine_table.cpp.
# ---------------------------------------------------------------------------

LIMINE_INTRINSICS: dict[str, tuple[int, PyType]] = {
    "_LIMINE": (0, PYINT),
}


# ---------------------------------------------------------------------------
# Interrupt stub table (see arch/x86_64_shared/idt_stubs.cpp). Same shape as
# LIMINE_INTRINSICS: read-only, always available in freestanding mode.
#
# _ISR_STUB(slot) returns the address of one of a small, fixed set of
# hand-written __attribute__((interrupt)) trampolines -- real interrupt
# gates need the CPU-mandated register-save/iretq dance a normal PyCk
# function can't produce, so that part stays a tiny amount of hand-written
# C++, exactly like the Limine request table did for boot info. Everything
# else (building IDT entries byte-by-byte, loading the IDTR, deciding what
# each vector actually *does*) is PyCk source -- see PythonOS/kernel/idt.py.
# Each stub calls _pyck_on_interrupt(vector), a single fixed entry point a
# PyCk source file provides by defining a top-level `def on_interrupt(vector:
# int)` -- the one function name codegen.py emits with C linkage instead of
# `static`, precisely so hand-written C++ in a different translation unit
# can call into it.
# ---------------------------------------------------------------------------

ISR_INTRINSICS: dict[str, tuple[int, PyType]] = {
    "_ISR_STUB": (1, PYINT),
    # _THREAD_TRAMPOLINE_ADDR() returns the address of a second small
    # hand-written function: _pyck_thread_trampoline (see
    # arch/x86_64_shared/thread_switch.cpp), the fixed "entry point" a
    # freshly-created kernel thread's fake initial stack points its
    # first `ret` (from switch_context()) at. Same shape/reasoning as
    # _ISR_STUB above, just for thread creation instead of interrupt
    # gates -- see PythonOS/kernel/threads.py.
    "_THREAD_TRAMPOLINE_ADDR": (0, PYINT),
    # _RING3_TRAMPOLINE_ADDR() returns the address of a fourth
    # hand-written function: ring3_trampoline (see
    # arch/x86_64_shared/thread_switch.cpp), which builds a fake
    # interrupt-return frame and `iretq`s into ring3 -- the one-time
    # launch for a freshly-loaded process's very first activation (see
    # PythonOS/kernel/threads.py's _init_ring3_thread_stack()).
    "_RING3_TRAMPOLINE_ADDR": (0, PYINT),
    # _SYSCALL_ENTRY_ADDR() returns the address of a third hand-written
    # function: syscall_entry (see arch/x86_64_shared/syscall_entry.cpp),
    # the real x86-64 `syscall` instruction's landing point -- wrmsr'd
    # into IA32_LSTAR by PyCk source (PythonOS/kernel/syscall.py) so
    # nothing about the SYSCALL/SYSRET MSR setup itself needs a builtin
    # of its own (wrmsr already exists as a HARDWARE_INTRINSIC).
    "_SYSCALL_ENTRY_ADDR": (0, PYINT),
    # _FORK_REGS_TABLE_ADDR() returns the base of the per-thread saved-
    # register table SYS_FORK (see PythonOS/kernel/syscall.py) fills in
    # for a new child thread, and _FORK_CHILD_TRAMPOLINE_ADDR() returns
    # the address of fork_child_trampoline, which reads that table back
    # the first (and only) time the child is scheduled -- see
    # arch/x86_64_shared/thread_switch.cpp and PythonOS/kernel/
    # threads.py's _init_fork_child_stack().
    "_FORK_REGS_TABLE_ADDR": (0, PYINT),
    "_FORK_CHILD_TRAMPOLINE_ADDR": (0, PYINT),
}


# ---------------------------------------------------------------------------
# Top-level functions called from outside the compiled PyCk program
# entirely -- hand-written C++ in a different translation unit (see
# idt_stubs.cpp) calls these directly by their extern "C" name, so
# nothing in the PyCk source itself ever needs to call them. Shared by
# codegen.py (emits these with C linkage instead of `static`) and
# TypeChecker._compute_reachable (which would otherwise -- correctly,
# but wrongly here -- conclude one of these is dead code, since no
# ast.Call anywhere in the source ever targets it).
#
# "thread_entry" is the same story for kernel threads: every thread a
# PyCk source file creates (see PythonOS/kernel/threads.py) shares this
# one entry point (told apart only by reading a "which thread is this"
# global the scheduler maintains) -- _pyck_thread_trampoline in
# arch/x86_64_shared/thread_switch.cpp calls it, nothing in PyCk source
# does.
# ---------------------------------------------------------------------------

EXTERNALLY_CALLED_FUNCS: set[str] = {"on_interrupt", "thread_entry", "on_syscall"}


# ---------------------------------------------------------------------------
# Comparison operator dunders. A class instance compared with `==`, `<`,
# etc. dispatches to the matching method (p1 == p2 -> p1.__eq__(p2)); a
# class defining __eq__ but not __ne__ gets __ne__ derived as `not __eq__`,
# same as real Python. Shared with ast_to_ir.py so both stages agree on
# which dunder each ast.cmpop maps to.
# ---------------------------------------------------------------------------

CMP_DUNDER: dict[type, str] = {
    ast.Eq: "__eq__", ast.NotEq: "__ne__",
    ast.Lt: "__lt__", ast.LtE: "__le__",
    ast.Gt: "__gt__", ast.GtE: "__ge__",
}

_CMP_SYMBOL: dict[type, str] = {
    ast.Eq: "==", ast.NotEq: "!=",
    ast.Lt: "<", ast.LtE: "<=",
    ast.Gt: ">", ast.GtE: ">=",
}


# ---------------------------------------------------------------------------
# Annotation parser
# ---------------------------------------------------------------------------

def _parse_annotation(node: ast.expr | None, filename: str) -> PyType | None:
    if node is None:
        return None
    if isinstance(node, ast.Name):
        mapping = {"int": PYINT, "float": PYFLOAT, "bool": PYBOOL, "str": PYSTRING}
        if node.id in mapping:
            return mapping[node.id]
    raise _error(filename, node, f"Unsupported type annotation: {ast.dump(node)}")


# ---------------------------------------------------------------------------
# Function signature
# ---------------------------------------------------------------------------

@dataclass
class FuncSig:
    name: str
    param_names: list[str]
    param_types: list[PyType | None]  # None = not yet inferred
    return_type: PyType | None        # None = not yet inferred
    annotations: dict[str, PyType] = field(default_factory=dict)  # param name -> annotated type
    return_annotation: PyType | None = None
    body: list[ast.stmt] = field(default_factory=list)
    local_types: dict[str, PyType] = field(default_factory=dict)  # inferred local var types
    owner_class: Optional["ClassSig"] = None  # set for methods; None for top-level functions
    global_names: set[str] = field(default_factory=set)  # names this function declares `global`


# ---------------------------------------------------------------------------
# Class signature (basic classes: no inheritance, no class vars, no dunders
# besides __init__). Attribute types are inferred *only* from `self.X = ...`
# assignments seen anywhere a value of this class's type is the assignment
# target -- in practice that's almost always __init__, but nothing special-
# cases the method name, so an attribute set elsewhere (e.g. from top-level
# code after construction) is picked up too via the same fixed-point loop
# used for parameter inference.
# ---------------------------------------------------------------------------

@dataclass
class ClassSig:
    name: str
    methods: dict[str, FuncSig] = field(default_factory=dict)
    attributes: dict[str, PyType] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Type checker / IR builder
# ---------------------------------------------------------------------------

class TypeChecker:
    def __init__(self, source: str, filename: str = "<string>",
                 dangerous_ops: bool = False, dangerous_io_ops: bool = False):
        self.filename = filename
        self.source = source
        self.dangerous_ops = dangerous_ops
        self.dangerous_io_ops = dangerous_io_ops
        self._funcs: dict[str, FuncSig] = {}
        self._classes: dict[str, ClassSig] = {}
        self._global_vars: dict[str, PyType] = {}
        self._global_assigns: dict[str, list[tuple[PyType, ast.AST]]] = {}
        self._errors: list[CompileError] = []
        # None until _compute_reachable runs -- treated as "everything is
        # reachable" so a caller that never calls it (some tests construct
        # a TypeChecker and drive pass1/pass2 by hand) sees the old,
        # unfiltered behavior.
        self._reachable_funcs: set[str] | None = None
        self._reachable_classes: set[str] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self) -> list[IR.IRNode]:
        """Run both passes, return IR. Raises CompileError on failure."""
        tree = ast.parse(self.source, filename=self.filename)
        self._pass1_collect(tree)
        self._compute_reachable(tree)
        self._pass2_infer_and_validate()
        if self._errors:
            for e in self._errors:
                print(str(e), file=sys.stderr)
            raise self._errors[0]
        return self._build_ir(tree)

    # ------------------------------------------------------------------
    # Pass 1: collect definitions
    # ------------------------------------------------------------------

    def _pass1_collect(self, tree: ast.Module):
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef):
                self._collect_func(node)
            elif isinstance(node, ast.ClassDef):
                self._collect_class(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                self._collect_global_assign(node)
            # Other top-level statements are handled in pass 2.

    def _build_func_sig(self, node: ast.FunctionDef) -> FuncSig:
        param_names = []
        annotations = {}
        for arg in node.args.args:
            param_names.append(arg.arg)
            ann = _parse_annotation(arg.annotation, self.filename)
            if ann is not None:
                annotations[arg.arg] = ann

        ret_ann = _parse_annotation(node.returns, self.filename)

        # `global x` can legally appear anywhere in the function body
        # (inside an if/while/for, not just at the top), so this walks
        # the whole subtree rather than just node.body's direct
        # statements. Whether each name is actually a real module-level
        # global gets validated later, in _infer_func_body -- pass 1
        # (which calls this) hasn't finished collecting global
        # assignments yet if this function happens to appear earlier in
        # the source than the global it references.
        global_names: set[str] = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Global):
                global_names.update(n.names)

        return FuncSig(
            name=node.name,
            param_names=param_names,
            param_types=[annotations.get(a) for a in param_names],
            return_type=ret_ann,
            annotations=annotations,
            return_annotation=ret_ann,
            body=node.body,
            global_names=global_names,
        )

    def _collect_func(self, node: ast.FunctionDef):
        self._funcs[node.name] = self._build_func_sig(node)

    def _collect_class(self, node: ast.ClassDef):
        if node.bases or node.keywords:
            self._errors.append(_error(self.filename, node,
                "Class inheritance is not supported (basic classes only)"))
        class_sig = ClassSig(name=node.name)
        class_type = PyType(PyTypeKind.CLASS, node.name)
        for item in node.body:
            if isinstance(item, ast.FunctionDef):
                method_sig = self._build_func_sig(item)
                method_sig.owner_class = class_sig
                if not method_sig.param_names:
                    self._errors.append(_error(self.filename, item,
                        f"Method '{item.name}' must take at least a 'self' parameter"))
                else:
                    # self's type is always statically known: the owning
                    # class. Pre-seed it (rather than special-casing "self"
                    # by name elsewhere) so the existing generic
                    # param-inference/validation machinery just works.
                    method_sig.param_types[0] = class_type
                class_sig.methods[item.name] = method_sig
            elif isinstance(item, ast.Pass):
                pass
            else:
                self._errors.append(_error(self.filename, item,
                    "Only method definitions are supported in a class body "
                    "(basic classes only)"))
        self._classes[node.name] = class_sig

    def _all_func_sigs(self):
        for sig in self._funcs.values():
            yield sig
        for class_sig in self._classes.values():
            for method_sig in class_sig.methods.values():
                yield method_sig

    def _reachable_func_sigs(self):
        """Same as _all_func_sigs(), but filtered to what
        _compute_reachable found -- a function/class never called
        (directly or transitively) from the module's own top-level
        executable statements shouldn't block compilation just because
        its parameter types can't be inferred from a call site that
        doesn't exist: nothing depends on it being type-correct, so it's
        simply not compiled at all, same as if it weren't there."""
        if self._reachable_funcs is None:
            yield from self._all_func_sigs()
            return
        for name, sig in self._funcs.items():
            if name in self._reachable_funcs:
                yield sig
        for cname, class_sig in self._classes.items():
            if cname in self._reachable_classes:
                for method_sig in class_sig.methods.values():
                    yield method_sig

    def _compute_reachable(self, tree: ast.Module) -> None:
        """Populate self._reachable_funcs/_reachable_classes: every
        function/class name reachable by following ast.Call targets
        starting from the module's own top-level executable statements
        (anything not itself a FunctionDef/ClassDef -- typically just the
        final `main()` call), transitively through the bodies of
        whatever that reaches. A class becomes reachable as a whole (all
        its methods included) as soon as its constructor is called
        anywhere reachable -- method calls are always `obj.method(...)`
        (an ast.Attribute, not a bare ast.Name), so they can't be
        resolved to a specific method by name alone without full type
        info, and don't need to be: every method of an already-reachable
        class is safe to treat as reachable too.
        """
        self._reachable_funcs = set()
        self._reachable_classes = set()

        def called_names(stmts) -> set[str]:
            names = set()
            for stmt in stmts:
                for n in ast.walk(stmt):
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                        names.add(n.func.id)
            return names

        top_level = [s for s in tree.body
                     if not isinstance(s, (ast.FunctionDef, ast.ClassDef))]
        # EXTERNALLY_CALLED_FUNCS are called from hand-written C++, not
        # from anything an ast.Call could ever point at in this source --
        # always seed them as reachable rather than have them look like
        # dead code.
        worklist = list(called_names(top_level)) + list(EXTERNALLY_CALLED_FUNCS)
        while worklist:
            name = worklist.pop()
            if name in self._funcs:
                if name in self._reachable_funcs:
                    continue
                self._reachable_funcs.add(name)
                worklist.extend(called_names(self._funcs[name].body))
            elif name in self._classes:
                if name in self._reachable_classes:
                    continue
                self._reachable_classes.add(name)
                for method_sig in self._classes[name].methods.values():
                    worklist.extend(called_names(method_sig.body))

    def _collect_global_assign(self, node: ast.stmt):
        names = self._collect_assign_targets(node)
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            val_node = node.value
        else:
            val_node = None
        for n in names:
            self._global_assigns.setdefault(n, [])
            if val_node is not None:
                self._global_assigns[n].append((PYUNKNOWN, val_node))

    def _collect_assign_targets(self, node: ast.stmt) -> list[str]:
        targets: list[str] = []
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    targets.append(t.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                targets.append(node.target.id)
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name):
                targets.append(node.target.id)
        elif isinstance(node, ast.For):
            if isinstance(node.target, ast.Name):
                targets.append(node.target.id)
        return targets

    # ------------------------------------------------------------------
    # Pass 2: infer types, validate annotations
    # ------------------------------------------------------------------

    def _pass2_infer_and_validate(self):
        # Globals are inferred first: a function body may read a
        # module-level global, and a later global's initializer may
        # reference an earlier one -- neither should depend on function
        # processing order.
        for name, assigns in self._global_assigns.items():
            self._infer_global_var(name, assigns)

        # A function's parameter/return types can be discovered either from
        # its own annotations or from how *other* functions call it, and
        # those other functions may be defined earlier or later in the
        # source. So iterate body inference across all functions to a fixed
        # point -- silently, without recording errors, since intermediate
        # rounds see only partially-resolved types and would otherwise
        # raise spurious errors -- before validating anything.
        max_rounds = len(self._funcs) + len(self._classes) + 2
        previous_snapshot = None
        for _ in range(max_rounds):
            saved_errors, self._errors = self._errors, []
            for sig in self._reachable_func_sigs():
                self._infer_func_body(sig)
            self._errors = saved_errors
            snapshot = self._signature_snapshot()
            if snapshot == previous_snapshot:
                break
            previous_snapshot = snapshot

        # Final pass: re-run body inference once more (types are now
        # stable) so genuine errors get recorded, then validate each
        # signature against its annotations.
        for sig in self._reachable_func_sigs():
            self._infer_func_body(sig)
        for sig in self._reachable_func_sigs():
            self._validate_func_signature(sig)
        for cname, class_sig in self._classes.items():
            if self._reachable_classes is not None and cname not in self._reachable_classes:
                continue
            self._validate_str_dunder(class_sig)
            self._validate_comparison_dunders(class_sig)

    def _validate_comparison_dunders(self, class_sig: ClassSig):
        # p1 == p2 etc. dispatches to a comparison dunder (see
        # _check_class_comparison / CMP_DUNDER); each must take exactly one
        # argument besides 'self' and return a bool.
        for dunder in CMP_DUNDER.values():
            method_sig = class_sig.methods.get(dunder)
            if method_sig is None:
                continue
            if len(method_sig.param_names) != 2:
                self._errors.append(_error(self.filename, method_sig.body[0],
                    f"'{dunder}' must take exactly one argument other than 'self'"))
            if method_sig.return_type is not None and method_sig.return_type != PYBOOL:
                self._errors.append(_error(self.filename, method_sig.body[0],
                    f"'{dunder}' must return a bool, not {method_sig.return_type}"))

    def _validate_str_dunder(self, class_sig: ClassSig):
        # print(instance) calls __str__ (falling back to __repr__) if the
        # class defines one -- see CppCodegen._resolve_print_arg. Validated
        # here rather than by the generic per-method checks in
        # _validate_func_signature, since only these two names need this
        # extra "no args, returns str" shape check.
        for dunder in ("__str__", "__repr__"):
            method_sig = class_sig.methods.get(dunder)
            if method_sig is None:
                continue
            if len(method_sig.param_names) != 1:
                self._errors.append(_error(self.filename, method_sig.body[0],
                    f"'{dunder}' must take no arguments other than 'self'"))
            if method_sig.return_type is not None and method_sig.return_type != PYSTRING:
                self._errors.append(_error(self.filename, method_sig.body[0],
                    f"'{dunder}' must return a str, not {method_sig.return_type}"))

    def _signature_snapshot(self):
        funcs = {
            name: (tuple(sig.param_types), sig.return_type)
            for name, sig in self._funcs.items()
        }
        classes = {
            name: (
                tuple(sorted(class_sig.attributes.items(), key=lambda kv: kv[0])),
                {mname: (tuple(msig.param_types), msig.return_type)
                 for mname, msig in class_sig.methods.items()},
            )
            for name, class_sig in self._classes.items()
        }
        return (funcs, classes)

    def _infer_func_body(self, sig: FuncSig):
        """Run one round of type inference over a function's body.

        Seeds the environment from explicit annotations first (the source
        of truth for the function's own reasoning about its parameters),
        falling back to whatever parameter type call sites have discovered
        so far. Mutates sig.return_type/param_types (and other functions'
        param_types, via calls found in the body); does not validate
        consistency -- see _validate_func_signature for that.
        """
        env: dict[str, PyType] = {}
        for i, pname in enumerate(sig.param_names):
            if pname in sig.annotations:
                env[pname] = sig.annotations[pname]
            elif sig.param_types[i] is not None:
                env[pname] = sig.param_types[i]

        # A name declared `global` shares the module-level variable's
        # type rather than getting its own -- seeded here from whatever
        # _infer_global_var (which _pass2_infer_and_validate runs before
        # any function body) already determined, so reads inside this
        # function see the real type immediately. Assigning to it here
        # (validated at the module level to already be a real global, so
        # a plain unassigned name always errors instead of silently
        # becoming an unrelated fresh local) may narrow/refine that type
        # further, which gets unified back into self._global_vars below
        # once the whole body's been walked -- the same consistency
        # check every other assignment to that global already goes
        # through.
        for gname in sig.global_names:
            if gname not in self._global_assigns:
                self._errors.append(_error(self.filename, sig.body[0],
                    f"'{gname}' is declared global in '{sig.name}' but is not "
                    f"assigned anywhere at module level"))
            elif gname in self._global_vars:
                env[gname] = self._global_vars[gname]

        for stmt in sig.body:
            self._infer_stmt(stmt, env, sig)

        for gname in sig.global_names:
            if gname in env:
                old = self._global_vars.get(gname)
                if old is None:
                    self._global_vars[gname] = env[gname]
                else:
                    try:
                        self._global_vars[gname] = unify(old, env[gname])
                    except TypeError as exc:
                        self._errors.append(_error(self.filename, sig.body[0], str(exc)))

        # Store inferred local types for IR builder
        sig.local_types = dict(env)

    def _validate_func_signature(self, sig: FuncSig):
        # Validate param types
        for i, pname in enumerate(sig.param_names):
            if pname in sig.annotations:
                # Annotation present — check consistency
                if sig.param_types[i] is not None and sig.param_types[i] != sig.annotations[pname]:
                    self._errors.append(_error(self.filename, sig.body[0],
                        f"Parameter '{pname}' annotation {sig.annotations[pname]} "
                        f"inconsistent with inferred type {sig.param_types[i]}"))
            else:
                if sig.param_types[i] is None:
                    self._errors.append(_error(self.filename, sig.body[0],
                        f"Cannot infer type of parameter '{pname}' in function '{sig.name}'"))
        # Validate return type
        if sig.return_annotation is not None:
            if sig.return_type is not None and sig.return_type != sig.return_annotation:
                self._errors.append(_error(self.filename, sig.body[0],
                    f"Return annotation {sig.return_annotation} inconsistent with "
                    f"inferred return type {sig.return_type}"))
            sig.return_type = sig.return_annotation
        else:
            if sig.return_type is None:
                sig.return_type = PYNONE  # no return statements → None
            # Check it was inferred (not still UNKNOWN)
            if sig.return_type.kind == PyTypeKind.UNKNOWN:
                self._errors.append(_error(self.filename, sig.body[0],
                    f"Cannot infer return type of function '{sig.name}'"))
            # If never called, type is still from returns only — that's OK if
            # there's at least one return. If no returns and no calls, it's PYNONE.

    def _infer_global_var(self, name: str, assigns: list[tuple[PyType, ast.AST]]):
        inferred = PYUNKNOWN
        for _, val_node in assigns:
            t = self._infer_expr(val_node, {}, {})
            try:
                inferred = unify(inferred, t) if inferred.kind != PyTypeKind.UNKNOWN else t
            except TypeError as exc:
                self._errors.append(_error(self.filename, val_node, str(exc)))
        if inferred.kind == PyTypeKind.UNKNOWN:
            self._errors.append(_error(self.filename, assigns[0][1],
                f"Cannot infer type of global variable '{name}'"))
        self._global_vars[name] = inferred

    def _infer_stmt(self, stmt: ast.stmt, env: dict[str, PyType], sig: FuncSig):
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    t = self._infer_expr(stmt.value, env, sig)
                    old = env.get(target.id)
                    if old is None:
                        env[target.id] = t
                    else:
                        try:
                            env[target.id] = unify(old, t)
                        except TypeError as exc:
                            self._errors.append(_error(self.filename, stmt, str(exc)))
                elif isinstance(target, ast.Attribute):
                    obj_type = self._infer_expr(target.value, env, sig)
                    val_type = self._infer_expr(stmt.value, env, sig)
                    if obj_type.kind == PyTypeKind.CLASS and obj_type.class_name in self._classes:
                        class_sig = self._classes[obj_type.class_name]
                        old = class_sig.attributes.get(target.attr)
                        if old is None:
                            class_sig.attributes[target.attr] = val_type
                        else:
                            try:
                                class_sig.attributes[target.attr] = unify(old, val_type)
                            except TypeError as exc:
                                self._errors.append(_error(self.filename, stmt, str(exc)))
                    elif obj_type.kind != PyTypeKind.UNKNOWN:
                        self._errors.append(_error(self.filename, stmt,
                            f"Attribute assignment is only supported on class instances, got {obj_type}"))
        elif isinstance(stmt, ast.AugAssign):
            if isinstance(stmt.target, ast.Name):
                t = self._infer_expr(stmt.value, env, sig)
                old = env.get(stmt.target.id)
                if old is not None:
                    try:
                        env[stmt.target.id] = unify(old, t)
                    except TypeError as exc:
                        self._errors.append(_error(self.filename, stmt, str(exc)))
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name):
                ann = _parse_annotation(stmt.annotation, self.filename)
                if ann is not None:
                    env[stmt.target.id] = ann
        elif isinstance(stmt, ast.Return):
            if stmt.value:
                t = self._infer_expr(stmt.value, env, sig)
                if sig.return_type is None:
                    sig.return_type = t
                else:
                    try:
                        sig.return_type = unify(sig.return_type, t)
                    except TypeError as exc:
                        self._errors.append(_error(self.filename, stmt, str(exc)))
        elif isinstance(stmt, ast.If):
            for s in stmt.body:
                self._infer_stmt(s, env, sig)
            for s in stmt.orelse:
                self._infer_stmt(s, env, sig)
        elif isinstance(stmt, ast.While):
            self._infer_expr(stmt.test, env, sig)
            for s in stmt.body:
                self._infer_stmt(s, env, sig)
        elif isinstance(stmt, ast.For):
            if isinstance(stmt.target, ast.Name):
                iter_type = self._infer_expr(stmt.iter, env, sig)
                if isinstance(stmt.iter, ast.Call):
                    func_name = ""
                    if isinstance(stmt.iter.func, ast.Name):
                        func_name = stmt.iter.func.id
                    if func_name == "range":
                        env[stmt.target.id] = PYINT
                    else:
                        env[stmt.target.id] = iter_type
                else:
                    env[stmt.target.id] = iter_type
            for s in stmt.body:
                self._infer_stmt(s, env, sig)
            for s in stmt.orelse:
                self._infer_stmt(s, env, sig)
        elif isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Call):
                self._infer_call(stmt.value, env, sig)
        elif isinstance(stmt, (ast.Break, ast.Continue, ast.Pass, ast.Global)):
            pass  # `global` itself is handled in _build_func_sig/_infer_func_body

    def _infer_call(self, call: ast.Call, env: dict[str, PyType], sig: FuncSig) -> PyType:
        if isinstance(call.func, ast.Attribute):
            return self._infer_method_call(call, env, sig)

        func_name = ""
        if isinstance(call.func, ast.Name):
            func_name = call.func.id

        if func_name in self._classes:
            class_sig = self._classes[func_name]
            init_sig = class_sig.methods.get("__init__")
            if init_sig is None:
                if len(call.args) != 0:
                    self._errors.append(_error(self.filename, call,
                        f"'{func_name}' takes no arguments (no __init__ defined)"))
                for arg in call.args:
                    self._infer_expr(arg, env, sig)
            else:
                expected = len(init_sig.param_names) - 1
                if len(call.args) != expected:
                    self._errors.append(_error(self.filename, call,
                        f"'{func_name}' constructor takes {expected} argument(s), "
                        f"got {len(call.args)}"))
                for i, arg in enumerate(call.args):
                    at = self._infer_expr(arg, env, sig)
                    pi = i + 1  # skip 'self'
                    if pi < len(init_sig.param_names):
                        if init_sig.param_types[pi] is None:
                            init_sig.param_types[pi] = at
                        else:
                            try:
                                init_sig.param_types[pi] = unify(init_sig.param_types[pi], at)
                            except TypeError as exc:
                                self._errors.append(_error(self.filename, call, str(exc)))
            return PyType(PyTypeKind.CLASS, func_name)

        if func_name == "print":
            for arg in call.args:
                self._infer_expr(arg, env, sig)
            # Validate no non-default sep/end
            for kw in call.keywords:
                if kw.arg in ("sep", "end"):
                    self._errors.append(_error(self.filename, call,
                        f"print() keyword argument '{kw.arg}' is not supported in v1 "
                        "(awaiting str support in v2)"))
            return PYNONE

        if func_name in ("peek8", "peek16", "peek32", "peek64"):
            if not self.dangerous_ops:
                self._errors.append(_error(self.filename, call,
                    f"'{func_name}' is undefined (use --enable-dangerous-memory-ops)"))
            if len(call.args) != 1:
                self._errors.append(_error(self.filename, call,
                    f"'{func_name}' takes exactly 1 argument"))
            return PYINT

        if func_name in ("poke8", "poke16", "poke32", "poke64"):
            if not self.dangerous_ops:
                self._errors.append(_error(self.filename, call,
                    f"'{func_name}' is undefined (use --enable-dangerous-memory-ops)"))
            if len(call.args) != 2:
                self._errors.append(_error(self.filename, call,
                    f"'{func_name}' takes exactly 2 arguments"))
            return PYNONE

        if func_name in ("raw_memzero", "raw_memcpy"):
            # Bulk versions of poke8/peek8+poke8, processing many bytes
            # in one call instead of one PyCk-level loop iteration per
            # byte -- needed for anything much bigger than a few KB
            # (e.g. a several-hundred-KB ELF segment, see
            # PythonOS/kernel/elf.py's load_elf()): this runtime's
            # PyObject heap is a bump allocator that never frees (see
            # arch/x86_64_graphical/support.cpp's py_free()), so a
            # byte-at-a-time PyCk loop's own boxed loop-counter/address
            # allocations exhaust it long before the copy finishes.
            if not self.dangerous_ops:
                self._errors.append(_error(self.filename, call,
                    f"'{func_name}' is undefined (use --enable-dangerous-memory-ops)"))
            expected_argc = 2 if func_name == "raw_memzero" else 3
            if len(call.args) != expected_argc:
                plural = "" if expected_argc == 1 else "s"
                self._errors.append(_error(self.filename, call,
                    f"'{func_name}' takes exactly {expected_argc} argument{plural}"))
            return PYNONE

        if func_name == "_ASM":
            # Unlike every other hardware intrinsic, _ASM's argument isn't
            # a runtime value -- it's spliced verbatim into the compiled
            # kernel as a literal `asm volatile(...)` block (see
            # codegen.py's _emit_asm_call), so it has to be a string
            # literal actually present in the source, not a computed
            # value. Fine for instruction sequences that need no address
            # at all (segment-register reloads with fixed selectors, a
            # push/lretq far return to a local label, ...); when an
            # instruction needs an actual runtime pointer (a real,
            # dynamically-allocated address, not a hand-picked one), use
            # _ASM1 instead.
            if not self.dangerous_io_ops:
                self._errors.append(_error(self.filename, call,
                    "'_ASM' is undefined (use --enable-dangerous-io-ops)"))
            if len(call.args) != 1:
                self._errors.append(_error(self.filename, call,
                    "'_ASM' takes exactly 1 argument"))
            elif not (isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)):
                self._errors.append(_error(self.filename, call,
                    "'_ASM' requires its argument to be a string literal"))
            return PYNONE

        if func_name == "_ASM1":
            # Like _ASM, but the template gets one real GCC extended-asm
            # input operand (referenced as %0 in the template) bound to
            # `value` -- a genuine runtime value (e.g. an address handed
            # back by VirtualAllocator.alloc()), not a hand-picked
            # literal. The template itself is still a compile-time
            # string literal for the same reason _ASM's is: it becomes
            # literal C++ source text, so there's nothing to splice a
            # computed *string* into -- but %0 lets the *value* stay
            # dynamic. Since %0 is a real operand reference here (unlike
            # _ASM's template, which has none), any literal '%' the
            # template needs for its own sake must be written doubled
            # ("%%") by hand, the normal GCC extended-asm convention.
            if not self.dangerous_io_ops:
                self._errors.append(_error(self.filename, call,
                    "'_ASM1' is undefined (use --enable-dangerous-io-ops)"))
            if len(call.args) != 2:
                self._errors.append(_error(self.filename, call,
                    "'_ASM1' takes exactly 2 arguments"))
            elif not (isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)):
                self._errors.append(_error(self.filename, call,
                    "'_ASM1' requires its first argument to be a string literal"))
            if len(call.args) == 2:
                self._infer_expr(call.args[1], env, sig)
            return PYNONE

        if func_name in HARDWARE_INTRINSICS:
            if not self.dangerous_io_ops:
                self._errors.append(_error(self.filename, call,
                    f"'{func_name}' is undefined (use --enable-dangerous-io-ops)"))
            expected_argc, ret_type = HARDWARE_INTRINSICS[func_name]
            if len(call.args) != expected_argc:
                plural = "" if expected_argc == 1 else "s"
                self._errors.append(_error(self.filename, call,
                    f"'{func_name}' takes exactly {expected_argc} argument{plural}"))
            for arg in call.args:
                self._infer_expr(arg, env, sig)
            return ret_type

        if func_name in LIMINE_INTRINSICS:
            expected_argc, ret_type = LIMINE_INTRINSICS[func_name]
            if len(call.args) != expected_argc:
                plural = "" if expected_argc == 1 else "s"
                self._errors.append(_error(self.filename, call,
                    f"'{func_name}' takes exactly {expected_argc} argument{plural}"))
            for arg in call.args:
                self._infer_expr(arg, env, sig)
            return ret_type

        if func_name in ISR_INTRINSICS:
            expected_argc, ret_type = ISR_INTRINSICS[func_name]
            if len(call.args) != expected_argc:
                plural = "" if expected_argc == 1 else "s"
                self._errors.append(_error(self.filename, call,
                    f"'{func_name}' takes exactly {expected_argc} argument{plural}"))
            for arg in call.args:
                self._infer_expr(arg, env, sig)
            return ret_type

        if func_name == "chr":
            # chr(code) -> a single-character string; "+" between two such
            # strings concatenates them (see PyStrObject in
            # runtime/freestanding/pyck_runtime.h). Always available; not
            # gated behind either dangerous-ops flag, since it touches no
            # memory/hardware.
            if len(call.args) != 1:
                self._errors.append(_error(self.filename, call,
                    "'chr' takes exactly 1 argument"))
            for arg in call.args:
                self._infer_expr(arg, env, sig)
            return PYSTRING

        if func_name == "len":
            # len(s) -> the number of bytes in a str value. Always
            # available, like chr(); str is the only sized/container-like
            # type in this subset.
            if len(call.args) != 1:
                self._errors.append(_error(self.filename, call,
                    "'len' takes exactly 1 argument"))
            arg_types = [self._infer_expr(arg, env, sig) for arg in call.args]
            if len(arg_types) == 1 and arg_types[0].kind not in (PyTypeKind.STR, PyTypeKind.UNKNOWN):
                self._errors.append(_error(self.filename, call,
                    f"'len' is only supported on str values, got {arg_types[0]}"))
            return PYINT

        if func_name == "hex":
            # hex(n) -> lowercase "0x..." string ("-0x..." if negative),
            # matching real Python's hex(). Always available, like
            # chr()/len(); not gated behind any flag since it's pure
            # computation, no memory/hardware access.
            if len(call.args) != 1:
                self._errors.append(_error(self.filename, call,
                    "'hex' takes exactly 1 argument"))
            arg_types = [self._infer_expr(arg, env, sig) for arg in call.args]
            if len(arg_types) == 1 and arg_types[0].kind not in (PyTypeKind.INT, PyTypeKind.BOOL, PyTypeKind.UNKNOWN):
                self._errors.append(_error(self.filename, call,
                    f"'hex' is only supported on int values, got {arg_types[0]}"))
            return PYSTRING

        if func_name == "range":
            # range() → used only in for loops; return type doesn't matter
            for arg in call.args:
                self._infer_expr(arg, env, sig)
            return PYINT  # placeholder

        # User-defined function
        if func_name in self._funcs:
            fsig = self._funcs[func_name]
            for i, arg in enumerate(call.args):
                at = self._infer_expr(arg, env, sig)
                if i < len(fsig.param_names):
                    pname = fsig.param_names[i]
                    if fsig.param_types[i] is None:
                        fsig.param_types[i] = at
                    else:
                        try:
                            fsig.param_types[i] = unify(fsig.param_types[i], at)
                        except TypeError as exc:
                            self._errors.append(_error(self.filename, call, str(exc)))
            rt = fsig.return_type
            if rt is None or rt.kind == PyTypeKind.UNKNOWN:
                return PYUNKNOWN
            return rt

        # Builtins (range already handled above)
        self._errors.append(_error(self.filename, call, f"Undefined function '{func_name}'"))
        return PYUNKNOWN

    def _check_class_comparison(self, node: ast.Compare, op: ast.cmpop,
                                 left_type: PyType, right_type: PyType):
        """Validate one leg of a chained comparison where either side is a
        class instance, and feed the other operand's type into the
        dunder's 'other' parameter (same as any other method call site) so
        its type gets discovered/unified through the normal fixed-point
        loop. See ast_to_ir.py's _build_class_compare for the IR side.
        """
        if (left_type.kind != PyTypeKind.CLASS or right_type.kind != PyTypeKind.CLASS
                or left_type.class_name != right_type.class_name):
            self._errors.append(_error(self.filename, node,
                f"Cannot compare {left_type} and {right_type}"))
            return
        class_sig = self._classes.get(left_type.class_name)
        if class_sig is None:
            return
        dunder = CMP_DUNDER[type(op)]
        effective = dunder
        if dunder == "__ne__" and dunder not in class_sig.methods and "__eq__" in class_sig.methods:
            effective = "__eq__"  # __ne__ falls back to `not __eq__`
        method_sig = class_sig.methods.get(effective)
        if method_sig is None:
            self._errors.append(_error(self.filename, node,
                f"'{left_type.class_name}' does not support '{_CMP_SYMBOL[type(op)]}' "
                f"(no {dunder} defined)"))
            return
        if len(method_sig.param_names) > 1:
            if method_sig.param_types[1] is None:
                method_sig.param_types[1] = right_type
            else:
                try:
                    method_sig.param_types[1] = unify(method_sig.param_types[1], right_type)
                except TypeError as exc:
                    self._errors.append(_error(self.filename, node, str(exc)))

    def _infer_method_call(self, call: ast.Call, env: dict[str, PyType], sig: FuncSig) -> PyType:
        assert isinstance(call.func, ast.Attribute)
        obj_type = self._infer_expr(call.func.value, env, sig)
        method_name = call.func.attr

        if obj_type.kind != PyTypeKind.CLASS or obj_type.class_name not in self._classes:
            if obj_type.kind != PyTypeKind.UNKNOWN:
                self._errors.append(_error(self.filename, call,
                    f"Method call is only supported on class instances, got {obj_type}"))
            for arg in call.args:
                self._infer_expr(arg, env, sig)
            return PYUNKNOWN

        class_sig = self._classes[obj_type.class_name]
        if method_name not in class_sig.methods:
            self._errors.append(_error(self.filename, call,
                f"'{obj_type.class_name}' has no method '{method_name}'"))
            for arg in call.args:
                self._infer_expr(arg, env, sig)
            return PYUNKNOWN

        method_sig = class_sig.methods[method_name]
        expected = len(method_sig.param_names) - 1
        if len(call.args) != expected:
            self._errors.append(_error(self.filename, call,
                f"'{method_name}' takes {expected} argument(s), got {len(call.args)}"))
        for i, arg in enumerate(call.args):
            at = self._infer_expr(arg, env, sig)
            pi = i + 1  # skip 'self'
            if pi < len(method_sig.param_names):
                if method_sig.param_types[pi] is None:
                    method_sig.param_types[pi] = at
                else:
                    try:
                        method_sig.param_types[pi] = unify(method_sig.param_types[pi], at)
                    except TypeError as exc:
                        self._errors.append(_error(self.filename, call, str(exc)))
        rt = method_sig.return_type
        if rt is None or rt.kind == PyTypeKind.UNKNOWN:
            return PYUNKNOWN
        return rt

    def _infer_expr(self, node: ast.expr, env: dict[str, PyType], sig: FuncSig) -> PyType:
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                return PYBOOL
            if isinstance(v, int):
                return PYINT
            if isinstance(v, float):
                return PYFLOAT
            if v is None:
                return PYNONE
            if isinstance(v, str):
                # String literals are only allowed inside print() arguments (v1 limitation)
                return PYSTRING
            self._errors.append(_error(self.filename, node, f"Unsupported constant type: {type(v).__name__}"))
            return PYUNKNOWN

        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            if node.id in self._global_vars:
                return self._global_vars[node.id]
            # Builtins that are values
            if node.id in ("True", "False"):
                return PYBOOL
            if node.id == "None":
                return PYNONE
            self._errors.append(_error(self.filename, node, f"Undefined variable '{node.id}'"))
            return PYUNKNOWN

        if isinstance(node, ast.BinOp):
            lt = self._infer_expr(node.left, env, sig)
            rt = self._infer_expr(node.right, env, sig)
            return self._infer_binop_type(node.op, lt, rt, node)

        if isinstance(node, ast.UnaryOp):
            ot = self._infer_expr(node.operand, env, sig)
            if isinstance(node.op, ast.USub):
                return ot
            if isinstance(node.op, ast.Not):
                return PYBOOL
            return ot

        if isinstance(node, ast.Compare):
            left_type = self._infer_expr(node.left, env, sig)
            for op, comparator in zip(node.ops, node.comparators):
                right_type = self._infer_expr(comparator, env, sig)
                if left_type.kind == PyTypeKind.CLASS or right_type.kind == PyTypeKind.CLASS:
                    self._check_class_comparison(node, op, left_type, right_type)
                left_type = right_type
            return PYBOOL

        if isinstance(node, ast.BoolOp):
            return PYBOOL

        if isinstance(node, ast.Call):
            return self._infer_call(node, env, sig)

        if isinstance(node, ast.IfExp):
            self._infer_expr(node.test, env, sig)
            bt = self._infer_expr(node.body, env, sig)
            ot = self._infer_expr(node.orelse, env, sig)
            try:
                return unify(bt, ot)
            except TypeError:
                return bt

        if isinstance(node, ast.Subscript):
            base_type = self._infer_expr(node.value, env, sig)
            if base_type.kind != PyTypeKind.STR:
                self._errors.append(_error(self.filename, node,
                    f"Subscript/slice is only supported on str values, got {base_type}"))
            if isinstance(node.slice, ast.Slice):
                if node.slice.lower is not None:
                    self._infer_expr(node.slice.lower, env, sig)
                if node.slice.upper is not None:
                    self._infer_expr(node.slice.upper, env, sig)
                if node.slice.step is not None:
                    self._infer_expr(node.slice.step, env, sig)
                    if (isinstance(node.slice.step, ast.Constant) and
                            isinstance(node.slice.step.value, int) and
                            node.slice.step.value == 0):
                        self._errors.append(_error(self.filename, node,
                            "slice step cannot be zero"))
                return PYSTRING
            else:
                self._infer_expr(node.slice, env, sig)
                return PYSTRING  # indexing a string always yields a 1-char string

        if isinstance(node, ast.Attribute):
            obj_type = self._infer_expr(node.value, env, sig)
            if obj_type.kind == PyTypeKind.CLASS and obj_type.class_name in self._classes:
                class_sig = self._classes[obj_type.class_name]
                if node.attr in class_sig.attributes:
                    return class_sig.attributes[node.attr]
                self._errors.append(_error(self.filename, node,
                    f"'{obj_type.class_name}' has no attribute '{node.attr}'"))
                return PYUNKNOWN
            if obj_type.kind != PyTypeKind.UNKNOWN:
                self._errors.append(_error(self.filename, node,
                    f"Attribute access is only supported on class instances, got {obj_type}"))
            return PYUNKNOWN

        self._errors.append(_error(self.filename, node, f"Unsupported expression: {type(node).__name__}"))
        return PYUNKNOWN

    def _infer_binop_type(self, op: ast.operator, lt: PyType, rt: PyType,
                          node: ast.expr) -> PyType:
        if isinstance(op, ast.Div):
            # / always returns float
            return PYFLOAT
        if isinstance(op, ast.FloorDiv):
            return PYINT
        if isinstance(op, ast.Mod):
            return PYINT
        if isinstance(op, ast.Pow):
            return PYINT
        if isinstance(op, ast.Mult):
            # str * int / int * str repeats the string -- unify() would
            # otherwise reject STR mixed with INT/BOOL as incompatible.
            kinds = {lt.kind, rt.kind}
            if PyTypeKind.STR in kinds and kinds <= {PyTypeKind.STR, PyTypeKind.INT, PyTypeKind.BOOL}:
                return PYSTRING
        try:
            return unify(lt, rt)
        except TypeError as exc:
            self._errors.append(_error(self.filename, node, str(exc)))
            return PYUNKNOWN
