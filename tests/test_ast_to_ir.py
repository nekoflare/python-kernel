"""Unit tests for pyck.ast_to_ir: lowering typed AST into IR nodes."""
import unittest

from pyck import ir as IR
from pyck.ast_to_ir import IRBuildError
from pyck.typechecker import CompileError
from pyck.types import PYFLOAT, PYINT, PYNONE, PYSTRING

from .helpers import build_ir


def _main_body(source_body: str, **build_ir_kwargs):
    """Wrap a function body and return the IR statements of `main`."""
    src = "def main():\n" + "\n".join(
        "    " + line for line in source_body.splitlines()
    ) + "\n"
    stmts = build_ir(src, **build_ir_kwargs)
    self_func = stmts[0]
    assert self_func.kind == IR.IRNodeKind.FUNCTION_DEF
    return self_func.body


class TestBinaryOps(unittest.TestCase):
    def test_add_maps_to_add(self):
        body = _main_body("x = 1 + 2")
        self.assertEqual(body[0].value.op, IR.BinOp.ADD)
        self.assertEqual(body[0].value.type, PYINT)

    def test_true_div_always_float(self):
        body = _main_body("x = 7 / 2")
        self.assertEqual(body[0].value.op, IR.BinOp.TRUEDIV)
        self.assertEqual(body[0].value.type, PYFLOAT)

    def test_floor_div_is_int(self):
        body = _main_body("x = 7 // 2")
        self.assertEqual(body[0].value.op, IR.BinOp.FLOORDIV)
        self.assertEqual(body[0].value.type, PYINT)

    def test_mod_is_int(self):
        body = _main_body("x = 7 % 2")
        self.assertEqual(body[0].value.op, IR.BinOp.MOD)
        self.assertEqual(body[0].value.type, PYINT)

    def test_xor_maps_to_bitxor(self):
        body = _main_body("x = 5 ^ 3")
        self.assertEqual(body[0].value.op, IR.BinOp.BITXOR)
        self.assertEqual(body[0].value.type, PYINT)


class TestUnaryOps(unittest.TestCase):
    def test_neg(self):
        body = _main_body("x = -5")
        self.assertEqual(body[0].value.op, IR.UnaryOp.NEG)

    def test_not(self):
        body = _main_body("x = not True")
        self.assertEqual(body[0].value.op, IR.UnaryOp.NOT)


class TestComparisons(unittest.TestCase):
    def test_simple_compare(self):
        body = _main_body("x = 1 < 2")
        node = body[0].value
        self.assertEqual(node.kind, IR.IRNodeKind.COMPARE)
        self.assertEqual(node.op, IR.CmpOp.LT)

    def test_chained_compare_lowers_to_bitand_of_two_compares(self):
        # `a < b < c` desugars to `(a < b) and (b < c)`, represented as a
        # BINARY_OP(BITAND) combining two COMPARE nodes. This is purely an
        # IR-shape check -- codegen's ability to actually emit this shape is
        # tested (and currently fails) in test_codegen.py.
        body = _main_body("x = 1\ny = 2\nz = 3\nw = x < y < z")
        node = body[-1].value
        self.assertEqual(node.kind, IR.IRNodeKind.BINARY_OP)
        self.assertEqual(node.op, IR.BinOp.BITAND)
        self.assertEqual(node.left.kind, IR.IRNodeKind.COMPARE)
        self.assertEqual(node.left.op, IR.CmpOp.LT)
        self.assertEqual(node.right.kind, IR.IRNodeKind.COMPARE)
        self.assertEqual(node.right.op, IR.CmpOp.LT)


