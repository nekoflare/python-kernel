"""Unit tests for pyck.ir: the typed IR node constructors.

These just check that each helper builds a node with the right kind,
type, and payload fields wired up -- the IR module has no logic beyond
that, but codegen/ast_to_ir depend on these invariants holding.
"""
import unittest

from pyck import ir as IR
from pyck.types import PYBOOL, PYFLOAT, PYINT, PYNONE, PYSTRING


class TestLiterals(unittest.TestCase):
    def test_int_lit(self):
        n = IR.ir_int_lit(42, line=1, col=2)
        self.assertEqual(n.kind, IR.IRNodeKind.INT_LIT)
        self.assertEqual(n.type, PYINT)
        self.assertEqual(n.int_value, 42)
        self.assertEqual((n.line, n.col), (1, 2))

    def test_float_lit(self):
        n = IR.ir_float_lit(3.5)
        self.assertEqual(n.kind, IR.IRNodeKind.FLOAT_LIT)
        self.assertEqual(n.type, PYFLOAT)
        self.assertEqual(n.float_value, 3.5)

    def test_bool_lit(self):
        n = IR.ir_bool_lit(True)
        self.assertEqual(n.kind, IR.IRNodeKind.BOOL_LIT)
        self.assertEqual(n.type, PYBOOL)
        self.assertTrue(n.bool_value)

    def test_str_lit(self):
        n = IR.ir_str_lit("hi")
        self.assertEqual(n.kind, IR.IRNodeKind.STR_LIT)
        self.assertEqual(n.type, PYSTRING)
        self.assertEqual(n.str_value, "hi")

    def test_none_lit(self):
        n = IR.ir_none_lit()
        self.assertEqual(n.kind, IR.IRNodeKind.NONE_LIT)
        self.assertEqual(n.type, PYNONE)


class TestNamesAndAssign(unittest.TestCase):
    def test_name(self):
        n = IR.ir_name("x", PYINT)
        self.assertEqual(n.kind, IR.IRNodeKind.NAME_LOAD)
        self.assertEqual(n.name, "x")
        self.assertEqual(n.type, PYINT)

    def test_assign_takes_type_from_value(self):
        val = IR.ir_int_lit(1)
        n = IR.ir_assign("x", val)
        self.assertEqual(n.kind, IR.IRNodeKind.ASSIGN)
        self.assertEqual(n.target, "x")
        self.assertIs(n.value, val)
        self.assertEqual(n.type, PYINT)


class TestOps(unittest.TestCase):
    def test_binop_carries_explicit_result_type(self):
        left, right = IR.ir_int_lit(1), IR.ir_float_lit(2.0)
        n = IR.ir_binop(IR.BinOp.ADD, left, right, PYFLOAT)
        self.assertEqual(n.kind, IR.IRNodeKind.BINARY_OP)
        self.assertEqual(n.op, IR.BinOp.ADD)
        self.assertIs(n.left, left)
        self.assertIs(n.right, right)
        self.assertEqual(n.type, PYFLOAT)

    def test_unaryop(self):
        operand = IR.ir_int_lit(1)
        n = IR.ir_unaryop(IR.UnaryOp.NEG, operand, PYINT)
        self.assertEqual(n.kind, IR.IRNodeKind.UNARY_OP)
        self.assertEqual(n.op, IR.UnaryOp.NEG)
        self.assertIs(n.operand, operand)

    def test_compare_type_is_always_bool(self):
        left, right = IR.ir_int_lit(1), IR.ir_int_lit(2)
        n = IR.ir_compare(IR.CmpOp.LT, left, right)
        self.assertEqual(n.kind, IR.IRNodeKind.COMPARE)
        self.assertEqual(n.type, PYBOOL)


