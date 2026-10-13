"""C++ code generation from PyCk IR.

Two modes:
  - hosted: PyObject*/CPython Stable ABI calls
  - freestanding: freestanding PyObject struct, vendored bignum, no libc
"""
from __future__ import annotations

import textwrap
from typing import Optional

from .types import PyTypeKind, PyType, PYSTRING
from . import ir as IR
from . import escape
from .typechecker import EXTERNALLY_CALLED_FUNCS


# ---------------------------------------------------------------------------
# Codegen error
# ---------------------------------------------------------------------------

class CodegenError(Exception):
    def __init__(self, msg: str, line: int = 0, col: int = 0):
        self.message = msg
        self.line = line
        self.col = col
        super().__init__(f"codegen:{line}:{col}: error: {msg}")


# ---------------------------------------------------------------------------
# C++ Code Generator
# ---------------------------------------------------------------------------

class CppCodegen:
    def __init__(self, mode: str = "hosted",
                 max_recursion_depth: int = 10000,
                 dangerous_ops: bool = False):
        self.mode = mode
        self.max_recursion_depth = max_recursion_depth
        self.dangerous_ops = dangerous_ops
        self._indent = 0
        self._lines: list[str] = []
        self._global_decls: list[str] = []
        self._forward_decls: list[str] = []
        self._class_decls: list[str] = []  # freestanding struct/type-object definitions
        self._classes: dict[str, IR.IRNode] = {}  # class_name -> CLASS_DEF node
        self._recursion_depth_name = "_pyck_recursion_depth"
        self._declared_vars: set[str] = set()  # track declared variables to avoid shadowing
        self._ret_tmp_counter = 0  # unique per-return temp names, see _emit_return
        self._escape = escape.EscapeInfo()  # populated per-generate(); see analyze()
        self._current_stack_locals: dict[str, str] = {}  # var -> class_name, current function only

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, ir_stmts: list[IR.IRNode]) -> str:
        """Generate complete C++ source from IR."""
        self._lines = []
        self._global_decls = []
        self._forward_decls = []
        self._class_decls = []
        self._classes = {}

        class_defs = [s for s in ir_stmts if s.kind == IR.IRNodeKind.CLASS_DEF]
        for cd in class_defs:
            self._classes[cd.class_name] = cd

        # Escape analysis: which locally-constructed class instances can
        # live on the C++ stack instead of the heap. Only meaningful in
        # freestanding mode -- hosted-mode instances are real CPython
        # PyDict objects. See escape.py for the two passes.
        self._escape = escape.analyze(ir_stmts) if self.mode == "freestanding" else escape.EscapeInfo()
        self._current_stack_locals = {}

        # Module-level Python variables become real C++ global variables,
        # declared before any function so functions can reference them.
        # The corresponding top-level ASSIGN statement (handled below by
        # _emit_module_main) then just assigns into it rather than
        # declaring a shadowing local. Collected recursively (not just
        # direct top-level ASSIGNs) for the same reason function bodies
        # need it -- see _collect_assigned_names.
        top_level_stmts = [s for s in ir_stmts
                           if s.kind not in (IR.IRNodeKind.FUNCTION_DEF, IR.IRNodeKind.CLASS_DEF)]
        global_names = self._collect_assigned_names(top_level_stmts)
        for name in sorted(global_names):
            self._global_decls.append(f"static PyObject* {name} = nullptr;")

        # Classes: struct/type-object definitions (freestanding only), then
        # forward decls for every method + the per-class constructor
        # (which bundles allocation + __init__ into one call expression).
        for cd in class_defs:
            self._emit_class_struct(cd)
        for cd in class_defs:
            for method in cd.class_methods:
                self._emit_func_forward(method, name_override=self._mangle_method_name(cd.class_name, method.func_name))
            self._forward_decls.append(self._constructor_forward_decl(cd))
            if cd.class_name in self._escape.stack_alloc_classes:
                self._forward_decls.append(self._stack_constructor_forward_decl(cd))

        # Generate forward declarations for all functions
        for stmt in ir_stmts:
            if stmt.kind == IR.IRNodeKind.FUNCTION_DEF:
                self._emit_func_forward(stmt)

        # Generate class method bodies + constructors
        for cd in class_defs:
            for method in cd.class_methods:
                self._emit_func_def(method, name_override=self._mangle_method_name(cd.class_name, method.func_name))
            self._emit_constructor_def(cd)
            if cd.class_name in self._escape.stack_alloc_classes:
                self._emit_stack_constructor_def(cd)

        # Generate function bodies
        for stmt in ir_stmts:
            if stmt.kind == IR.IRNodeKind.FUNCTION_DEF:
                self._emit_func_def(stmt)

        # Generate module-level code (main). Top-level assignments are
        # never stack-alloc candidates (module scope outlives every
        # function frame) -- see escape.analyze, which only considers
        # FUNCTION_DEF bodies.
        self._declared_vars = set(global_names)
        self._current_stack_locals = {}
        self._emit_module_main(ir_stmts)

        # Assemble
        header = self._emit_header()
        footer = self._emit_footer()
        globals_src = "\n".join(self._global_decls)
        class_src = "\n".join(self._class_decls)
        forward = "\n".join(self._forward_decls)
        body = "\n".join(self._lines)
        return f"{header}\n{globals_src}\n{class_src}\n{forward}\n\n{body}\n{footer}\n"

    # ------------------------------------------------------------------
    # Header / footer
    # ------------------------------------------------------------------

    def _emit_header(self) -> str:
        if self.mode == "hosted":
            depth = self.max_recursion_depth
            return f"""\
#include <Python.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>

static void _pyck_fputPyObject(PyObject* obj, FILE* fp) {{
    PyObject* strobj = PyObject_Str(obj);
    if (strobj) {{
        PyObject* bytes = PyUnicode_AsEncodedString(strobj, "utf-8", "strict");
        if (bytes) {{
            const char* utf8 = PyBytes_AsString(bytes);
            if (utf8) fputs(utf8, fp);
            Py_DECREF(bytes);
        }}
        Py_DECREF(strobj);
    }}
}}

extern "C" void py_print(PyObject* args, PyObject* sep, PyObject* end) {{
    Py_ssize_t nargs = PyTuple_Size(args);
    for (Py_ssize_t i = 0; i < nargs; i++) {{
        if (i > 0) _pyck_fputPyObject(sep, stdout);
        PyObject* item = PyTuple_GetItem(args, i);
        _pyck_fputPyObject(item, stdout);
    }}
    _pyck_fputPyObject(end, stdout);
}}

static inline int _pyck_int_is_negative(PyObject* obj) {{
    return PyLong_AsLong(obj) < 0;
}}

static PyObject* _pyck_str_getitem(PyObject* s, int64_t index) {{
    Py_ssize_t len = PyUnicode_GetLength(s);
    if (index < 0) index += len;
    return PyUnicode_Substring(s, index, index + 1);
}}

static PyObject* _pyck_str_slice(PyObject* s, int64_t start, int64_t stop, int64_t step,
                                  int has_start, int has_stop, int has_step) {{
    Py_ssize_t len = PyUnicode_GetLength(s);
    int64_t st = has_step ? step : 1;
    if (st == 0) {{
        fprintf(stderr, "ValueError: slice step cannot be zero\\n");
        abort();
    }}
    // Omitted bounds default to extreme sentinels (INT64_MAX/-INT64_MAX),
    // not directly to len-1/-1: running them through the exact same
    // clamp below as an explicit index would, they collapse to the right
    // default. Using -1/len-1 directly instead breaks when the *other*
    // bound is a real negative index, since -1 alone is indistinguishable
    // from "user explicitly wrote -1".
    int64_t a = has_start ? start : (st < 0 ? INT64_MAX : (int64_t)0);
    int64_t b = has_stop ? stop : (st < 0 ? -INT64_MAX : INT64_MAX);
    if (a < 0) {{ a += len; if (a < 0) a = (st < 0) ? -1 : 0; }}
    else if (a >= len) {{ a = (st < 0) ? (int64_t)len - 1 : (int64_t)len; }}
    if (b < 0) {{ b += len; if (b < 0) b = (st < 0) ? -1 : 0; }}
    else if (b >= len) {{ b = (st < 0) ? (int64_t)len - 1 : (int64_t)len; }}
    if (st == 1) {{
        return PyUnicode_Substring(s, a, b);
    }}
    Py_ssize_t result_len = 0;
    if (st < 0) {{ if (b < a) result_len = (a - b - 1) / (-st) + 1; }}
    else {{ if (a < b) result_len = (b - a - 1) / st + 1; }}
    PyObject* result = PyUnicode_FromString("");
    int64_t idx = a;
    for (Py_ssize_t i = 0; i < result_len; i++) {{
        PyObject* ch = PyUnicode_Substring(s, idx, idx + 1);
        PyObject* new_result = PyUnicode_Concat(result, ch);
        Py_DECREF(result);
        Py_DECREF(ch);
        result = new_result;
        idx += st;
    }}
    return result;
}}

static PyObject* _pyck_int_to_hex(PyObject* obj) {{
    // A plain `PyLong_AsLongLong` (signed) used to make hex() of any
    // value in [2^63, 2^64) -- e.g. a canonical higher-half address like
    // 0xffff800000000000 -- raise OverflowError, since such a value has
    // no representation as a *signed* 64-bit integer at all despite
    // fitting perfectly in 64 bits unsigned. Check the sign via a real
    // comparison first (works for any magnitude), then convert the
    // (now always non-negative) value with the unsigned counterpart.
    PyObject* zero = PyLong_FromLong(0);
    int neg = PyObject_RichCompareBool(obj, zero, Py_LT);
    Py_DECREF(zero);
    unsigned long long v;
    if (neg) {{
        PyObject* mag = PyNumber_Negative(obj);
        v = PyLong_AsUnsignedLongLong(mag);
        Py_DECREF(mag);
    }} else {{
        v = PyLong_AsUnsignedLongLong(obj);
    }}

    char digits[32];
    int n = 0;
    if (v == 0) {{
        digits[n++] = '0';
    }} else {{
        while (v > 0) {{
            int d = (int)(v & 0xF);
            digits[n++] = (d < 10) ? ('0' + d) : ('a' + d - 10);
            v >>= 4;
        }}
    }}
    char out[40];
    int j = 0;
    if (neg) out[j++] = '-';
    out[j++] = '0';
    out[j++] = 'x';
    for (int k = n - 1; k >= 0; k--) out[j++] = digits[k];
    out[j] = '\\0';
    return PyUnicode_FromString(out);
}}

static int64_t _pyck_recursion_depth = 0;
static const int64_t _pyck_max_recursion_depth = {depth};

#define PYCK_CHECK_RECURSION() do {{ \\
    if (++_pyck_recursion_depth > _pyck_max_recursion_depth) {{ \\
        fprintf(stderr, "RuntimeError: maximum recursion depth exceeded\\n"); \\
        abort(); \\
    }} \\
}} while(0)

#define PYCK_LEAVE_RECURSION() do {{ \\
    --_pyck_recursion_depth; \\
}} while(0)
"""
        else:
            # Freestanding header is emitted by the freestanding runtime
            return """\
#include "pyck_runtime.h"

static int64_t _pyck_recursion_depth = 0;
extern int64_t _pyck_max_recursion_depth;

#define PYCK_CHECK_RECURSION() do { \\
    if (++_pyck_recursion_depth > _pyck_max_recursion_depth) { \\
        py_fatal_error("maximum recursion depth exceeded"); \\
    } \\
} while(0)

#define PYCK_LEAVE_RECURSION() do { \\
    --_pyck_recursion_depth; \\
} while(0)
"""

    def _emit_footer(self) -> str:
        if self.mode == "hosted":
            return """\
int main(int argc, char* argv[]) {
    Py_Initialize();
    pyck_main();
    Py_Finalize();
    return 0;
}
"""
        else:
            return ""

    # ------------------------------------------------------------------
    # Forward declarations
    # ------------------------------------------------------------------

    # A top-level `def on_interrupt(vector: int)` is the one PyCk function
    # hand-written C++ in a different translation unit needs to call
    # directly (see arch/x86_64_shared/idt_stubs.cpp's ISR trampolines) --
    # every other function is `static` (internal linkage) since nothing
    # outside the single generated .cpp file ever calls it by name.
    # Shared with typechecker.py's EXTERNALLY_CALLED_FUNCS (same set,
    # imported from there) so dead-code elimination doesn't mistake this
    # for unreachable just because no ast.Call in the source targets it.
    _EXTERN_C_FUNCS = EXTERNALLY_CALLED_FUNCS

    def _emit_func_forward(self, node: IR.IRNode, name_override: str | None = None):
        ret = self._type_to_cpp(node.type)
        name = name_override or self._mangle_func_name(node.func_name)
        params = ", ".join(f"PyObject* {p[0]}" for p in node.params)
        if name_override is None and node.func_name in self._EXTERN_C_FUNCS:
            self._forward_decls.append(f'extern "C" {ret} {name}({params});')
            return
        self._forward_decls.append(f"static {ret} {name}({params});")

    # ------------------------------------------------------------------
    # Function definition
    # ------------------------------------------------------------------

    def _collect_assigned_names(self, stmts: list[IR.IRNode]) -> set[str]:
        """Recursively collect every variable name assigned anywhere in
        `stmts` -- ASSIGN targets and FOR_RANGE loop variables, including
        inside nested if/while/for bodies. Used to pre-declare every local
        at the very top of its enclosing function/module: C++ scopes a
        `PyObject* x = ...` declaration to whichever nested `{ }` block
        first assigns x, but Python considers x visible for the rest of
        the enclosing function regardless of which branch assigned it
        (e.g. `if c: x = 1 else: x = 2` then using x afterward) -- so
        every name needs declaring once, up front, before any branching.
        """
        names: set[str] = set()
        for stmt in stmts:
            if stmt.kind == IR.IRNodeKind.ASSIGN:
                names.add(stmt.target)
            elif stmt.kind == IR.IRNodeKind.FOR_RANGE:
                names.add(stmt.loop_var)
                names |= self._collect_assigned_names(stmt.body)
            elif stmt.kind == IR.IRNodeKind.IF:
                names |= self._collect_assigned_names(stmt.body)
                for _, elif_body in stmt.elif_branches:
                    names |= self._collect_assigned_names(elif_body)
                names |= self._collect_assigned_names(stmt.else_body)
            elif stmt.kind == IR.IRNodeKind.WHILE:
                names |= self._collect_assigned_names(stmt.body)
            elif stmt.kind == IR.IRNodeKind.BLOCK:
                names |= self._collect_assigned_names(stmt.stmts)
        return names

    def _emit_func_def(self, node: IR.IRNode, name_override: str | None = None):
        ret = self._type_to_cpp(node.type)
        name = name_override or self._mangle_func_name(node.func_name)
        params = ", ".join(f"PyObject* {p[0]}" for p in node.params)

        self._declared_vars = set()
        # Add parameter names as already declared
        for pname, _ in node.params:
            self._declared_vars.add(pname)
        self._current_stack_locals = self._escape.locals_for(node)

        linkage = "static"
        if name_override is None and node.func_name in self._EXTERN_C_FUNCS:
            linkage = 'extern "C"'
        self._line(f"{linkage} {ret} {name}({params}) {{")
        self._indent += 1

        # Recursion guard
        self._line("PYCK_CHECK_RECURSION();")

        # Pre-declare every local assigned anywhere in the body -- see
        # _collect_assigned_names for why this can't be left to whichever
        # nested block happens to assign each name first. A name this
        # function declared `global` is excluded here: it already has a
        # `static PyObject*` declaration at file scope (from the
        # module-level assignment that made it a global in the first
        # place), so a fresh local declaration would just shadow it.
        # Marking it "already declared" without emitting anything makes
        # a later assignment inside this function emit a plain
        # `x = value;`, which falls through to that same file-scope
        # variable under ordinary C++ scoping -- nothing else about
        # assignment/read emission needs to know or care that it's a
        # global rather than a local.
        local_names = sorted(self._collect_assigned_names(node.body)
                              - self._declared_vars - node.global_names)
        for lname in local_names:
            if lname in self._current_stack_locals:
                # Raw storage for a stack-allocated instance (see
                # escape.py): `lname` itself stays a plain PyObject*,
                # pointed at this struct's address once construction
                # runs, exactly like the heap path points it at a
                # py_alloc'd block.
                struct_name = self._instance_struct_name(self._current_stack_locals[lname])
                self._line(f"{struct_name} {self._stack_slot_name(lname)};")
            self._line(f"PyObject* {lname} = nullptr;")
            self._declared_vars.add(lname)
        self._declared_vars |= node.global_names

        # Emit body
        for stmt in node.body:
            self._emit_stmt(stmt)

        self._line("PYCK_LEAVE_RECURSION();")
        self._indent -= 1
        self._line("}")
        self._line("")

    # ------------------------------------------------------------------
    # Classes (basic: no inheritance, no class vars, no dunders but
    # __init__). Instances are represented differently per mode:
    #   - freestanding: a per-class C++ struct (PyObject header + one
    #     PyObject* field per attribute) with a per-class PyTypeObject
    #     singleton, mirroring the existing _pyck_type_int/float/str
    #     pattern exactly.
    #   - hosted: a real CPython dict (PyDict_New()), attributes stored/
    #     read via PyDict_SetItemString/PyDict_GetItemString. Simpler than
    #     building a real PyType_FromSpec type and still fully valid under
    #     the limited API, since Py_INCREF/Py_DECREF just work on it.
    # Either way, a per-class "constructor" function bundles allocation +
    # __init__ into a single call expression (e.g. `_pyck_new_Point(x, y)`),
    # since C++ has no statement-expression equivalent to lean on here.
    # Method calls are always direct, monomorphic calls to a mangled name
    # (`_pyck_ClassName_method`) -- no vtables, since there's no
    # inheritance/polymorphism in scope.
    # ------------------------------------------------------------------

    def _instance_struct_name(self, class_name: str) -> str:
        return f"_pyck_instance_{class_name}"

    def _type_object_name(self, class_name: str) -> str:
        return f"_pyck_type_{class_name}"

    def _mangle_method_name(self, class_name: str, method_name: str) -> str:
        return f"_pyck_{class_name}_{method_name}"

    def _constructor_name(self, class_name: str) -> str:
        return f"_pyck_new_{class_name}"

    def _init_params(self, cd: IR.IRNode) -> list[tuple[str, PyType]]:
        """The constructor's parameter list: __init__'s params minus 'self'."""
        for method in cd.class_methods:
            if method.func_name == "__init__":
                return method.params[1:]
        return []

    def _emit_class_struct(self, cd: IR.IRNode):
        if self.mode != "freestanding":
            return
        struct_name = self._instance_struct_name(cd.class_name)
        type_name = self._type_object_name(cd.class_name)
        dealloc_name = f"_pyck_dealloc_{cd.class_name}"
        fields = "".join(f"    PyObject* {aname};\n" for aname, _ in cd.class_attributes)
        decrefs = "".join(
            f"    Py_DECREF((({struct_name}*)self)->{aname});\n" for aname, _ in cd.class_attributes
        )
        self._class_decls.append(
            f"struct {struct_name} {{\n"
            f"    PyObject ob_base;\n"
            f"{fields}"
            f"}};\n\n"
            f"static void {dealloc_name}(PyObject* self) {{\n"
            f"{decrefs}"
            f"    py_free(self);\n"
            f"}}\n\n"
            f"static PyTypeObject {type_name} = {{ {{ 1, 0 }}, \"{cd.class_name}\", {dealloc_name} }};\n"
        )
        if cd.class_name in self._escape.stack_alloc_classes:
            # A stack-allocated instance needs its own type object whose
            # tp_dealloc decrefs fields exactly like the heap one but never
            # calls py_free(self) -- Py_DECREF (pyck_runtime.h) dispatches
            # purely through ob_type->tp_dealloc, so this is the only hook
            # available to make that generic enough to be stack-safe.
            self._class_decls.append(
                f"static void {dealloc_name}_stack(PyObject* self) {{\n"
                f"{decrefs}"
                f"}}\n\n"
                f"static PyTypeObject {type_name}_stack = "
                f"{{ {{ 1, 0 }}, \"{cd.class_name}\", {dealloc_name}_stack }};\n"
            )

    def _constructor_forward_decl(self, cd: IR.IRNode) -> str:
        params = ", ".join(f"PyObject* {p[0]}" for p in self._init_params(cd))
        return f"static PyObject* {self._constructor_name(cd.class_name)}({params});"

    def _stack_constructor_name(self, class_name: str) -> str:
        return f"{self._constructor_name(class_name)}_stack"

    def _stack_slot_name(self, var: str) -> str:
        return f"_pyck_stack_{var}"

    def _stack_constructor_forward_decl(self, cd: IR.IRNode) -> str:
        struct_name = self._instance_struct_name(cd.class_name)
        params = ", ".join([f"{struct_name}* _storage"]
                            + [f"PyObject* {p[0]}" for p in self._init_params(cd)])
        return f"static PyObject* {self._stack_constructor_name(cd.class_name)}({params});"

    def _emit_constructor_def(self, cd: IR.IRNode):
        init_params = self._init_params(cd)
        params = ", ".join(f"PyObject* {p[0]}" for p in init_params)
        has_init = any(m.func_name == "__init__" for m in cd.class_methods)
        self._line(f"static PyObject* {self._constructor_name(cd.class_name)}({params}) {{")
        self._indent += 1
        if self.mode == "freestanding":
            struct_name = self._instance_struct_name(cd.class_name)
            type_name = self._type_object_name(cd.class_name)
            self._line(f"PyObject* self = (PyObject*)py_alloc(sizeof({struct_name}));")
            self._line("self->ob_refcnt = 1;")
            self._line(f"self->ob_type = &{type_name};")
        else:
            self._line("PyObject* self = PyDict_New();")
        if has_init:
            init_name = self._mangle_method_name(cd.class_name, "__init__")
            arg_names = ", ".join(["self"] + [p[0] for p in init_params])
            self._line(f"{init_name}({arg_names});")
        self._line("return self;")
        self._indent -= 1
        self._line("}")
        self._line("")

    def _emit_stack_constructor_def(self, cd: IR.IRNode):
        """Like _emit_constructor_def, but initializes caller-provided
        storage in place instead of py_alloc'ing -- used at call sites
        escape.py proved don't need the instance to outlive the current
        C++ stack frame. Freestanding only (stack_alloc_classes is always
        empty in hosted mode, so generate() never emits this there)."""
        struct_name = self._instance_struct_name(cd.class_name)
        type_name = self._type_object_name(cd.class_name)
        init_params = self._init_params(cd)
        params = ", ".join([f"{struct_name}* _storage"]
                            + [f"PyObject* {p[0]}" for p in init_params])
        has_init = any(m.func_name == "__init__" for m in cd.class_methods)
        self._line(f"static PyObject* {self._stack_constructor_name(cd.class_name)}({params}) {{")
        self._indent += 1
        self._line("PyObject* self = (PyObject*)_storage;")
        self._line("self->ob_refcnt = 1;")
        self._line(f"self->ob_type = &{type_name}_stack;")
        if has_init:
            init_name = self._mangle_method_name(cd.class_name, "__init__")
            arg_names = ", ".join(["self"] + [p[0] for p in init_params])
            self._line(f"{init_name}({arg_names});")
        self._line("return self;")
        self._indent -= 1
        self._line("}")
        self._line("")

    def _emit_attr_get(self, node: IR.IRNode) -> str:
        target = self._emit_expr(node.attr_target)
        if self.mode == "freestanding":
            struct_name = self._instance_struct_name(node.class_name)
            return f"(({struct_name}*){target})->{node.attr_name}"
        return f'PyDict_GetItemString({target}, "{node.attr_name}")'

    def _emit_attr_set(self, node: IR.IRNode):
        target = self._emit_expr(node.attr_target)
        value = self._emit_expr(node.value)
        if self.mode == "freestanding":
            struct_name = self._instance_struct_name(node.class_name)
            self._line("{")
            self._indent += 1
            self._line(f"PyObject* _attr_target = {target};")
            self._line(f"PyObject* _attr_value = {value};")
            self._line(f"(({struct_name}*)_attr_target)->{node.attr_name} = _attr_value;")
            self._line("Py_INCREF(_attr_value);")
            self._indent -= 1
            self._line("}")
        else:
            self._line(f'PyDict_SetItemString({target}, "{node.attr_name}", {value});')

    def _emit_class_new(self, node: IR.IRNode) -> str:
        args = ", ".join(self._emit_expr(a) for a in node.args)
        return f"{self._constructor_name(node.class_name)}({args})"

    def _emit_class_new_stack(self, node: IR.IRNode, target: str) -> str:
        """Stack-allocated counterpart of _emit_class_new: only called by
        _emit_assign, and only when escape.py proved `target` doesn't
        outlive this function's frame -- see EscapeInfo.locals_for."""
        args = ", ".join([f"&{self._stack_slot_name(target)}"]
                          + [self._emit_expr(a) for a in node.args])
        return f"{self._stack_constructor_name(node.class_name)}({args})"

    def _emit_method_call(self, node: IR.IRNode) -> str:
        target = self._emit_expr(node.attr_target)
        name = self._mangle_method_name(node.class_name, node.func_name)
        args = ", ".join([target] + [self._emit_expr(a) for a in node.args])
        return f"{name}({args})"

    # ------------------------------------------------------------------
    # Module-level main
    # ------------------------------------------------------------------

    def _emit_module_main(self, ir_stmts: list[IR.IRNode]):
        if self.mode == "freestanding":
            self._line("extern \"C\" void pyck_main() {")
        else:
            self._line("void pyck_main() {")
        self._indent += 1
        for stmt in ir_stmts:
            if stmt.kind not in (IR.IRNodeKind.FUNCTION_DEF, IR.IRNodeKind.CLASS_DEF):
                self._emit_stmt(stmt)
        self._indent -= 1
        self._line("}")

    # ------------------------------------------------------------------
    # Statement emission
    # ------------------------------------------------------------------

    def _emit_stmt(self, node: IR.IRNode):
        line = node.line
        col = node.col

        if node.kind == IR.IRNodeKind.ASSIGN:
            self._emit_assign(node)
        elif node.kind == IR.IRNodeKind.RETURN:
            self._emit_return(node)
        elif node.kind == IR.IRNodeKind.IF:
            self._emit_if(node)
        elif node.kind == IR.IRNodeKind.WHILE:
            self._emit_while(node)
        elif node.kind == IR.IRNodeKind.FOR_RANGE:
            self._emit_for_range(node)
        elif node.kind == IR.IRNodeKind.BREAK:
            self._line("break;")
        elif node.kind == IR.IRNodeKind.CONTINUE:
            self._line("continue;")
        elif node.kind == IR.IRNodeKind.PRINT_CALL:
            self._emit_print(node)
        elif node.kind == IR.IRNodeKind.FUNCTION_CALL:
            self._emit_func_call_stmt(node)
        elif node.kind == IR.IRNodeKind.PEEK:
            self._emit_peek_stmt(node)
        elif node.kind == IR.IRNodeKind.POKE:
            self._emit_poke(node)
        elif node.kind == IR.IRNodeKind.BLOCK:
            for s in node.stmts:
                self._emit_stmt(s)
        elif node.kind == IR.IRNodeKind.INCREF:
            self._line(f"Py_INCREF({node.ref_target});")
        elif node.kind == IR.IRNodeKind.DECREF:
            self._line(f"Py_DECREF({node.ref_target});")
        elif node.kind == IR.IRNodeKind.ATTR_SET:
            self._emit_attr_set(node)
        elif node.kind in (IR.IRNodeKind.METHOD_CALL, IR.IRNodeKind.CLASS_NEW):
            val = self._emit_expr(node)
            if node.type.kind != PyTypeKind.NONE:
                self._line("{")
                self._indent += 1
                self._line(f"PyObject* _discard = {val};")
                self._line("Py_DECREF(_discard);")
                self._indent -= 1
                self._line("}")
            else:
                self._line(f"{val};")
        else:
            raise CodegenError(f"Unhandled IR node kind: {node.kind}", line, col)

    def _emit_assign(self, node: IR.IRNode):
        target = node.target
        if (node.value.kind == IR.IRNodeKind.CLASS_NEW
                and self._current_stack_locals.get(target) == node.value.class_name):
            value = self._emit_class_new_stack(node.value, target)
        else:
            value = self._emit_expr(node.value)
        cpp_type = self._type_to_cpp(node.value.type)
        if target in self._declared_vars:
            # Variable already declared — just assign
            self._line(f"{target} = {value};")
        else:
            self._line(f"{cpp_type} {target} = {value};")
            self._declared_vars.add(target)
        # INCREF the new value (it's a PyObject*)
        if cpp_type == "PyObject*":
            self._line(f"Py_INCREF({target});")

    def _emit_return(self, node: IR.IRNode):
        # PYCK_LEAVE_RECURSION() only ever got emitted once, at the tail
        # of _emit_func_def -- unreachable code for any function whose
        # actual execution path exits through an earlier `return`
        # instead of falling off the end (an *extremely* common shape:
        # any if/guard-clause early return, or even just a function
        # whose final statement happens to be its own return). Every
        # such call permanently leaked one increment of
        # _pyck_recursion_depth with no matching decrement, unrelated to
        # any real call-stack depth -- given enough calls (not
        # necessarily recursive ones: 50 flat, non-recursive calls to a
        # function with an early return is enough to trip a depth-40
        # limit), that alone eventually hits
        # "maximum recursion depth exceeded".
        #
        # The value has to be fully evaluated *before* leaving (its own
        # nested calls manage their own depth independently and must
        # run while this function still counts as "entered"), so it's
        # computed into a temp first rather than embedded directly in
        # `return <expr>;`, which would evaluate it *after* -- there's
        # no way to sequence "evaluate, then leave, then return" with a
        # single C++ return statement otherwise.
        if node.return_value:
            val = self._emit_expr(node.return_value)
            self._ret_tmp_counter += 1
            tmp = f"_pyck_ret_tmp{self._ret_tmp_counter}"
            self._line(f"PyObject* {tmp} = {val};")
            self._line("PYCK_LEAVE_RECURSION();")
            self._line(f"return {tmp};")
        else:
            self._line("PYCK_LEAVE_RECURSION();")
            self._line("return;")

    def _emit_if(self, node: IR.IRNode):
        test = self._emit_test(node.test)
        self._line(f"if ({test}) {{")
        self._indent += 1
        for s in node.body:
            self._emit_stmt(s)
        self._indent -= 1
        for elif_test, elif_body in node.elif_branches:
            et = self._emit_test(elif_test)
            self._line(f"}} else if ({et}) {{")
            self._indent += 1
            for s in elif_body:
                self._emit_stmt(s)
            self._indent -= 1
        if node.else_body:
            self._line("} else {")
            self._indent += 1
            for s in node.else_body:
                self._emit_stmt(s)
            self._indent -= 1
        self._line("}")

    def _emit_while(self, node: IR.IRNode):
        test = self._emit_test(node.test)
        self._line(f"while ({test}) {{")
        self._indent += 1
        for s in node.body:
            self._emit_stmt(s)
        self._indent -= 1
        self._line("}")

    def _emit_for_range(self, node: IR.IRNode):
        var = node.loop_var
        start = self._emit_expr(node.range_start)
        stop = self._emit_expr(node.range_stop)
        # Declare loop variable (unless already declared earlier in scope)
        if var in self._declared_vars:
            self._line(f"{var} = {start};")
        else:
            self._line(f"PyObject* {var} = {start};")
            self._declared_vars.add(var)
        self._line(f"Py_INCREF({var});")

        # Emit step
        if node.range_step is not None:
            step_val = self._emit_expr(node.range_step)
            # Determine direction
            self._line("{")
            self._indent += 1
            self._line(f"PyObject* _step = {step_val};")
            self._line("Py_INCREF(_step);")
            # Comparison helper
            if self.mode == "hosted":
                self._line(f"while (_pyck_int_is_negative(_step) ? PyLong_AsLong({var}) > PyLong_AsLong({stop}) : PyLong_AsLong({var}) < PyLong_AsLong({stop})) {{")
            else:
                self._line(f"while (_pyck_int_is_negative(_step) ? _pyck_long_as_long({var}) > _pyck_long_as_long({stop}) : _pyck_long_as_long({var}) < _pyck_long_as_long({stop})) {{")
            self._indent += 1
            for s in node.body:
                self._emit_stmt(s)
            # var = var + step
            if self.mode == "hosted":
                self._line(f"PyObject* _new_var = PyNumber_Add({var}, _step);")
            else:
                self._line(f"PyObject* _new_var = _pyck_bignum_add({var}, _step);")
            self._line(f"Py_DECREF({var});")
            self._line(f"{var} = _new_var;")
            self._line(f"Py_INCREF({var});")
            self._indent -= 1
            self._line("}")
            self._line("Py_DECREF(_step);")
            self._indent -= 1
            self._line("}")
        else:
            # Simple range without step
            if self.mode == "hosted":
                self._line(f"while (PyLong_AsLong({var}) < PyLong_AsLong({stop})) {{")
            else:
                self._line(f"while (_pyck_long_as_long({var}) < _pyck_long_as_long({stop})) {{")
            self._indent += 1
            for s in node.body:
                self._emit_stmt(s)
            # var = var + 1
            if self.mode == "hosted":
                self._line(f"PyObject* _one = PyLong_FromLong(1);")
                self._line(f"PyObject* _new_var = PyNumber_Add({var}, _one);")
            else:
                self._line(f"PyObject* _one = _pyck_long_from_long(1);")
                self._line(f"PyObject* _new_var = _pyck_bignum_add({var}, _one);")
            self._line(f"Py_DECREF(_one);")
            self._line(f"Py_DECREF({var});")
            self._line(f"{var} = _new_var;")
            self._line(f"Py_INCREF({var});")
            self._indent -= 1
            self._line("}")

        self._line(f"Py_DECREF({var});")

    def _resolve_print_arg(self, arg: IR.IRNode) -> IR.IRNode:
        """If `arg` is a class instance whose class defines __str__ (or,
        failing that, __repr__ -- same fallback order as real Python's
        str()), rewrite it into a call to that method. The result is a
        plain PYSTRING-typed expression, so it flows through the exact
        same printing path as any other string value (chr() results,
        boxed literals, ...) with no further special-casing needed.
        """
        if arg.type.kind != PyTypeKind.CLASS:
            return arg
        cd = self._classes.get(arg.type.class_name)
        if cd is None:
            return arg
        for dunder in ("__str__", "__repr__"):
            if any(m.func_name == dunder for m in cd.class_methods):
                return IR.ir_method_call(arg, arg.type.class_name, dunder, [],
                                          PYSTRING, arg.line, arg.col)
        return arg

    def _emit_print(self, node: IR.IRNode):
        print_args = [self._resolve_print_arg(a) for a in node.print_args]
        all_literals = all(a.kind == IR.IRNodeKind.STR_LIT for a in print_args)

        self._line("{")
        self._indent += 1

        if self.mode == "hosted":
            # Every arg (literal or value) is printed in order with a
            # single space between consecutive args, matching print()'s
            # default sep=' ' regardless of which args happen to be
            # string literals vs boxed values.
            next_sep = False
            for arg in print_args:
                if next_sep:
                    self._line('fputs(" ", stdout);')
                if arg.kind == IR.IRNodeKind.STR_LIT:
                    cstr = self._emit_str_lit(arg)
                    self._line(f'fputs({cstr}, stdout);')
                else:
                    val = self._emit_expr(arg)
                    self._line(f"_pyck_fputPyObject({val}, stdout);")
                next_sep = True
            self._line('fputs("\\n", stdout);')
        elif all_literals:
            # Fast path: a print() made up entirely of string literals
            # needs no PyObject boxing at all.
            for i, arg in enumerate(print_args):
                if i > 0:
                    self._line('_pyck_print_cstr(" ");')
                cstr = self._emit_str_lit(arg)
                self._line(f'_pyck_print_cstr({cstr});')
            self._line('_pyck_print_cstr("\\n");')
        else:
            # Mixed (or all-value) print: box every argument -- including
            # any literals -- into one tuple in original source order, so
            # py_print's own separator/newline handling produces correct
            # output regardless of how literals and values are
            # interleaved (a literal boxed here just to be printed once
            # is never explicitly freed, same as every other short-lived
            # PyObject* this compiler hands to _pyck_tuple_set).
            n = len(print_args)
            self._line(f"PyObject* _args = _pyck_tuple_new({n});")
            for j, arg in enumerate(print_args):
                if arg.kind == IR.IRNodeKind.STR_LIT:
                    val = self._emit_str_lit_boxed(arg)
                else:
                    val = self._emit_expr(arg)
                self._line(f"_pyck_tuple_set(_args, {j}, {val});")
            self._line("py_print(_args, NULL, NULL);")
            self._line("_pyck_tuple_decref(_args);")

        self._indent -= 1
        self._line("}")

    def _emit_func_call_stmt(self, node: IR.IRNode):
        # Function call as statement (ignore return value)
        val = self._emit_func_call_expr(node)
        if node.type.kind != PyTypeKind.NONE:
            self._line("{")
            self._indent += 1
            self._line(f"PyObject* _discard = {val};")
            self._line("Py_DECREF(_discard);")
            self._indent -= 1
            self._line("}")
        else:
            self._line(f"{val};")

    def _emit_peek_stmt(self, node: IR.IRNode):
        # peek as expression, but used as statement — evaluate and discard
        val = self._emit_peek_expr(node)
        self._line(f"(void){val};")

    def _emit_poke(self, node: IR.IRNode):
        addr = self._unbox_int(self._emit_expr(node.poke_addr))
        val = self._unbox_int(self._emit_expr(node.poke_value))
        width = node.peek_width
        self._line(f"*reinterpret_cast<volatile uint{width}_t*>((uintptr_t)({addr})) = {val};")

    # ------------------------------------------------------------------
    # Expression emission
    # ------------------------------------------------------------------

    def _emit_expr(self, node: IR.IRNode) -> str:
        if node.kind == IR.IRNodeKind.INT_LIT:
            return self._emit_int_lit(node)
        if node.kind == IR.IRNodeKind.FLOAT_LIT:
            return self._emit_float_lit(node)
        if node.kind == IR.IRNodeKind.BOOL_LIT:
            return self._emit_bool_lit(node)
        if node.kind == IR.IRNodeKind.STR_LIT:
            return self._emit_str_lit_boxed(node)
        if node.kind == IR.IRNodeKind.NONE_LIT:
            return self._emit_none_lit(node)
        if node.kind == IR.IRNodeKind.NAME_LOAD:
            return node.name
        if node.kind == IR.IRNodeKind.BINARY_OP:
            return self._emit_binop(node)
        if node.kind == IR.IRNodeKind.UNARY_OP:
            return self._emit_unaryop(node)
        if node.kind == IR.IRNodeKind.COMPARE:
            return self._emit_compare(node)
        if node.kind == IR.IRNodeKind.FUNCTION_CALL:
            return self._emit_func_call_expr(node)
        if node.kind == IR.IRNodeKind.PEEK:
            return self._emit_peek_expr(node)
        if node.kind == IR.IRNodeKind.STR_GETITEM:
            return self._emit_str_getitem(node)
        if node.kind == IR.IRNodeKind.STR_SLICE:
            return self._emit_str_slice(node)
        if node.kind == IR.IRNodeKind.CLASS_NEW:
            return self._emit_class_new(node)
        if node.kind == IR.IRNodeKind.ATTR_GET:
            return self._emit_attr_get(node)
        if node.kind == IR.IRNodeKind.METHOD_CALL:
            return self._emit_method_call(node)
        raise CodegenError(f"Unhandled IR expr kind: {node.kind}", node.line, node.col)

    def _emit_int_lit(self, node: IR.IRNode) -> str:
        if self.mode == "hosted":
            return f"PyLong_FromLong({node.int_value})"
        else:
            return f"_pyck_long_from_long({node.int_value})"

    def _emit_float_lit(self, node: IR.IRNode) -> str:
        if self.mode == "hosted":
            return f"PyFloat_FromDouble({node.float_value})"
        else:
            return f"_pyck_float_from_double({node.float_value})"

    def _emit_bool_lit(self, node: IR.IRNode) -> str:
        val = "Py_True" if node.bool_value else "Py_False"
        if self.mode == "freestanding":
            val = "_pyck_true" if node.bool_value else "_pyck_false"
        return val

    def _emit_none_lit(self, node: IR.IRNode) -> str:
        if self.mode == "hosted":
            return "Py_None"
        return "_pyck_none"

    def _emit_str_lit(self, node: IR.IRNode) -> str:
        # Lowers to a plain C string literal -- used directly (not boxed)
        # by _emit_print's fast path, which prints a literal argument via
        # fputs/_pyck_print_cstr without ever constructing a PyObject* for
        # it. Every other context needs a real value; see
        # _emit_str_lit_boxed for that.
        escaped = node.str_value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
        return f'"{escaped}"'

    def _emit_str_lit_boxed(self, node: IR.IRNode) -> str:
        # A string literal used as a real value (assigned, concatenated,
        # passed as an argument, ...) rather than as a direct print()
        # argument: boxed into an actual runtime string object, the same
        # kind chr() produces, so it can be used anywhere one can.
        cstr = self._emit_str_lit(node)
        if self.mode == "hosted":
            return f"PyUnicode_FromString({cstr})"
        length = len(node.str_value.encode("utf-8"))
        return f"_pyck_str_from_cstr({cstr}, {length})"

    def _emit_binop(self, node: IR.IRNode) -> str:
        left = self._emit_expr(node.left)
        right = self._emit_expr(node.right)
        op = node.op

        # True division always returns float
        if op == IR.BinOp.TRUEDIV:
            if self.mode == "hosted":
                return f"PyNumber_TrueDivide({left}, {right})"
            return f"_pyck_bignum_truediv({left}, {right})"

        if op == IR.BinOp.FLOORDIV:
            if self.mode == "hosted":
                return f"PyNumber_FloorDivide({left}, {right})"
            return f"_pyck_bignum_floordiv({left}, {right})"

        if op == IR.BinOp.MOD:
            if self.mode == "hosted":
                return f"PyNumber_Remainder({left}, {right})"
            return f"_pyck_bignum_mod({left}, {right})"

        if op == IR.BinOp.POW:
            if self.mode == "hosted":
                return f"PyNumber_Power({left}, {right}, Py_None)"
            return f"_pyck_bignum_pow({left}, {right})"

        # `and`/`or` (BoolOp) and chained comparisons both lower to
        # BINARY_OP(BITAND/BITOR) over PYBOOL operands -- combine their
        # truthiness and box the result, rather than falling through to the
        # int-bitwise-and/or paths below (which assume real bignum operands).
        if node.type.kind == PyTypeKind.BOOL and op in (IR.BinOp.BITAND, IR.BinOp.BITOR):
            combiner = "&&" if op == IR.BinOp.BITAND else "||"
            cond = f"({self._truthiness_call(left)} {combiner} {self._truthiness_call(right)})"
            true_val, false_val = self._bool_singletons()
            return f"({cond} ? {true_val} : {false_val})"

        # String operations: "+" concatenates two strings; "*" repeats a
        # string N times (either operand order, like real Python). Operands
        # can be chr() results, string literals (boxed via
        # _emit_str_lit_boxed), or anything built from those.
        if node.type.kind == PyTypeKind.STR:
            if op == IR.BinOp.ADD:
                if self.mode == "hosted":
                    return f"PyUnicode_Concat({left}, {right})"
                return f"_pyck_str_concat({left}, {right})"
            if op == IR.BinOp.MUL:
                if node.left.type.kind == PyTypeKind.STR:
                    str_expr, count_expr = left, right
                else:
                    str_expr, count_expr = right, left
                count = self._unbox_int(count_expr)
                if self.mode == "hosted":
                    return f"PySequence_Repeat({str_expr}, {count})"
                return f"_pyck_str_repeat({str_expr}, {count})"
            raise CodegenError(f"Unsupported operation on strings: {op}", node.line, node.col)

        # Determine result type for the operation
        if node.type.kind == PyTypeKind.FLOAT:
            if op == IR.BinOp.ADD:
                if self.mode == "hosted":
                    return f"PyNumber_Add({left}, {right})"
                return f"_pyck_float_add({left}, {right})"
            if op == IR.BinOp.SUB:
                if self.mode == "hosted":
                    return f"PyNumber_Subtract({left}, {right})"
                return f"_pyck_float_sub({left}, {right})"
            if op == IR.BinOp.MUL:
                if self.mode == "hosted":
                    return f"PyNumber_Multiply({left}, {right})"
                return f"_pyck_float_mul({left}, {right})"

        # Int operations
        if node.type.kind == PyTypeKind.INT:
            if op == IR.BinOp.ADD:
                if self.mode == "hosted":
                    return f"PyNumber_Add({left}, {right})"
                return f"_pyck_bignum_add({left}, {right})"
            if op == IR.BinOp.SUB:
                if self.mode == "hosted":
                    return f"PyNumber_Subtract({left}, {right})"
                return f"_pyck_bignum_sub({left}, {right})"
            if op == IR.BinOp.MUL:
                if self.mode == "hosted":
                    return f"PyNumber_Multiply({left}, {right})"
                return f"_pyck_bignum_mul({left}, {right})"
            if op == IR.BinOp.BITAND:
                if self.mode == "hosted":
                    return f"PyNumber_And({left}, {right})"
                return f"_pyck_bignum_and({left}, {right})"
            if op == IR.BinOp.BITOR:
                if self.mode == "hosted":
                    return f"PyNumber_Or({left}, {right})"
                return f"_pyck_bignum_or({left}, {right})"
            if op == IR.BinOp.BITXOR:
                if self.mode == "hosted":
                    return f"PyNumber_Xor({left}, {right})"
                return f"_pyck_bignum_xor({left}, {right})"
            if op == IR.BinOp.LSHIFT:
                if self.mode == "hosted":
                    return f"PyNumber_Lshift({left}, {right})"
                return f"_pyck_bignum_lshift({left}, {right})"
            if op == IR.BinOp.RSHIFT:
                if self.mode == "hosted":
                    return f"PyNumber_Rshift({left}, {right})"
                return f"_pyck_bignum_rshift({left}, {right})"

        # Generic fallback
        op_map = {
            IR.BinOp.ADD: ("PyNumber_Add", "_pyck_bignum_add"),
            IR.BinOp.SUB: ("PyNumber_Subtract", "_pyck_bignum_sub"),
            IR.BinOp.MUL: ("PyNumber_Multiply", "_pyck_bignum_mul"),
        }
        if op in op_map:
            hosted, free = op_map[op]
            return f"{hosted}({left}, {right})" if self.mode == "hosted" else f"{free}({left}, {right})"

        raise CodegenError(f"Unhandled binary op: {op}", node.line, node.col)

    def _emit_unaryop(self, node: IR.IRNode) -> str:
        operand = self._emit_expr(node.operand)
        if node.op == IR.UnaryOp.NEG:
            if self.mode == "hosted":
                return f"PyNumber_Negative({operand})"
            return f"_pyck_bignum_neg({operand})"
        if node.op == IR.UnaryOp.NOT:
            if self.mode == "hosted":
                return f"PyObject_IsTrue({operand}) ? Py_False : Py_True"
            return f"_pyck_is_true({operand}) ? _pyck_false : _pyck_true"
        raise CodegenError(f"Unhandled unary op: {node.op}", node.line, node.col)

    def _emit_compare(self, node: IR.IRNode) -> str:
        # COMPARE's IR type is always PYBOOL (a real, usable value -- it can
        # be assigned, printed, returned, or combined with and/or/chained
        # comparisons), so box the raw C++ condition as a PyObject* bool
        # singleton rather than returning a native bool.
        cond = self._emit_compare_condition(node)
        true_val, false_val = self._bool_singletons()
        return f"({cond} ? {true_val} : {false_val})"

    def _emit_compare_condition(self, node: IR.IRNode) -> str:
        """Emit the raw (native C++ bool) comparison expression for `node`."""
        left = self._emit_expr(node.left)
        right = self._emit_expr(node.right)
        op = node.op

        # For string comparisons (lexicographic, byte-wise)
        if (node.left.type.kind == PyTypeKind.STR or
                node.right.type.kind == PyTypeKind.STR):
            if self.mode == "hosted":
                opid_map = {
                    IR.CmpOp.LT: "Py_LT", IR.CmpOp.LTE: "Py_LE",
                    IR.CmpOp.GT: "Py_GT", IR.CmpOp.GTE: "Py_GE",
                    IR.CmpOp.EQ: "Py_EQ", IR.CmpOp.NEQ: "Py_NE",
                }
                return f"PyObject_RichCompareBool({left}, {right}, {opid_map[op]})"
            c_op = {
                IR.CmpOp.LT: "<", IR.CmpOp.LTE: "<=", IR.CmpOp.GT: ">",
                IR.CmpOp.GTE: ">=", IR.CmpOp.EQ: "==", IR.CmpOp.NEQ: "!=",
            }[op]
            return f"(_pyck_str_cmp({left}, {right}) {c_op} 0)"

        # For float comparisons
        if (node.left.type.kind == PyTypeKind.FLOAT or
                node.right.type.kind == PyTypeKind.FLOAT):
            cmp_map = {
                IR.CmpOp.LT: ("<", "_pyck_float_lt"),
                IR.CmpOp.LTE: ("<=", "_pyck_float_lte"),
                IR.CmpOp.GT: (">", "_pyck_float_gt"),
                IR.CmpOp.GTE: (">=", "_pyck_float_gte"),
                IR.CmpOp.EQ: ("==", "_pyck_float_eq"),
                IR.CmpOp.NEQ: ("!=", "_pyck_float_neq"),
            }
            c_op, free_fn = cmp_map[op]
            if self.mode == "hosted":
                return f"({left} {c_op} {right})"
            return f"{free_fn}({left}, {right})"

        # For int/bool comparisons
        cmp_map = {
            IR.CmpOp.LT: ("PyLong_AsLong", "<"),
            IR.CmpOp.LTE: ("PyLong_AsLong", "<="),
            IR.CmpOp.GT: ("PyLong_AsLong", ">"),
            IR.CmpOp.GTE: ("PyLong_AsLong", ">="),
            IR.CmpOp.EQ: ("PyLong_AsLong", "=="),
            IR.CmpOp.NEQ: ("PyLong_AsLong", "!="),
        }
        converter, c_op = cmp_map[op]
        if self.mode == "hosted":
            return f"({converter}({left}) {c_op} {converter}({right}))"
        return f"(_pyck_long_as_long({left}) {c_op} _pyck_long_as_long({right}))"

    def _bool_singletons(self) -> tuple[str, str]:
        """Return the (true, false) PyObject* singleton names for this mode."""
        if self.mode == "hosted":
            return "Py_True", "Py_False"
        return "_pyck_true", "_pyck_false"

    def _truthiness_call(self, expr: str) -> str:
        """Wrap `expr` (a PyObject*) in a genuine Python-truthiness test,
        returning a native C++ bool."""
        if self.mode == "hosted":
            return f"PyObject_IsTrue({expr})"
        return f"_pyck_is_true({expr})"

    def _emit_test(self, node: IR.IRNode) -> str:
        """Emit a native C++ bool for `node` used as an if/while condition.

        Every expression (comparisons, and/or, bool/int/float variables,
        function calls, ...) is a PyObject* once emitted via _emit_expr, so
        route it through real Python truthiness rather than testing
        pointer non-nullness (which is always true for a valid PyObject*).
        """
        return self._truthiness_call(self._emit_expr(node))

    def _emit_func_call_expr(self, node: IR.IRNode) -> str:
        if node.func_name == "_ASM":
            return self._emit_asm_call(node)
        if node.func_name == "_ASM1":
            return self._emit_asm1_call(node)
        if node.func_name in self._HW_INTRINSICS:
            return self._emit_intrinsic_call(node)
        if node.func_name in self._MEMMAP_INTRINSICS:
            return self._emit_memmap_call(node)
        if node.func_name in self._ISR_INTRINSICS:
            return self._emit_isr_call(node)
        if node.func_name == "chr":
            return self._emit_chr_call(node)
        if node.func_name == "len":
            return self._emit_len_call(node)
        if node.func_name == "hex":
            return self._emit_hex_call(node)
        name = self._mangle_func_name(node.func_name)
        args = [self._emit_expr(a) for a in node.args]
        args_str = ", ".join(args)
        return f"{name}({args_str})"

    def _unbox_int(self, expr: str) -> str:
        """A raw (native) int64_t/long from a PyObject* int expression."""
        if self.mode == "hosted":
            return f"(int64_t)PyLong_AsLong({expr})"
        return f"_pyck_long_as_long({expr})"

    def _emit_chr_call(self, node: IR.IRNode) -> str:
        # chr(code) -> a single-character string. Hosted mode gets a real
        # CPython string object for free via PyUnicode_FromOrdinal (which
        # PyObject_Str/_pyck_fputPyObject already know how to print);
        # freestanding mode uses the minimal PyStrObject runtime type
        # (see runtime/freestanding/pyck_runtime.h).
        code_expr = self._emit_expr(node.args[0])
        if self.mode == "hosted":
            return f"PyUnicode_FromOrdinal((int)PyLong_AsLong({code_expr}))"
        return f"_pyck_char_from_code(_pyck_long_as_long({code_expr}))"

    def _emit_len_call(self, node: IR.IRNode) -> str:
        arg_expr = self._emit_expr(node.args[0])
        if self.mode == "hosted":
            return f"PyLong_FromLong((long)PyUnicode_GetLength({arg_expr}))"
        return f"_pyck_long_from_long(_pyck_str_length({arg_expr}))"

    def _emit_hex_call(self, node: IR.IRNode) -> str:
        # hex(n) -> a "0x..."/"-0x..." string. Same _pyck_int_to_hex name
        # in both modes -- hosted's version (in _emit_header's preamble)
        # and freestanding's (runtime/freestanding/py_runtime.cpp) share
        # an identical call site, unlike chr()/len() above.
        arg_expr = self._emit_expr(node.args[0])
        return f"_pyck_int_to_hex({arg_expr})"

    def _emit_asm_call(self, node: IR.IRNode) -> str:
        # _ASM's whole point is splicing literal assembly text straight
        # into the compiled kernel, so unlike every other builtin here
        # there's no runtime value to unbox -- typechecker.py already
        # rejected anything but a string literal argument, so this is
        # just re-escaping that exact text for the *generated* C++
        # file's own string literal syntax.
        if self.mode == "hosted":
            raise CodegenError(
                "'_ASM' requires direct hardware access and is only "
                "available in freestanding mode (hosted/userspace has no "
                "I/O privilege to execute arbitrary instructions)",
                node.line, node.col)
        arg = node.args[0] if node.args else None
        if arg is None or arg.kind != IR.IRNodeKind.STR_LIT:
            raise CodegenError("'_ASM' requires its argument to be a string literal",
                                node.line, node.col)
        # The clobber list below makes this *extended* asm (any operand
        # list, even one with empty output/input sections, switches GCC's
        # parser out of plain-text "basic asm" mode) -- which treats a
        # bare '%' as the start of an operand reference (%0, %%, ...), so
        # a literal register name like "%rax" in the source text has to
        # become "%%rax" or GCC reads it as a malformed operand number.
        escaped = (arg.str_value.replace("\\", "\\\\")
                   .replace('"', '\\"')
                   .replace("%", "%%")
                   .replace("\n", "\\n")
                   .replace("\t", "\\t"))
        # No real input/output operands -- the argument is always a fixed
        # literal, never something referencing a C++ variable -- so this
        # instead tells the compiler a blanket "assume everything scratch
        # might have changed" rather than binding anything specific.
        # rsp/rbp are deliberately left out: clobbering the stack/frame
        # pointer is not safe to ask for.
        clobbers = ('"rax", "rbx", "rcx", "rdx", "rsi", "rdi", '
                    '"r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15", '
                    '"memory", "cc"')
        return f'asm volatile("{escaped}" ::: {clobbers})'

    def _emit_asm1_call(self, node: IR.IRNode) -> str:
        # Same idea as _ASM, but the template carries one real GCC
        # extended-asm input operand (%0) bound to a genuine runtime
        # value -- typically a dynamically-allocated address -- instead
        # of everything being a fixed literal. Since %0 is a real operand
        # reference here, '%' is NOT auto-doubled the way _ASM's template
        # is: the template is expected to already be written as valid
        # extended-asm syntax (any literal '%' of its own spelled "%%" by
        # hand), matching how a human writing this same asm block
        # directly in C++ would have to write it too.
        if self.mode == "hosted":
            raise CodegenError(
                "'_ASM1' requires direct hardware access and is only "
                "available in freestanding mode (hosted/userspace has no "
                "I/O privilege to execute arbitrary instructions)",
                node.line, node.col)
        if len(node.args) != 2:
            raise CodegenError("'_ASM1' takes exactly 2 arguments", node.line, node.col)
        tmpl_arg, value_arg = node.args
        if tmpl_arg.kind != IR.IRNodeKind.STR_LIT:
            raise CodegenError("'_ASM1' requires its first argument to be a string literal",
                                node.line, node.col)
        escaped = (tmpl_arg.str_value.replace("\\", "\\\\")
                   .replace('"', '\\"')
                   .replace("\n", "\\n")
                   .replace("\t", "\\t"))
        value_expr = self._unbox_int(self._emit_expr(value_arg))
        return (f'asm volatile("{escaped}" :: "r"((unsigned long)({value_expr})) '
                f': "memory", "cc")')

    def _emit_str_getitem(self, node: IR.IRNode) -> str:
        target = self._emit_expr(node.subscript_target)
        index = self._unbox_int(self._emit_expr(node.subscript_index))
        return f"_pyck_str_getitem({target}, {index})"

    def _emit_str_slice(self, node: IR.IRNode) -> str:
        target = self._emit_expr(node.subscript_target)
        has_start = node.slice_start is not None
        has_stop = node.slice_stop is not None
        has_step = node.slice_step is not None
        start = self._unbox_int(self._emit_expr(node.slice_start)) if has_start else "0"
        stop = self._unbox_int(self._emit_expr(node.slice_stop)) if has_stop else "0"
        step = self._unbox_int(self._emit_expr(node.slice_step)) if has_step else "1"
        return (f"_pyck_str_slice({target}, {start}, {stop}, {step}, "
                f"{1 if has_start else 0}, {1 if has_stop else 0}, {1 if has_step else 0})")

    # name -> (C function name, [arg cast types], return cast type or None for void)
    _HW_INTRINSICS = {
        "inb":  ("inb", ["uint16_t"], "uint8_t"),
        "outb": ("outb", ["uint16_t", "uint8_t"], None),
        "inw":  ("_pyck_inw", ["uint16_t"], "uint16_t"),
        "outw": ("_pyck_outw", ["uint16_t", "uint16_t"], None),
        "inl":  ("_pyck_inl", ["uint16_t"], "uint32_t"),
        "outl": ("_pyck_outl", ["uint16_t", "uint32_t"], None),
        "rdmsr": ("_pyck_rdmsr", ["uint32_t"], "uint64_t"),
        "wrmsr": ("_pyck_wrmsr", ["uint32_t", "uint64_t"], None),
        "cpuid_eax": ("_pyck_cpuid_eax", ["uint32_t"], "uint32_t"),
        "cpuid_ebx": ("_pyck_cpuid_ebx", ["uint32_t"], "uint32_t"),
        "cpuid_ecx": ("_pyck_cpuid_ecx", ["uint32_t"], "uint32_t"),
        "cpuid_edx": ("_pyck_cpuid_edx", ["uint32_t"], "uint32_t"),
        "read_cr0":  ("_pyck_read_cr0", [], "unsigned long"),
        "write_cr0": ("_pyck_write_cr0", ["unsigned long"], None),
        "read_cr2":  ("_pyck_read_cr2", [], "unsigned long"),
        "write_cr2": ("_pyck_write_cr2", ["unsigned long"], None),
        "read_cr3":  ("_pyck_read_cr3", [], "unsigned long"),
        "write_cr3": ("_pyck_write_cr3", ["unsigned long"], None),
        "read_cr4":  ("_pyck_read_cr4", [], "unsigned long"),
        "write_cr4": ("_pyck_write_cr4", ["unsigned long"], None),
        "switch_context": ("_pyck_switch_context",
                            ["unsigned long", "unsigned long"], None),
        "raw_memzero": ("_pyck_raw_memzero", ["unsigned long", "unsigned long"], None),
        "raw_memcpy": ("_pyck_raw_memcpy",
                       ["unsigned long", "unsigned long", "unsigned long"], None),
        "print_char": ("_pyck_print_char", ["int64_t"], None),
        "_EXEC_INTO_RING3": ("_pyck_exec_into_ring3",
                              ["unsigned long", "unsigned long"], None),
        "_SET_SYSCALL_STACK_TOP": ("_pyck_set_syscall_stack_top",
                                    ["unsigned long"], None),
    }

    def _emit_intrinsic_call(self, node: IR.IRNode) -> str:
        name = node.func_name
        if self.mode == "hosted":
            raise CodegenError(
                f"'{name}' requires direct hardware access and is only "
                f"available in freestanding mode (hosted/userspace has no "
                f"I/O privilege to execute it)",
                node.line, node.col)
        c_name, arg_casts, ret_cast = self._HW_INTRINSICS[name]
        args_str = ", ".join(
            f"({cast})_pyck_long_as_long({self._emit_expr(a)})"
            for cast, a in zip(arg_casts, node.args)
        )
        call = f"{c_name}({args_str})"
        if ret_cast is None:
            return call
        return f"_pyck_long_from_long((int64_t){call})"

    # name -> (C function name, [arg cast types], return cast type or None for void)
    _MEMMAP_INTRINSICS = {
        "_LIMINE": ("_pyck_limine_table_addr", [], "uint64_t"),
    }

    # name -> (C function name, [arg cast types]) -- see arch/x86_64_shared/
    # idt_stubs.cpp; always returns uint64_t, like _MEMMAP_INTRINSICS.
    _ISR_INTRINSICS = {
        "_ISR_STUB": ("_pyck_isr_stub_addr", ["uint64_t"]),
        "_THREAD_TRAMPOLINE_ADDR": ("_pyck_thread_trampoline_addr", []),
        "_RING3_TRAMPOLINE_ADDR": ("_pyck_ring3_trampoline_addr", []),
        "_SYSCALL_ENTRY_ADDR": ("_pyck_syscall_entry_addr", []),
        "_FORK_REGS_TABLE_ADDR": ("_pyck_fork_regs_table_addr", []),
        "_FORK_CHILD_TRAMPOLINE_ADDR": ("_pyck_fork_child_trampoline_addr", []),
    }

    def _emit_isr_call(self, node: IR.IRNode) -> str:
        name = node.func_name
        if self.mode == "hosted":
            raise CodegenError(
                f"'{name}' requires real interrupt gates and is only "
                f"available in freestanding mode (a hosted process has no "
                f"such concept)",
                node.line, node.col)
        c_name, arg_casts = self._ISR_INTRINSICS[name]
        args_str = ", ".join(
            f"({cast})_pyck_long_as_long({self._emit_expr(a)})"
            for cast, a in zip(arg_casts, node.args)
        )
        call = f"{c_name}({args_str})"
        return f"_pyck_long_from_long((int64_t){call})"

    def _emit_memmap_call(self, node: IR.IRNode) -> str:
        name = node.func_name
        if self.mode == "hosted":
            raise CodegenError(
                f"'{name}' requires the Limine boot protocol and is only "
                f"available in freestanding mode (a hosted process has no "
                f"such concept)",
                node.line, node.col)
        c_name, arg_casts, _ret_cast = self._MEMMAP_INTRINSICS[name]
        args_str = ", ".join(
            f"({cast})_pyck_long_as_long({self._emit_expr(a)})"
            for cast, a in zip(arg_casts, node.args)
        )
        call = f"{c_name}({args_str})"
        return f"_pyck_long_from_long((int64_t){call})"

    def _emit_peek_expr(self, node: IR.IRNode) -> str:
        addr = self._unbox_int(self._emit_expr(node.peek_addr))
        width = node.peek_width
        if self.mode == "hosted":
            return f"PyLong_FromLong(*reinterpret_cast<volatile uint{width}_t*>((uintptr_t)({addr})))"
        return f"_pyck_long_from_long(*reinterpret_cast<volatile uint{width}_t*>((uintptr_t)({addr})))"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # Built-in function names that don't need mangling
    _BUILTIN_NAMES = {"print", "range", "peek8", "peek16", "peek32", "peek64",
                      "poke8", "poke16", "poke32", "poke64"}

    def _mangle_func_name(self, name: str) -> str:
        """Mangle Python function names to avoid C name collisions."""
        if name in self._BUILTIN_NAMES:
            return name
        return f"_pyck_{name}"

    def _type_to_cpp(self, typ: PyType) -> str:
        if typ.kind == PyTypeKind.NONE:
            return "void"
        return "PyObject*"

    def _line(self, text: str):
        self._lines.append("    " * self._indent + text)
