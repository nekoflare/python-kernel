"""AST-to-IR builder. Converts a typed Python AST into the IR representation.

Called after type inference, so all types are resolved. Emits explicit
Py_INCREF/Py_DECREF for every reference creation and scope exit.
"""
from __future__ import annotations

import ast
from typing import Optional

from .types import PyType, PyTypeKind, PYINT, PYFLOAT, PYBOOL, PYNONE, PYSTRING, PYUNKNOWN, unify
from . import ir as IR
from .typechecker import FuncSig, ClassSig, HARDWARE_INTRINSICS, LIMINE_INTRINSICS, ISR_INTRINSICS, CMP_DUNDER


# ---------------------------------------------------------------------------
# Exception for unsupported constructs
# ---------------------------------------------------------------------------

class IRBuildError(Exception):
    def __init__(self, filename: str, node: ast.AST, msg: str):
        # Same per-node file tag preference as typechecker.py's _error --
        # see that function's comment for why.
        self.filename = getattr(node, "_pyck_file", filename)
        self.line = getattr(node, "lineno", 0)
        self.col = getattr(node, "col_offset", 0)
        self.message = msg
        super().__init__(f"{self.filename}:{self.line}:{self.col}: error: {msg}")


# ---------------------------------------------------------------------------
# IR Builder
# ---------------------------------------------------------------------------