class TestBoolOps(unittest.TestCase):
    def test_and_lowers_to_bitand_chain(self):
        body = _main_body("a = True\nb = False\nc = a and b")
        node = body[-1].value
        self.assertEqual(node.kind, IR.IRNodeKind.BINARY_OP)
        self.assertEqual(node.op, IR.BinOp.BITAND)

    def test_or_lowers_to_bitor_chain(self):
        body = _main_body("a = True\nb = False\nc = a or b")
        node = body[-1].value
        self.assertEqual(node.kind, IR.IRNodeKind.BINARY_OP)
        self.assertEqual(node.op, IR.BinOp.BITOR)

    def test_multi_value_and_chains_left_to_right(self):
        body = _main_body("a = True\nb = True\nc = True\nd = a and b and c")
        node = body[-1].value
        # ((a and b) and c)
        self.assertEqual(node.op, IR.BinOp.BITAND)
        self.assertEqual(node.left.op, IR.BinOp.BITAND)


class TestAugAssign(unittest.TestCase):
    def test_aug_assign_desugars_to_binop(self):
        body = _main_body("x = 1\nx += 2")
        aug = body[1]
        self.assertEqual(aug.kind, IR.IRNodeKind.ASSIGN)
        self.assertEqual(aug.target, "x")
        self.assertEqual(aug.value.kind, IR.IRNodeKind.BINARY_OP)
        self.assertEqual(aug.value.op, IR.BinOp.ADD)
        self.assertEqual(aug.value.left.kind, IR.IRNodeKind.NAME_LOAD)
        self.assertEqual(aug.value.left.name, "x")
        self.assertEqual(aug.value.right.int_value, 2)


class TestForRange(unittest.TestCase):
    def test_single_arg_range_starts_at_zero_no_step(self):
        body = _main_body("for i in range(5):\n    pass")
        node = body[0]
        self.assertEqual(node.kind, IR.IRNodeKind.FOR_RANGE)
        self.assertEqual(node.loop_var, "i")
        self.assertEqual(node.range_start.int_value, 0)
        self.assertEqual(node.range_stop.int_value, 5)
        self.assertIsNone(node.range_step)

    def test_two_arg_range_sets_start_and_stop(self):
        body = _main_body("for i in range(1, 5):\n    pass")
        node = body[0]
        self.assertEqual(node.range_start.int_value, 1)
        self.assertEqual(node.range_stop.int_value, 5)
        self.assertIsNone(node.range_step)

    def test_three_arg_range_sets_step(self):
        body = _main_body("for i in range(0, 10, 2):\n    pass")
        node = body[0]
        self.assertEqual(node.range_start.int_value, 0)
        self.assertEqual(node.range_stop.int_value, 10)
        self.assertEqual(node.range_step.int_value, 2)

    def test_non_range_iterable_rejected(self):
        # List literals aren't a supported expression at all (rejected by
        # the typechecker before ast_to_ir ever sees the for-loop), so this
        # raises CompileError rather than reaching IRBuilder's own
        # "Only 'for x in range(...)' loops are supported" check.
        with self.assertRaises((IRBuildError, CompileError)):
            _main_body("for i in [1, 2, 3]:\n    pass")


class TestHardwareIntrinsics(unittest.TestCase):
    def test_read_call_is_a_typed_function_call_node(self):
        body = _main_body("x = inb(0x60)", dangerous_io_ops=True)
        node = body[0].value
        self.assertEqual(node.kind, IR.IRNodeKind.FUNCTION_CALL)
        self.assertEqual(node.func_name, "inb")
        self.assertEqual(node.type, PYINT)
        self.assertEqual(len(node.args), 1)

    def test_write_call_is_none_typed(self):
        body = _main_body("outb(0x60, 1)", dangerous_io_ops=True)
        node = body[0]
        self.assertEqual(node.kind, IR.IRNodeKind.FUNCTION_CALL)
        self.assertEqual(node.func_name, "outb")
        self.assertEqual(node.type, PYNONE)
        self.assertEqual(len(node.args), 2)

    def test_no_arg_read_cr_call(self):
        body = _main_body("x = read_cr0()", dangerous_io_ops=True)
        node = body[0].value
        self.assertEqual(node.func_name, "read_cr0")
        self.assertEqual(node.type, PYINT)
        self.assertEqual(node.args, [])