class TestControlFlow(unittest.TestCase):
    def test_if_stores_branches(self):
        test = IR.ir_bool_lit(True)
        body = [IR.ir_break()]
        elif_branches = [(IR.ir_bool_lit(False), [IR.ir_continue()])]
        else_body = [IR.ir_break()]
        n = IR.ir_if(test, body, elif_branches, else_body)
        self.assertEqual(n.kind, IR.IRNodeKind.IF)
        self.assertIs(n.test, test)
        self.assertEqual(n.body, body)
        self.assertEqual(n.elif_branches, elif_branches)
        self.assertEqual(n.else_body, else_body)
        self.assertEqual(n.type, PYNONE)

    def test_if_defaults_to_empty_branches(self):
        n = IR.ir_if(IR.ir_bool_lit(True), [])
        self.assertEqual(n.elif_branches, [])
        self.assertEqual(n.else_body, [])

    def test_while(self):
        test = IR.ir_bool_lit(True)
        body = [IR.ir_break()]
        n = IR.ir_while(test, body)
        self.assertEqual(n.kind, IR.IRNodeKind.WHILE)
        self.assertIs(n.test, test)
        self.assertEqual(n.body, body)

    def test_for_range_fields(self):
        start, stop, step = IR.ir_int_lit(0), IR.ir_int_lit(10), IR.ir_int_lit(2)
        body = [IR.ir_break()]
        n = IR.ir_for_range("i", start, stop, step, body)
        self.assertEqual(n.kind, IR.IRNodeKind.FOR_RANGE)
        self.assertEqual(n.loop_var, "i")
        self.assertIs(n.range_start, start)
        self.assertIs(n.range_stop, stop)
        self.assertIs(n.range_step, step)
        self.assertEqual(n.body, body)

    def test_break_continue(self):
        self.assertEqual(IR.ir_break().kind, IR.IRNodeKind.BREAK)
        self.assertEqual(IR.ir_continue().kind, IR.IRNodeKind.CONTINUE)


class TestFunctionsAndCalls(unittest.TestCase):
    def test_func_def(self):
        params = [("a", PYINT), ("b", PYFLOAT)]
        body = [IR.ir_return(IR.ir_int_lit(1))]
        n = IR.ir_func_def("f", params, body, PYINT)
        self.assertEqual(n.kind, IR.IRNodeKind.FUNCTION_DEF)
        self.assertEqual(n.func_name, "f")
        self.assertEqual(n.params, params)
        self.assertEqual(n.body, body)
        self.assertEqual(n.type, PYINT)

    def test_func_call(self):
        args = [IR.ir_int_lit(1), IR.ir_int_lit(2)]
        n = IR.ir_func_call("f", args, PYINT)
        self.assertEqual(n.kind, IR.IRNodeKind.FUNCTION_CALL)
        self.assertEqual(n.func_name, "f")
        self.assertEqual(n.args, args)
        self.assertEqual(n.type, PYINT)

    def test_return_with_value_takes_its_type(self):
        val = IR.ir_float_lit(1.0)
        n = IR.ir_return(val)
        self.assertEqual(n.kind, IR.IRNodeKind.RETURN)
        self.assertIs(n.return_value, val)
        self.assertEqual(n.type, PYFLOAT)

    def test_return_without_value_is_none_type(self):
        n = IR.ir_return(None)
        self.assertIsNone(n.return_value)
        self.assertEqual(n.type, PYNONE)


class TestMisc(unittest.TestCase):
    def test_print(self):
        args = [IR.ir_str_lit("hi"), IR.ir_int_lit(1)]
        n = IR.ir_print(args)
        self.assertEqual(n.kind, IR.IRNodeKind.PRINT_CALL)
        self.assertEqual(n.print_args, args)
        self.assertEqual(n.type, PYNONE)

    def test_block(self):
        stmts = [IR.ir_break(), IR.ir_continue()]
        n = IR.ir_block(stmts)
        self.assertEqual(n.kind, IR.IRNodeKind.BLOCK)
        self.assertEqual(n.stmts, stmts)

    def test_peek(self):
        addr = IR.ir_int_lit(0x1000)
        n = IR.ir_peek(32, addr)
        self.assertEqual(n.kind, IR.IRNodeKind.PEEK)
        self.assertEqual(n.peek_width, 32)
        self.assertIs(n.peek_addr, addr)
        self.assertEqual(n.type, PYINT)

    def test_poke(self):
        addr, val = IR.ir_int_lit(0x1000), IR.ir_int_lit(7)
        n = IR.ir_poke(8, addr, val)
        self.assertEqual(n.kind, IR.IRNodeKind.POKE)
        self.assertEqual(n.peek_width, 8)
        self.assertIs(n.poke_addr, addr)
        self.assertIs(n.poke_value, val)
        self.assertEqual(n.type, PYNONE)

    def test_incref_decref(self):
        inc = IR.ir_incref("x")
        dec = IR.ir_decref("x")
        self.assertEqual(inc.kind, IR.IRNodeKind.INCREF)
        self.assertEqual(inc.ref_target, "x")
        self.assertEqual(dec.kind, IR.IRNodeKind.DECREF)
        self.assertEqual(dec.ref_target, "x")


if __name__ == "__main__":
    unittest.main()