class IRBuilder:
    def __init__(self, source: str, filename: str,
                 funcs: dict[str, FuncSig],
                 global_vars: dict[str, PyType],
                 dangerous_ops: bool = False,
                 classes: dict[str, ClassSig] | None = None,
                 reachable_funcs: set[str] | None = None,
                 reachable_classes: set[str] | None = None):
        self.filename = filename
        self.source = source
        self.funcs = funcs
        self.global_vars = global_vars
        self.dangerous_ops = dangerous_ops
        self.classes = classes or {}
        # None means "everything is reachable" (unfiltered) -- matches
        # TypeChecker._reachable_func_sigs()'s same default, for callers
        # that construct an IRBuilder without running
        # TypeChecker._compute_reachable first.
        self.reachable_funcs = reachable_funcs
        self.reachable_classes = reachable_classes
        self._temp_counter = 0
        self._current_sig: FuncSig | None = None

    def _fresh_temp(self) -> str:
        self._temp_counter += 1
        return f"_t{self._temp_counter}"

    def build(self, tree: ast.Module) -> list[IR.IRNode]:
        """Build IR for the entire module. Skips a FunctionDef/ClassDef
        TypeChecker._compute_reachable found unreachable (never called,
        directly or transitively, from the module's own top-level
        executable statements) entirely -- its param_types/return_type
        were never inferred (nothing calls it, so there's nothing to
        infer from), so building IR for it would need types that don't
        exist; skipping it here is what actually keeps it out of the
        compiled program, matching the intent of not compiling dead
        code rather than erroring on it.
        """
        stmts: list[IR.IRNode] = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                if self.reachable_funcs is not None and node.name not in self.reachable_funcs:
                    continue
                stmts.append(self._build_func(node))
            elif isinstance(node, ast.ClassDef):
                if self.reachable_classes is not None and node.name not in self.reachable_classes:
                    continue
                stmts.append(self._build_class(node))
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                built = self._build_stmt(node)
                if built is not None:
                    stmts.append(built)
            elif isinstance(node, ast.Expr):
                built = self._build_stmt(node)
                if built is not None:
                    stmts.append(built)
            elif isinstance(node, (ast.Break, ast.Continue, ast.Pass)):
                pass  # handled inside loops
        return stmts

    def _build_func(self, node: ast.FunctionDef) -> IR.IRNode:
        sig = self.funcs[node.name]
        self._current_sig = sig
        body = self._build_block(node.body, sig)
        self._current_sig = None
        return IR.ir_func_def(
            name=node.name,
            params=list(zip(sig.param_names, sig.param_types)),
            body=body,
            return_type=sig.return_type or PYNONE,
            line=node.lineno,
            col=node.col_offset,
            global_names=frozenset(sig.global_names),
        )

    def _build_class(self, node: ast.ClassDef) -> IR.IRNode:
        class_sig = self.classes[node.name]
        method_nodes = []
        for item in node.body:
            if isinstance(item, ast.FunctionDef):
                method_sig = class_sig.methods[item.name]
                self._current_sig = method_sig
                body = self._build_block(item.body, method_sig)
                self._current_sig = None
                method_nodes.append(IR.ir_func_def(
                    name=item.name,
                    params=list(zip(method_sig.param_names, method_sig.param_types)),
                    body=body,
                    return_type=method_sig.return_type or PYNONE,
                    line=item.lineno,
                    col=item.col_offset,
                    global_names=frozenset(method_sig.global_names),
                ))
        attributes = list(class_sig.attributes.items())
        return IR.ir_class_def(node.name, attributes, method_nodes, node.lineno, node.col_offset)

    def _build_block(self, stmts: list[ast.stmt], sig: FuncSig) -> list[IR.IRNode]:
        result: list[IR.IRNode] = []
        for s in stmts:
            built = self._build_stmt(s)
            if built is not None:
                result.append(built)
        return result

    def _build_stmt(self, stmt: ast.stmt) -> IR.IRNode | None:
        line = getattr(stmt, "lineno", 0)
        col = getattr(stmt, "col_offset", 0)

        if isinstance(stmt, ast.Assign):
            if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                target = stmt.targets[0].id
                val = self._build_expr(stmt.value)
                return IR.ir_assign(target, val, line, col)
            if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Attribute):
                attr_target = self._build_expr(stmt.targets[0].value)
                class_name = attr_target.type.class_name
                val = self._build_expr(stmt.value)
                return IR.ir_attr_set(attr_target, class_name, stmt.targets[0].attr, val, line, col)
            # Tuple unpacking — not in v1
            raise IRBuildError(self.filename, stmt, "Tuple unpacking not supported in v1")

        if isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name):
                target = stmt.target.id
                if stmt.value:
                    val = self._build_expr(stmt.value)
                    return IR.ir_assign(target, val, line, col)
                return None
            raise IRBuildError(self.filename, stmt, "Annotated assignment target must be a name")

        if isinstance(stmt, ast.AugAssign):
            if isinstance(stmt.target, ast.Name):
                target = stmt.target.id
                # Desugar: x += e  →  x = x + e
                load = IR.ir_name(target, self._resolve_name_type(target, stmt), line, col)
                val = self._build_expr(stmt.value)
                op = self._aug_to_binop(stmt.op)
                result_type = self._infer_binop_result(load.type, val.type, op)
                binop = IR.ir_binop(op, load, val, result_type, line, col)
                return IR.ir_assign(target, binop, line, col)
            raise IRBuildError(self.filename, stmt, "Augmented assignment target must be a name")

        if isinstance(stmt, ast.Return):
            if stmt.value:
                val = self._build_expr(stmt.value)
                return IR.ir_return(val, line, col)
            return IR.ir_return(None, line, col)

        if isinstance(stmt, ast.If):
            test = self._build_expr(stmt.test)
            body = self._build_block(stmt.body, None)  # sig not needed for IR
            elif_branches = []
            elif_cases = stmt.orelse
            # Convert chained elif
            while elif_cases and len(elif_cases) == 1 and isinstance(elif_cases[0], ast.If):
                elif_node = elif_cases[0]
                elif_test = self._build_expr(elif_node.test)
                elif_body = self._build_block(elif_node.body, None)
                elif_branches.append((elif_test, elif_body))
                elif_cases = elif_node.orelse
            else_body = self._build_block(elif_cases, None) if elif_cases else []
            return IR.ir_if(test, body, elif_branches, else_body, line, col)

        if isinstance(stmt, ast.While):
            test = self._build_expr(stmt.test)
            body = self._build_block(stmt.body, None)
            # Handle break/continue — they're already in the IR as IRNodeKind.BREAK/CONTINUE
            return IR.ir_while(test, body, line, col)

        if isinstance(stmt, ast.For):
            return self._build_for_range(stmt)

        if isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Call):
                return self._build_call_stmt(stmt.value)
            # Skip docstrings (string constant expression statements)
            if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                return None
            raise IRBuildError(self.filename, stmt, "Expression statements other than calls not supported in v1")

        if isinstance(stmt, ast.Break):
            return IR.ir_break(line, col)
        if isinstance(stmt, ast.Continue):
            return IR.ir_continue(line, col)
        if isinstance(stmt, ast.Pass):
            return None
        if isinstance(stmt, ast.Global):
            # Purely a compile-time directive -- TypeChecker._build_func_sig
            # already collected these names into FuncSig.global_names (which
            # _build_func/_build_class thread onto the IR func-def node), and
            # that's the only thing that actually changes: codegen.py skips
            # declaring a fresh local for a name declared global here, so a
            # plain `x = ...` in the emitted C++ falls through to the
            # existing file-scope static instead of shadowing it. Nothing to
            # emit for the statement itself.
            return None

        raise IRBuildError(self.filename, stmt, f"Unsupported statement: {type(stmt).__name__}")

    def _build_for_range(self, stmt: ast.For) -> IR.IRNode:
        line = stmt.lineno
        col = stmt.col_offset

        if not isinstance(stmt.target, ast.Name):
            raise IRBuildError(self.filename, stmt, "For loop variable must be a simple name")

        loop_var = stmt.target.id

        # Expect range(...)
        if not (isinstance(stmt.iter, ast.Call) and
                isinstance(stmt.iter.func, ast.Name) and
                stmt.iter.func.id == "range"):
            raise IRBuildError(self.filename, stmt, "Only 'for x in range(...)' loops are supported in v1")

        args = stmt.iter.args
        if len(args) == 1:
            start = ir_int_lit(0, line, col)
            stop = self._build_expr(args[0])
            step = None
        elif len(args) == 2:
            start = self._build_expr(args[0])
            stop = self._build_expr(args[1])
            step = None
        elif len(args) == 3:
            start = self._build_expr(args[0])
            stop = self._build_expr(args[1])
            step = self._build_expr(args[2])
        else:
            raise IRBuildError(self.filename, stmt, "range() takes 1-3 arguments")

        body = self._build_block(stmt.body, None)
        return IR.ir_for_range(loop_var, start, stop, step, body, line, col)

    def _build_call_stmt(self, call: ast.Call) -> IR.IRNode:
        line = call.lineno
        col = call.col_offset

        if isinstance(call.func, ast.Attribute):
            return self._build_method_call(call, line, col)

        func_name = ""
        if isinstance(call.func, ast.Name):
            func_name = call.func.id

        if func_name in self.classes:
            args = [self._build_expr(a) for a in call.args]
            return IR.ir_class_new(func_name, args, line, col)

        if func_name == "print":
            args = [self._build_expr(a) for a in call.args]
            return IR.ir_print(args, line, col)

        if func_name in ("peek8", "peek16", "peek32", "peek64"):
            width = int(func_name[4:])
            addr = self._build_expr(call.args[0]) if call.args else ir_int_lit(0, line, col)
            return IR.ir_peek(width, addr, line, col)

        if func_name in ("poke8", "poke16", "poke32", "poke64"):
            width = int(func_name[4:])
            addr = self._build_expr(call.args[0]) if call.args else ir_int_lit(0, line, col)
            val = self._build_expr(call.args[1]) if len(call.args) > 1 else ir_int_lit(0, line, col)
            return IR.ir_poke(width, addr, val, line, col)

        if func_name in ("raw_memzero", "raw_memcpy"):
            args = [self._build_expr(a) for a in call.args]
            return IR.ir_func_call(func_name, args, PYNONE, line, col)

        if func_name in ("_ASM", "_ASM1"):
            args = [self._build_expr(a) for a in call.args]
            return IR.ir_func_call(func_name, args, PYNONE, line, col)

        if func_name in HARDWARE_INTRINSICS:
            args = [self._build_expr(a) for a in call.args]
            _, ret_type = HARDWARE_INTRINSICS[func_name]
            return IR.ir_func_call(func_name, args, ret_type, line, col)

        if func_name in LIMINE_INTRINSICS:
            args = [self._build_expr(a) for a in call.args]
            _, ret_type = LIMINE_INTRINSICS[func_name]
            return IR.ir_func_call(func_name, args, ret_type, line, col)

        if func_name in ISR_INTRINSICS:
            args = [self._build_expr(a) for a in call.args]
            _, ret_type = ISR_INTRINSICS[func_name]
            return IR.ir_func_call(func_name, args, ret_type, line, col)

        if func_name == "chr":
            args = [self._build_expr(a) for a in call.args]
            return IR.ir_func_call(func_name, args, PYSTRING, line, col)

        if func_name == "len":
            args = [self._build_expr(a) for a in call.args]
            return IR.ir_func_call(func_name, args, PYINT, line, col)

        if func_name == "hex":
            args = [self._build_expr(a) for a in call.args]
            return IR.ir_func_call(func_name, args, PYSTRING, line, col)

        # User-defined function call
        args = [self._build_expr(a) for a in call.args]
        sig = self.funcs.get(func_name)
        rt = sig.return_type if sig else PYUNKNOWN
        return IR.ir_func_call(func_name, args, rt, line, col)

    def _build_expr(self, node: ast.expr) -> IR.IRNode:
        line = getattr(node, "lineno", 0)
        col = getattr(node, "col_offset", 0)

        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                return IR.ir_bool_lit(v, line, col)
            if isinstance(v, int):
                return IR.ir_int_lit(v, line, col)
            if isinstance(v, float):
                return IR.ir_float_lit(v, line, col)
            if v is None:
                return IR.ir_none_lit(line, col)
            if isinstance(v, str):
                return IR.ir_str_lit(v, line, col)
            raise IRBuildError(self.filename, node, f"Unsupported constant: {type(v).__name__}")

        if isinstance(node, ast.Name):
            t = self._resolve_name_type(node.id, node)
            return IR.ir_name(node.id, t, line, col)

        if isinstance(node, ast.BinOp):
            left = self._build_expr(node.left)
            right = self._build_expr(node.right)
            op = self._ast_binop_to_ir(node.op)
            result_type = self._infer_binop_result(left.type, right.type, op)
            return IR.ir_binop(op, left, right, result_type, line, col)

        if isinstance(node, ast.UnaryOp):
            operand = self._build_expr(node.operand)
            if isinstance(node.op, ast.USub):
                return IR.ir_unaryop(IR.UnaryOp.NEG, operand, operand.type, line, col)
            if isinstance(node.op, ast.Not):
                return IR.ir_unaryop(IR.UnaryOp.NOT, operand, PYBOOL, line, col)
            raise IRBuildError(self.filename, node, f"Unsupported unary op: {type(node.op).__name__}")

        if isinstance(node, ast.Compare):
            left = self._build_expr(node.left)
            results = []
            for op, comp in zip(node.ops, node.comparators):
                right = self._build_expr(comp)
                results.append(self._build_single_compare(left, right, op, line, col))
                left = right
            if len(results) == 1:
                return results[0]
            # Chain: a < b < c  →  (a < b) and (b < c)
            result = results[0]
            for r in results[1:]:
                result = IR.ir_binop(IR.BinOp.BITAND, result, r, PYBOOL, line, col)
            return result

        if isinstance(node, ast.BoolOp):
            op = IR.BinOp.BITAND if isinstance(node.op, ast.And) else IR.BinOp.BITOR
            left = self._build_expr(node.values[0])
            for val in node.values[1:]:
                right = self._build_expr(val)
                left = IR.ir_binop(op, left, right, PYBOOL, line, col)
            return left

        if isinstance(node, ast.Call):
            return self._build_call_expr(node)

        if isinstance(node, ast.IfExp):
            test = self._build_expr(node.test)
            body = self._build_expr(node.body)
            orelse = self._build_expr(node.orelse)
            # Result type is unified
            try:
                result_type = unify(body.type, orelse.type)
            except TypeError:
                result_type = body.type
            # Inline ternary as if/else — but IR doesn't have a ternary node.
            # Lower to: we'll need a temp. Actually, codegen will handle this.
            # For now, store as a special form. We'll use BLOCK with assign + if.
            # Actually, let's just emit it as an IR node that codegen knows about.
            # Simplest: use a BLOCK that assigns to a temp.
            temp = self._fresh_temp()
            # This is a bit ugly but works: we emit a block
            # For simplicity, let's just evaluate and return.
            # Real implementation would use a temp. For v1, keep it simple.
            # Actually, let's just leave it as is — codegen will handle IfExp.
            # We'll add it to IR as a node. But IR doesn't have IfExp.
            # Let me just return the body if test is always true — no, that's wrong.
            # OK, let me add an IF_EXPR to the IR for this purpose.
            # For now, raise an error — ternary expressions are an edge case.
            raise IRBuildError(self.filename, node, "Ternary expressions not supported in v1 (use if/else)")

        if isinstance(node, ast.Subscript):
            target = self._build_expr(node.value)
            if isinstance(node.slice, ast.Slice):
                start = self._build_expr(node.slice.lower) if node.slice.lower is not None else None
                stop = self._build_expr(node.slice.upper) if node.slice.upper is not None else None
                step = self._build_expr(node.slice.step) if node.slice.step is not None else None
                return IR.ir_str_slice(target, start, stop, step, line, col)
            index = self._build_expr(node.slice)
            return IR.ir_str_getitem(target, index, line, col)

        if isinstance(node, ast.Attribute):
            target = self._build_expr(node.value)
            class_name = target.type.class_name
            class_sig = self.classes.get(class_name) if class_name else None
            attr_type = class_sig.attributes.get(node.attr, PYUNKNOWN) if class_sig else PYUNKNOWN
            return IR.ir_attr_get(target, class_name, node.attr, attr_type, line, col)

        raise IRBuildError(self.filename, node, f"Unsupported expression: {type(node).__name__}")

    def _build_call_expr(self, call: ast.Call) -> IR.IRNode:
        line = call.lineno
        col = call.col_offset

        if isinstance(call.func, ast.Attribute):
            return self._build_method_call(call, line, col)

        func_name = ""
        if isinstance(call.func, ast.Name):
            func_name = call.func.id

        if func_name in self.classes:
            args = [self._build_expr(a) for a in call.args]
            return IR.ir_class_new(func_name, args, line, col)

        if func_name in ("peek8", "peek16", "peek32", "peek64"):
            width = int(func_name[4:])
            addr = self._build_expr(call.args[0]) if call.args else ir_int_lit(0, line, col)
            return IR.ir_peek(width, addr, line, col)

        # range() — shouldn't appear outside for loops, but handle it
        if func_name == "range":
            raise IRBuildError(self.filename, call, "range() can only be used in 'for x in range(...)'")

        if func_name in ("_ASM", "_ASM1"):
            args = [self._build_expr(a) for a in call.args]
            return IR.ir_func_call(func_name, args, PYNONE, line, col)

        if func_name in HARDWARE_INTRINSICS:
            args = [self._build_expr(a) for a in call.args]
            _, ret_type = HARDWARE_INTRINSICS[func_name]
            return IR.ir_func_call(func_name, args, ret_type, line, col)

        if func_name in LIMINE_INTRINSICS:
            args = [self._build_expr(a) for a in call.args]
            _, ret_type = LIMINE_INTRINSICS[func_name]
            return IR.ir_func_call(func_name, args, ret_type, line, col)

        if func_name in ISR_INTRINSICS:
            args = [self._build_expr(a) for a in call.args]
            _, ret_type = ISR_INTRINSICS[func_name]
            return IR.ir_func_call(func_name, args, ret_type, line, col)

        if func_name == "chr":
            args = [self._build_expr(a) for a in call.args]
            return IR.ir_func_call(func_name, args, PYSTRING, line, col)

        if func_name == "len":
            args = [self._build_expr(a) for a in call.args]
            return IR.ir_func_call(func_name, args, PYINT, line, col)

        if func_name == "hex":
            args = [self._build_expr(a) for a in call.args]
            return IR.ir_func_call(func_name, args, PYSTRING, line, col)

        # User-defined function call (used as expression)
        args = [self._build_expr(a) for a in call.args]
        sig = self.funcs.get(func_name)
        rt = sig.return_type if sig else PYUNKNOWN
        return IR.ir_func_call(func_name, args, rt, line, col)

    def _build_single_compare(self, left: IR.IRNode, right: IR.IRNode, op: ast.cmpop,
                              line: int, col: int) -> IR.IRNode:
        if left.type.kind == PyTypeKind.CLASS or right.type.kind == PyTypeKind.CLASS:
            return self._build_class_compare(left, right, op, line, col)
        cmp_op = self._ast_cmpop_to_ir(op)
        return IR.ir_compare(cmp_op, left, right, line, col)

    def _build_class_compare(self, left: IR.IRNode, right: IR.IRNode, op: ast.cmpop,
                             line: int, col: int) -> IR.IRNode:
        class_name = left.type.class_name
        class_sig = self.classes[class_name]
        dunder = CMP_DUNDER[type(op)]
        if dunder == "__ne__" and dunder not in class_sig.methods:
            # __ne__ falls back to `not __eq__`, matching real Python --
            # the typechecker already verified __eq__ exists in this case.
            eq_sig = class_sig.methods["__eq__"]
            rt = eq_sig.return_type or PYBOOL
            eq_call = IR.ir_method_call(left, class_name, "__eq__", [right], rt, line, col)
            return IR.ir_unaryop(IR.UnaryOp.NOT, eq_call, PYBOOL, line, col)
        method_sig = class_sig.methods[dunder]
        rt = method_sig.return_type or PYBOOL
        return IR.ir_method_call(left, class_name, dunder, [right], rt, line, col)

    def _build_method_call(self, call: ast.Call, line: int, col: int) -> IR.IRNode:
        assert isinstance(call.func, ast.Attribute)
        target = self._build_expr(call.func.value)
        class_name = target.type.class_name
        method_name = call.func.attr
        class_sig = self.classes.get(class_name) if class_name else None
        method_sig = class_sig.methods.get(method_name) if class_sig else None
        rt = method_sig.return_type if method_sig and method_sig.return_type else PYUNKNOWN
        args = [self._build_expr(a) for a in call.args]
        return IR.ir_method_call(target, class_name, method_name, args, rt, line, col)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_name_type(self, name: str, node: ast.AST) -> PyType:
        # Check current function's local types first
        if self._current_sig and name in self._current_sig.local_types:
            return self._current_sig.local_types[name]
        if name in self.global_vars:
            return self.global_vars[name]
        if name in self.funcs:
            sig = self.funcs[name]
            return sig.return_type or PYNONE
        if name in ("True", "False"):
            return PYBOOL
        if name == "None":
            return PYNONE
        raise IRBuildError(self.filename, node, f"Undefined variable '{name}'")

    def _ast_binop_to_ir(self, op: ast.operator) -> IR.BinOp:
        mapping = {
            ast.Add: IR.BinOp.ADD,
            ast.Sub: IR.BinOp.SUB,
            ast.Mult: IR.BinOp.MUL,
            ast.Div: IR.BinOp.TRUEDIV,
            ast.FloorDiv: IR.BinOp.FLOORDIV,
            ast.Mod: IR.BinOp.MOD,
            ast.Pow: IR.BinOp.POW,
            ast.BitAnd: IR.BinOp.BITAND,
            ast.BitOr: IR.BinOp.BITOR,
            ast.BitXor: IR.BinOp.BITXOR,
            ast.LShift: IR.BinOp.LSHIFT,
            ast.RShift: IR.BinOp.RSHIFT,
        }
        result = mapping.get(type(op))
        if result is None:
            raise IRBuildError(self.filename, op, f"Unsupported binary op: {type(op).__name__}")
        return result

    def _aug_to_binop(self, op: ast.operator) -> IR.BinOp:
        return self._ast_binop_to_ir(op)

    def _ast_cmpop_to_ir(self, op: ast.cmpop) -> IR.CmpOp:
        mapping = {
            ast.Eq: IR.CmpOp.EQ,
            ast.NotEq: IR.CmpOp.NEQ,
            ast.Lt: IR.CmpOp.LT,
            ast.LtE: IR.CmpOp.LTE,
            ast.Gt: IR.CmpOp.GT,
            ast.GtE: IR.CmpOp.GTE,
        }
        result = mapping.get(type(op))
        if result is None:
            raise IRBuildError(self.filename, op, f"Unsupported comparison: {type(op).__name__}")
        return result

    def _infer_binop_result(self, lt: PyType, rt: PyType, op: IR.BinOp) -> PyType:
        if op == IR.BinOp.TRUEDIV:
            return PYFLOAT
        if op in (IR.BinOp.FLOORDIV, IR.BinOp.MOD, IR.BinOp.POW):
            return PYINT
        if op == IR.BinOp.MUL:
            kinds = {lt.kind, rt.kind}
            if PyTypeKind.STR in kinds and kinds <= {PyTypeKind.STR, PyTypeKind.INT, PyTypeKind.BOOL}:
                return PYSTRING
        try:
            return unify(lt, rt)
        except TypeError:
            return lt


def ir_int_lit(value: int, line: int = 0, col: int = 0) -> IR.IRNode:
    return IR.ir_int_lit(value, line, col)
