"""Escape analysis for stack-allocating non-escaping class instances.

Freestanding-mode class instances are heap-allocated via py_alloc() and
(today) never freed on reassignment or scope exit -- codegen has no general
GC or destructor-on-scope-exit for locals, so every local class instance is
a permanent leak. Any instance whose lifetime is provably confined to the
function activation that creates it can instead live in that function's
C++ stack frame: no allocation, no leak, freed automatically when the frame
pops.

Two conservative, sound passes, both used only for --target=freestanding
(hosted-mode instances are real CPython PyDict objects and can't be stack
allocated):

  1. Per-class "self safety" (`compute_self_safe_classes`): a class is
     eligible at all only if none of its methods (including __init__) can
     let `self` leak beyond the call -- returned, stored into a different
     object's attribute, assigned to a global/another variable, or passed
     as a plain argument anywhere. `self` may only ever appear as the
     receiver (`attr_target`) of an attribute/method access on itself.
     This is a whole-class property: methods are compiled once and shared
     by every instance, so one unsafe method disqualifies the class for
     every call site.

  2. Per local variable, per function (`analyze`): a `v = ClassName(args)`
     site is eligible only if `ClassName` is self-safe (1) and `v` is
     never used in any way other than as that same receiver position,
     anywhere in the function that creates it (recursively through
     if/while/for-range bodies, not into nested function/class defs).
     Any other use -- returned, passed as an argument, stored into
     another object's attribute, assigned to a global or another local,
     read in a print() call -- disqualifies it and it stays heap
     allocated exactly as before.

Both passes default to "escapes" for any IR shape they don't specifically
recognize as safe, rather than enumerating every unsafe pattern: a wrong
"doesn't escape" verdict here means a dangling pointer into a popped C++
stack frame, so the whitelist is deliberately narrow.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ir as IR

# ATTR_GET/ATTR_SET/METHOD_CALL are the only node kinds where a tracked
# name may appear as `attr_target` without counting as an escape -- it's
# being used as a receiver, not handed off anywhere.
_SAFE_RECEIVER_KINDS = (IR.IRNodeKind.ATTR_GET, IR.IRNodeKind.ATTR_SET, IR.IRNodeKind.METHOD_CALL)

# Every other IRNode field that can hold a single child expression.
_CHILD_NODE_FIELDS = (
    "left", "right", "operand", "test", "return_value", "range_start",
    "range_stop", "range_step", "peek_addr", "poke_addr", "poke_value",
    "subscript_target", "subscript_index", "slice_start", "slice_stop",
    "slice_step", "value", "attr_target",
)
# Every IRNode field that can hold a list of child statements/expressions.
_CHILD_LIST_FIELDS = ("body", "args", "print_args", "stmts", "else_body")


def _is_name(node: IR.IRNode | None, name: str) -> bool:
    return node is not None and node.kind == IR.IRNodeKind.NAME_LOAD and node.name == name


def _name_escapes(nodes: list[IR.IRNode], name: str) -> bool:
    """True if `name` is loaded anywhere in `nodes` other than as the
    receiver of an attribute/method access on itself."""

    def walk(node: IR.IRNode | None) -> bool:
        if node is None:
            return False
        if node.kind in (IR.IRNodeKind.FUNCTION_DEF, IR.IRNodeKind.CLASS_DEF):
            # Independent scope -- not reachable inside a function/method
            # body in this language (no nested defs), but stop here
            # rather than assume that.
            return False
        if node.kind == IR.IRNodeKind.NAME_LOAD:
            return node.name == name
        if node.kind in _SAFE_RECEIVER_KINDS:
            at = node.attr_target
            if not _is_name(at, name) and walk(at):
                return True
            if node.kind == IR.IRNodeKind.ATTR_SET and walk(node.value):
                return True
            if node.kind == IR.IRNodeKind.METHOD_CALL and any(walk(a) for a in node.args):
                return True
            return False
        for fname in _CHILD_NODE_FIELDS:
            if walk(getattr(node, fname)):
                return True
        for fname in _CHILD_LIST_FIELDS:
            if any(walk(n) for n in getattr(node, fname)):
                return True
        for cond, blk in node.elif_branches:
            if walk(cond) or any(walk(n) for n in blk):
                return True
        return False

    return any(walk(n) for n in nodes)


def _class_is_self_safe(cd: IR.IRNode) -> bool:
    for method in cd.class_methods:
        if not method.params:
            continue  # malformed/no receiver -- nothing to check
        self_name = method.params[0][0]
        if _name_escapes(method.body, self_name):
            return False
    return True


def compute_self_safe_classes(class_defs: list[IR.IRNode]) -> set[str]:
    """Classes none of whose methods can leak `self` beyond the call."""
    return {cd.class_name for cd in class_defs if _class_is_self_safe(cd)}


def _collect_class_new_assigns(stmts: list[IR.IRNode]) -> list[tuple[str, str]]:
    """(target, class_name) for every `target = ClassName(...)` in `stmts`,
    recursing into nested if/while/for-range bodies."""
    found: list[tuple[str, str]] = []

    def walk(nodes: list[IR.IRNode]) -> None:
        for node in nodes:
            if (node.kind == IR.IRNodeKind.ASSIGN and node.value is not None
                    and node.value.kind == IR.IRNodeKind.CLASS_NEW):
                found.append((node.target, node.value.class_name))
            if node.kind == IR.IRNodeKind.IF:
                walk(node.body)
                for _, blk in node.elif_branches:
                    walk(blk)
                walk(node.else_body)
            elif node.kind == IR.IRNodeKind.WHILE:
                walk(node.body)
            elif node.kind == IR.IRNodeKind.FOR_RANGE:
                walk(node.body)
            elif node.kind == IR.IRNodeKind.BLOCK:
                walk(node.stmts)

    walk(stmts)
    return found


def _compute_stack_locals_for_function(func_def: IR.IRNode, self_safe_classes: set[str]) -> dict[str, str]:
    candidates = _collect_class_new_assigns(func_def.body)
    targets = {t for t, _ in candidates}
    result: dict[str, str] = {}
    for target in targets:
        if target in func_def.global_names:
            continue
        class_names = {c for t, c in candidates if t == target}
        if len(class_names) != 1:
            continue  # type checker unifies to one class per name; be safe anyway
        (class_name,) = class_names
        if class_name not in self_safe_classes:
            continue
        if _name_escapes(func_def.body, target):
            continue
        result[target] = class_name
    return result


@dataclass
class EscapeInfo:
    self_safe_classes: set[str] = field(default_factory=set)
    # id(FUNCTION_DEF node) -> {local var name: class name}
    stack_locals: dict[int, dict[str, str]] = field(default_factory=dict)
    # Classes with >=1 stack-allocated call site somewhere in the program
    # -- codegen only needs to emit the extra stack PyTypeObject/dealloc/
    # constructor variant for these.
    stack_alloc_classes: set[str] = field(default_factory=set)

    def locals_for(self, func_def: IR.IRNode) -> dict[str, str]:
        return self.stack_locals.get(id(func_def), {})


def analyze(ir_stmts: list[IR.IRNode]) -> EscapeInfo:
    class_defs = [s for s in ir_stmts if s.kind == IR.IRNodeKind.CLASS_DEF]
    self_safe = compute_self_safe_classes(class_defs)

    funcs: list[IR.IRNode] = list(s for s in ir_stmts if s.kind == IR.IRNodeKind.FUNCTION_DEF)
    for cd in class_defs:
        funcs.extend(cd.class_methods)

    stack_locals: dict[int, dict[str, str]] = {}
    used_classes: set[str] = set()
    for func in funcs:
        locals_ = _compute_stack_locals_for_function(func, self_safe)
        if locals_:
            stack_locals[id(func)] = locals_
            used_classes.update(locals_.values())

    return EscapeInfo(self_safe_classes=self_safe, stack_locals=stack_locals,
                       stack_alloc_classes=used_classes)