class TestChr(unittest.TestCase):
    def test_chr_is_a_string_typed_function_call_node(self):
        body = _main_body("x = chr(65)")
        node = body[0].value
        self.assertEqual(node.kind, IR.IRNodeKind.FUNCTION_CALL)
        self.assertEqual(node.func_name, "chr")
        self.assertEqual(node.type, PYSTRING)
        self.assertEqual(len(node.args), 1)


class TestLen(unittest.TestCase):
    def test_len_is_an_int_typed_function_call_node(self):
        body = _main_body('x = len("hello")')
        node = body[0].value
        self.assertEqual(node.kind, IR.IRNodeKind.FUNCTION_CALL)
        self.assertEqual(node.func_name, "len")
        self.assertEqual(node.type, PYINT)
        self.assertEqual(len(node.args), 1)


class TestStringIndexingAndSlicing(unittest.TestCase):
    def test_getitem_node_shape(self):
        body = _main_body('s = "hello"\nc = s[0]')
        node = body[1].value
        self.assertEqual(node.kind, IR.IRNodeKind.STR_GETITEM)
        self.assertEqual(node.type, PYSTRING)
        self.assertEqual(node.subscript_target.name, "s")
        self.assertEqual(node.subscript_index.int_value, 0)

    def test_slice_node_shape_with_both_bounds(self):
        body = _main_body('s = "hello"\nt = s[1:3]')
        node = body[1].value
        self.assertEqual(node.kind, IR.IRNodeKind.STR_SLICE)
        self.assertEqual(node.type, PYSTRING)
        self.assertEqual(node.slice_start.int_value, 1)
        self.assertEqual(node.slice_stop.int_value, 3)

    def test_slice_node_shape_with_omitted_bounds(self):
        body = _main_body('s = "hello"\nt = s[:]')
        node = body[1].value
        self.assertEqual(node.kind, IR.IRNodeKind.STR_SLICE)
        self.assertIsNone(node.slice_start)
        self.assertIsNone(node.slice_stop)

    def test_slice_node_shape_with_step(self):
        body = _main_body('s = "hello"\nt = s[1:3:2]')
        node = body[1].value
        self.assertEqual(node.kind, IR.IRNodeKind.STR_SLICE)
        self.assertEqual(node.slice_start.int_value, 1)
        self.assertEqual(node.slice_stop.int_value, 3)
        self.assertEqual(node.slice_step.int_value, 2)

    def test_slice_node_shape_with_only_step(self):
        body = _main_body('s = "hello"\nt = s[::-1]')
        node = body[1].value
        self.assertIsNone(node.slice_start)
        self.assertIsNone(node.slice_stop)
        self.assertEqual(node.slice_step.op.value, "-")  # UnaryOp NEG wrapping int_value=1


class TestUnsupportedConstructs(unittest.TestCase):
    def test_tuple_unpacking_rejected(self):
        with self.assertRaises(IRBuildError):
            build_ir("a, b = 1, 2\n")

    def test_ternary_rejected(self):
        with self.assertRaises(IRBuildError):
            _main_body("x = 1 if True else 2")


