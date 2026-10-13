"""Unit tests for pyck.escape: the stack-allocation escape analysis.

Drives the real source -> IR pipeline (via helpers.build_ir) so these test
the same IR shapes codegen actually sees, then asserts directly on
escape.analyze()'s verdicts -- independent of how codegen renders them.
See tests/test_codegen.py's TestStackAllocation for the codegen-level
(C++ text) assertions, and test_integration_freestanding.py for real
g++ compile checks.
"""
import unittest

from pyck import ir as IR
from pyck import escape

from .helpers import build_ir


def _funcs_by_name(ir_stmts):
    out = {}
    for s in ir_stmts:
        if s.kind == IR.IRNodeKind.FUNCTION_DEF:
            out[s.func_name] = s
        elif s.kind == IR.IRNodeKind.CLASS_DEF:
            for m in s.class_methods:
                out[f"{s.class_name}.{m.func_name}"] = m
    return out


class TestSelfSafety(unittest.TestCase):
    def test_class_using_self_only_as_receiver_is_safe(self):
        src = (
            "class Point:\n"
            "    def __init__(self, x: int, y: int):\n"
            "        self.x = x\n"
            "        self.y = y\n"
            "    def total(self) -> int:\n"
            "        return self.x + self.y\n"
            "p = Point(1, 2)\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        self.assertIn("Point", info.self_safe_classes)

    def test_method_returning_self_is_unsafe(self):
        src = (
            "class Node:\n"
            "    def __init__(self, v: int):\n"
            "        self.v = v\n"
            "    def identity(self):\n"
            "        return self\n"
            "n = Node(1)\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        self.assertNotIn("Node", info.self_safe_classes)

    def test_storing_self_into_another_objects_attribute_is_unsafe(self):
        # Holder.child is inferred as Leaker-typed (from `Holder(leaker1)`
        # below), so `holder.child = self` inside Leaker.leak type-checks
        # -- self and the attribute are the same class.
        src = (
            "class Leaker:\n"
            "    def __init__(self):\n"
            "        self.v = 1\n"
            "    def leak(self, holder):\n"
            "        holder.child = self\n"
            "class Holder:\n"
            "    def __init__(self, first):\n"
            "        self.child = first\n"
            "def main():\n"
            "    leaker1 = Leaker()\n"
            "    leaker2 = Leaker()\n"
            "    holder = Holder(leaker1)\n"
            "    leaker2.leak(holder)\n"
            "main()\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        self.assertNotIn("Leaker", info.self_safe_classes)
        # Holder itself never leaks its own self -- unaffected by Leaker's problem.
        self.assertIn("Holder", info.self_safe_classes)

    def test_passing_self_as_a_plain_argument_is_unsafe(self):
        src = (
            "class Widget:\n"
            "    def __init__(self):\n"
            "        self.v = 1\n"
            "    def register(self):\n"
            "        track(self)\n"
            "def track(w):\n"
            "    pass\n"
            "w = Widget()\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        self.assertNotIn("Widget", info.self_safe_classes)

    def test_assigning_self_to_a_global_is_unsafe(self):
        src = (
            "class Singleton:\n"
            "    def __init__(self):\n"
            "        self.v = 1\n"
            "    def register(self):\n"
            "        global g\n"
            "        g = self\n"
            "g = Singleton()\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        self.assertNotIn("Singleton", info.self_safe_classes)

    def test_self_calling_its_own_method_is_safe(self):
        src = (
            "class Counter:\n"
            "    def __init__(self):\n"
            "        self.n = 0\n"
            "    def bump(self):\n"
            "        self.n = self.n + 1\n"
            "    def bump_twice(self):\n"
            "        self.bump()\n"
            "        self.bump()\n"
            "c = Counter()\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        self.assertIn("Counter", info.self_safe_classes)


class TestStackLocals(unittest.TestCase):
    POINT = (
        "class Point:\n"
        "    def __init__(self, x: int, y: int):\n"
        "        self.x = x\n"
        "        self.y = y\n"
        "    def total(self) -> int:\n"
        "        return self.x + self.y\n"
    )

    def test_locally_used_instance_is_stack_eligible(self):
        src = self.POINT + (
            "def compute() -> int:\n"
            "    p = Point(3, 4)\n"
            "    return p.total()\n"
            "compute()\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        funcs = _funcs_by_name(ir_stmts)
        self.assertEqual(info.locals_for(funcs["compute"]), {"p": "Point"})
        self.assertIn("Point", info.stack_alloc_classes)

    def test_returned_instance_is_not_stack_eligible(self):
        src = self.POINT + (
            "def make():\n"
            "    p = Point(1, 2)\n"
            "    return p\n"
            "make()\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        funcs = _funcs_by_name(ir_stmts)
        self.assertEqual(info.locals_for(funcs["make"]), {})

    def test_assigned_to_global_is_not_stack_eligible(self):
        src = self.POINT + (
            "g = Point(0, 0)\n"
            "def stash():\n"
            "    global g\n"
            "    p = Point(5, 6)\n"
            "    g = p\n"
            "stash()\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        funcs = _funcs_by_name(ir_stmts)
        self.assertEqual(info.locals_for(funcs["stash"]), {})

    def test_passed_as_function_argument_is_not_stack_eligible(self):
        src = self.POINT + (
            "def helper(p) -> int:\n"
            "    return p.total()\n"
            "def make():\n"
            "    p = Point(7, 8)\n"
            "    helper(p)\n"
            "make()\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        funcs = _funcs_by_name(ir_stmts)
        self.assertEqual(info.locals_for(funcs["make"]), {})

    def test_stored_into_another_objects_attribute_is_not_stack_eligible(self):
        # `p` is handed off into `b`'s field, so `p` itself is disqualified
        # (its lifetime is no longer provably confined to build()'s frame
        # -- b could, in general, outlive it). `b` never escapes build()
        # by name, though, so it's a separate, still-eligible candidate:
        # storing a pointer *into* a stack object doesn't require the
        # pointee to also be stack allocated.
        src = (
            self.POINT
            + "class Box:\n"
            "    def __init__(self, first):\n"
            "        self.child = first\n"
            "def build():\n"
            "    seed = Point(0, 0)\n"
            "    b = Box(seed)\n"
            "    p = Point(1, 1)\n"
            "    b.child = p\n"
            "build()\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        funcs = _funcs_by_name(ir_stmts)
        stack_locals = info.locals_for(funcs["build"])
        self.assertNotIn("p", stack_locals)
        self.assertNotIn("seed", stack_locals)  # passed into Box(...)'s constructor args
        self.assertEqual(stack_locals.get("b"), "Box")

    def test_printed_instance_is_conservatively_not_stack_eligible(self):
        src = self.POINT + (
            "def show():\n"
            "    p = Point(2, 2)\n"
            "    print(p.total())\n"
            "    print(p)\n"
            "show()\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        funcs = _funcs_by_name(ir_stmts)
        self.assertEqual(info.locals_for(funcs["show"]), {})

    def test_unsafe_class_never_yields_stack_locals(self):
        src = (
            "class Node:\n"
            "    def __init__(self, v: int):\n"
            "        self.v = v\n"
            "    def identity(self):\n"
            "        return self\n"
            "def make():\n"
            "    n = Node(1)\n"
            "    return n.v\n"
            "make()\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        funcs = _funcs_by_name(ir_stmts)
        self.assertEqual(info.locals_for(funcs["make"]), {})
        self.assertEqual(info.stack_alloc_classes, set())

    def test_reused_across_loop_iterations_is_stack_eligible(self):
        src = self.POINT + (
            "def loop() -> int:\n"
            "    total = 0\n"
            "    i = 0\n"
            "    while i < 3:\n"
            "        p = Point(i, i)\n"
            "        total = total + p.total()\n"
            "        i = i + 1\n"
            "    return total\n"
            "loop()\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        funcs = _funcs_by_name(ir_stmts)
        self.assertEqual(info.locals_for(funcs["loop"]), {"p": "Point"})

    def test_module_level_instance_is_never_stack_eligible(self):
        src = self.POINT + "p = Point(1, 2)\n"
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        # No FUNCTION_DEF at all creates it -- nothing to look up, and
        # stack_alloc_classes must stay empty (module scope is forever).
        self.assertEqual(info.stack_locals, {})
        self.assertEqual(info.stack_alloc_classes, set())

    def test_method_body_can_itself_have_stack_eligible_locals(self):
        # A class method constructing a *different*, self-safe class's
        # instance purely locally should also qualify -- Pass 2 runs over
        # every FUNCTION_DEF, including methods, not just top-level defs.
        src = self.POINT + (
            "class Wrapper:\n"
            "    def __init__(self):\n"
            "        self.v = 0\n"
            "    def compute(self) -> int:\n"
            "        p = Point(9, 9)\n"
            "        return p.total()\n"
            "w = Wrapper()\n"
        )
        ir_stmts = build_ir(src)
        info = escape.analyze(ir_stmts)
        funcs = _funcs_by_name(ir_stmts)
        self.assertEqual(info.locals_for(funcs["Wrapper.compute"]), {"p": "Point"})
