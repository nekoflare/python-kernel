"""Typed intermediate representation for PyCk.

The IR sits between Python's AST and C++ codegen. Every node carries a resolved
PyType. Refcounting operations (Py_INCREF/Py_DECREF) are explicit in the IR.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from .types import PyType, PYINT, PYFLOAT, PYBOOL, PYNONE, PYSTRING


# ---------------------------------------------------------------------------
# IR Node base
# ---------------------------------------------------------------------------

class IRNodeKind(Enum):
    # Literals
    INT_LIT = auto()
    FLOAT_LIT = auto()
    BOOL_LIT = auto()
    STR_LIT = auto()    # string literal (print args only in v1)
    NONE_LIT = auto()

    # Variables / names
    NAME_LOAD = auto()       # load a variable
    NAME_STORE = auto()      # store to a variable

    # Arithmetic / logic
    BINARY_OP = auto()       # +, -, *, /, //, %
    UNARY_OP = auto()        # -, not
    COMPARE = auto()         # ==, !=, <, <=, >, >=

    # Control flow
    IF = auto()
    WHILE = auto()
    FOR_RANGE = auto()
    BREAK = auto()
    CONTINUE = auto()

    # Functions
    FUNCTION_DEF = auto()
    FUNCTION_CALL = auto()
    RETURN = auto()

    # Print
    PRINT_CALL = auto()

    # Dangerous memory ops
    PEEK = auto()
    POKE = auto()

    # String indexing / slicing
    STR_GETITEM = auto()   # s[i]
    STR_SLICE = auto()     # s[a:b]

    # Refcounting (explicit)
    INCREF = auto()
    DECREF = auto()

    # Block / sequence
    BLOCK = auto()

    # Assignment
    ASSIGN = auto()

    # Classes (basic: no inheritance, no class vars, no dunders but __init__)
    CLASS_DEF = auto()     # class Foo: ...
    CLASS_NEW = auto()     # Foo(args) -- instantiation
    ATTR_GET = auto()      # obj.attr
    ATTR_SET = auto()      # obj.attr = value
    METHOD_CALL = auto()   # obj.method(args)


class BinOp(Enum):
    ADD = "+"
    SUB = "-"
    MUL = "*"
    TRUEDIV = "/"
    FLOORDIV = "//"
    MOD = "%"
    POW = "**"
    BITAND = "&"
    BITOR = "|"
    BITXOR = "^"
    LSHIFT = "<<"
    RSHIFT = ">>"


class UnaryOp(Enum):
    NEG = "-"
    NOT = "not"


class CmpOp(Enum):
    EQ = "=="
    NEQ = "!="
    LT = "<"
    LTE = "<="
    GT = ">"
    GTE = ">="


@dataclass
class IRNode:
    kind: IRNodeKind
    type: PyType = field(default_factory=lambda: PYINT)
    line: int = 0
    col: int = 0

    # --- Literals ---
    int_value: Optional[int] = None
    float_value: Optional[float] = None
    bool_value: Optional[bool] = None
    str_value: Optional[str] = None

    # --- Names ---
    name: Optional[str] = None

    # --- Binary / unary ---
    op: Optional[BinOp | UnaryOp | CmpOp] = None
    left: Optional[IRNode] = None
    right: Optional[IRNode] = None
    operand: Optional[IRNode] = None  # for unary

    # --- Control flow ---
    test: Optional[IRNode] = None
    body: list[IRNode] = field(default_factory=list)
    elif_branches: list[tuple[IRNode, list[IRNode]]] = field(default_factory=list)
    else_body: list[IRNode] = field(default_factory=list)

    # --- For range ---
    loop_var: Optional[str] = None
    range_start: Optional[IRNode] = None
    range_stop: Optional[IRNode] = None
    range_step: Optional[IRNode] = None

    # --- Functions ---
    func_name: Optional[str] = None
    params: list[tuple[str, PyType]] = field(default_factory=list)
    args: list[IRNode] = field(default_factory=list)
    return_value: Optional[IRNode] = None
    global_names: frozenset[str] = field(default_factory=frozenset)  # FUNCTION_DEF: names declared `global`

    # --- Print ---
    print_args: list[IRNode] = field(default_factory=list)

    # --- Dangerous ops ---
    peek_width: Optional[int] = None  # 8, 16, 32, 64
    peek_addr: Optional[IRNode] = None
    poke_addr: Optional[IRNode] = None
    poke_value: Optional[IRNode] = None

    # --- String indexing / slicing ---
    subscript_target: Optional[IRNode] = None      # the str expression being indexed/sliced
    subscript_index: Optional[IRNode] = None       # for STR_GETITEM
    slice_start: Optional[IRNode] = None           # for STR_SLICE; None = not given
    slice_stop: Optional[IRNode] = None            # for STR_SLICE; None = not given
    slice_step: Optional[IRNode] = None            # for STR_SLICE; None = not given (defaults to 1)

    # --- Refcount ---
    ref_target: Optional[str] = None  # variable name or temp id
    ref_count: int = 0  # unused for now, always 1

    # --- Block ---
    stmts: list[IRNode] = field(default_factory=list)

    # --- Assignment ---
    target: Optional[str] = None
    value: Optional[IRNode] = None

    # --- Classes ---
    class_name: Optional[str] = None                       # CLASS_DEF/CLASS_NEW/ATTR_GET/ATTR_SET/METHOD_CALL
    class_attributes: list[tuple[str, PyType]] = field(default_factory=list)  # CLASS_DEF, in discovery order
    class_methods: list[IRNode] = field(default_factory=list)  # CLASS_DEF, each a FUNCTION_DEF node
    attr_target: Optional[IRNode] = None                   # ATTR_GET/ATTR_SET/METHOD_CALL: the object expr
    attr_name: Optional[str] = None                         # ATTR_GET/ATTR_SET: attribute name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ir_int_lit(value: int, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.INT_LIT, type=PYINT, int_value=value, line=line, col=col)


def ir_float_lit(value: float, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.FLOAT_LIT, type=PYFLOAT, float_value=value, line=line, col=col)


def ir_bool_lit(value: bool, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.BOOL_LIT, type=PYBOOL, bool_value=value, line=line, col=col)


def ir_str_lit(value: str, line: int = 0, col: int = 0) -> IRNode:
    from .types import PYSTRING
    return IRNode(kind=IRNodeKind.STR_LIT, type=PYSTRING, str_value=value, line=line, col=col)


def ir_none_lit(line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.NONE_LIT, type=PYNONE, line=line, col=col)


def ir_name(name: str, typ: PyType, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.NAME_LOAD, type=typ, name=name, line=line, col=col)


def ir_assign(target: str, value: IRNode, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.ASSIGN, target=target, value=value, type=value.type, line=line, col=col)


def ir_binop(op: BinOp, left: IRNode, right: IRNode, result_type: PyType, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.BINARY_OP, op=op, left=left, right=right, type=result_type, line=line, col=col)


def ir_unaryop(op: UnaryOp, operand: IRNode, result_type: PyType, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.UNARY_OP, op=op, operand=operand, type=result_type, line=line, col=col)


def ir_compare(op: CmpOp, left: IRNode, right: IRNode, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.COMPARE, op=op, left=left, right=right, type=PYBOOL, line=line, col=col)


def ir_if(test: IRNode, body: list[IRNode],
          elif_branches: list[tuple[IRNode, list[IRNode]]] | None = None,
          else_body: list[IRNode] | None = None,
          line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.IF, test=test, body=body,
                  elif_branches=elif_branches or [],
                  else_body=else_body or [],
                  type=PYNONE, line=line, col=col)


def ir_while(test: IRNode, body: list[IRNode], line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.WHILE, test=test, body=body, type=PYNONE, line=line, col=col)


def ir_for_range(loop_var: str, start: IRNode, stop: IRNode, step: IRNode | None,
                 body: list[IRNode], line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.FOR_RANGE, loop_var=loop_var,
                  range_start=start, range_stop=stop, range_step=step,
                  body=body, type=PYNONE, line=line, col=col)


def ir_func_def(name: str, params: list[tuple[str, PyType]], body: list[IRNode],
                return_type: PyType, line: int = 0, col: int = 0,
                global_names: frozenset[str] = frozenset()) -> IRNode:
    return IRNode(kind=IRNodeKind.FUNCTION_DEF, func_name=name, params=params,
                  body=body, type=return_type, line=line, col=col,
                  global_names=global_names)


def ir_func_call(name: str, args: list[IRNode], result_type: PyType,
                 line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.FUNCTION_CALL, func_name=name, args=args,
                  type=result_type, line=line, col=col)


def ir_return(value: IRNode | None, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.RETURN, return_value=value,
                  type=value.type if value else PYNONE, line=line, col=col)


def ir_print(args: list[IRNode], line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.PRINT_CALL, print_args=args, type=PYNONE, line=line, col=col)


def ir_block(stmts: list[IRNode], line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.BLOCK, stmts=stmts, type=PYNONE, line=line, col=col)


def ir_peek(width: int, addr: IRNode, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.PEEK, peek_width=width, peek_addr=addr, type=PYINT, line=line, col=col)


def ir_poke(width: int, addr: IRNode, value: IRNode, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.POKE, peek_width=width, poke_addr=addr, poke_value=value, type=PYNONE, line=line, col=col)


def ir_str_getitem(target: IRNode, index: IRNode, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.STR_GETITEM, subscript_target=target, subscript_index=index,
                  type=PYSTRING, line=line, col=col)


def ir_str_slice(target: IRNode, start: Optional[IRNode], stop: Optional[IRNode],
                 step: Optional[IRNode] = None, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.STR_SLICE, subscript_target=target, slice_start=start, slice_stop=stop,
                  slice_step=step, type=PYSTRING, line=line, col=col)


def ir_incref(var: str, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.INCREF, ref_target=var, type=PYNONE, line=line, col=col)


def ir_decref(var: str, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.DECREF, ref_target=var, type=PYNONE, line=line, col=col)


def ir_break(line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.BREAK, type=PYNONE, line=line, col=col)


def ir_continue(line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.CONTINUE, type=PYNONE, line=line, col=col)


def ir_class_def(name: str, attributes: list[tuple[str, PyType]], methods: list[IRNode],
                 line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.CLASS_DEF, class_name=name, class_attributes=attributes,
                  class_methods=methods, type=PYNONE, line=line, col=col)


def ir_class_new(name: str, args: list[IRNode], line: int = 0, col: int = 0) -> IRNode:
    from .types import PyType as _PyType, PyTypeKind as _PyTypeKind
    return IRNode(kind=IRNodeKind.CLASS_NEW, class_name=name, args=args,
                  type=_PyType(_PyTypeKind.CLASS, name), line=line, col=col)


def ir_attr_get(target: IRNode, class_name: str, attr: str, result_type: PyType,
               line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.ATTR_GET, attr_target=target, class_name=class_name,
                  attr_name=attr, type=result_type, line=line, col=col)


def ir_attr_set(target: IRNode, class_name: str, attr: str, value: IRNode,
               line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.ATTR_SET, attr_target=target, class_name=class_name,
                  attr_name=attr, value=value, type=PYNONE, line=line, col=col)


def ir_method_call(target: IRNode, class_name: str, method_name: str, args: list[IRNode],
                   result_type: PyType, line: int = 0, col: int = 0) -> IRNode:
    return IRNode(kind=IRNodeKind.METHOD_CALL, attr_target=target, class_name=class_name,
                  func_name=method_name, args=args, type=result_type, line=line, col=col)