class TestClasses(unittest.TestCase):
    _SRC = (
        "class Point:\n"
        "    def __init__(self, x: int, y: int):\n"
        "        self.x = x\n"
        "        self.y = y\n"
        "    def total(self):\n"
        "        return self.x + self.y\n"
        "def main():\n"
        "    p = Point(1, 2)\n"
        "    print(p.x)\n"
        "    p.x = 5\n"
        "    print(p.total())\n"
        "main()\n"
    )

    def test_class_def_node_shape(self):
        stmts = build_ir(self._SRC)
        class_def = stmts[0]
        self.assertEqual(class_def.kind, IR.IRNodeKind.CLASS_DEF)
        self.assertEqual(class_def.class_name, "Point")
        self.assertEqual(dict(class_def.class_attributes), {"x": PYINT, "y": PYINT})
        method_names = {m.func_name for m in class_def.class_methods}
        self.assertEqual(method_names, {"__init__", "total"})

    def test_instantiation_node_shape(self):
        stmts = build_ir(self._SRC)
        main_body = stmts[1].body
        new_node = main_body[0].value
        self.assertEqual(new_node.kind, IR.IRNodeKind.CLASS_NEW)
        self.assertEqual(new_node.class_name, "Point")
        self.assertEqual(len(new_node.args), 2)

    def test_attr_get_node_shape(self):
        stmts = build_ir(self._SRC)
        main_body = stmts[1].body
        print_node = main_body[1]
        attr_get = print_node.print_args[0]
        self.assertEqual(attr_get.kind, IR.IRNodeKind.ATTR_GET)
        self.assertEqual(attr_get.class_name, "Point")
        self.assertEqual(attr_get.attr_name, "x")
        self.assertEqual(attr_get.type, PYINT)

    def test_attr_set_node_shape(self):
        stmts = build_ir(self._SRC)
        main_body = stmts[1].body
        attr_set = main_body[2]
        self.assertEqual(attr_set.kind, IR.IRNodeKind.ATTR_SET)
        self.assertEqual(attr_set.class_name, "Point")
        self.assertEqual(attr_set.attr_name, "x")
        self.assertEqual(attr_set.value.int_value, 5)

    def test_method_call_node_shape(self):
        stmts = build_ir(self._SRC)
        main_body = stmts[1].body
        method_call = main_body[3].print_args[0]
        self.assertEqual(method_call.kind, IR.IRNodeKind.METHOD_CALL)
        self.assertEqual(method_call.class_name, "Point")
        self.assertEqual(method_call.func_name, "total")
        self.assertEqual(method_call.type, PYINT)

    def test_init_first_param_is_self_typed_as_class(self):
        from pyck.types import PyTypeKind
        stmts = build_ir(self._SRC)
        class_def = stmts[0]
        init_method = next(m for m in class_def.class_methods if m.func_name == "__init__")
        self_name, self_type = init_method.params[0]
        self.assertEqual(self_name, "self")
        self.assertEqual(self_type.kind, PyTypeKind.CLASS)
        self.assertEqual(self_type.class_name, "Point")


class TestComparisonDunders(unittest.TestCase):
    _SRC = (
        "class Point:\n"
        "    def __init__(self, x: int):\n"
        "        self.x = x\n"
        "    def __eq__(self, other):\n"
        "        return self.x == other.x\n"
        "def main():\n"
        "    a = Point(1)\n"
        "    b = Point(2)\n"
        "    r1 = a == b\n"
        "    r2 = a != b\n"
        "main()\n"
    )

    def test_eq_lowers_to_a_method_call(self):
        stmts = build_ir(self._SRC)
        main_body = stmts[1].body
        eq_node = main_body[2].value
        self.assertEqual(eq_node.kind, IR.IRNodeKind.METHOD_CALL)
        self.assertEqual(eq_node.class_name, "Point")
        self.assertEqual(eq_node.func_name, "__eq__")

    def test_ne_without_explicit_ne_lowers_to_not_eq_call(self):
        stmts = build_ir(self._SRC)
        main_body = stmts[1].body
        ne_node = main_body[3].value
        self.assertEqual(ne_node.kind, IR.IRNodeKind.UNARY_OP)
        self.assertEqual(ne_node.op, IR.UnaryOp.NOT)
        self.assertEqual(ne_node.operand.kind, IR.IRNodeKind.METHOD_CALL)
        self.assertEqual(ne_node.operand.func_name, "__eq__")


if __name__ == "__main__":
    unittest.main()
