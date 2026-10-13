"""Multi-file module resolution for PyCk.

PyCk still transpiles to a single compiled unit: every resolved file's
top-level statements get concatenated (in dependency order) into one
combined AST, exactly as PythonOS/build.py's old string-concatenation
trick did. What changes is *which* files get included, and what each
file is allowed to reference: a file's own top-level def/class/
assignments are always visible to itself, and anything else -- a name
defined in a different file -- has to be named in that file's own
`from <module> import ...`, or it's a compile error, even if some other
file's import happens to pull the same name into the overall program
anyway. Without that check, a file could reference a name it never
declared a dependency on, as long as *something else* needed that file
concatenated in too -- exactly the silent-implicit-access this module
exists to catch.

Only `from <module> import name1, name2` and `from <module> import *`
are supported, resolved relative to the entry file's own directory --
no dotted packages, no relative (`.`) imports, no plain `import module`
(this project's source never qualifies a name with a module prefix, so
there'd be nothing for that form to do).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


class ModuleResolutionError(Exception):
    """Raised for anything wrong with the import graph itself (missing
    file, circular import, unsupported import form, importing a name
    that doesn't exist) -- distinct from CompileError, which is for
    errors PyCk finds *within* otherwise-resolved source."""


@dataclass
class ModuleInfo:
    name: str
    path: Path
    tree: ast.Module
    bound_names: set[str] = field(default_factory=set)
    # imported name -> module name that provides it
    imports: dict[str, str] = field(default_factory=dict)
    star_imports: list[str] = field(default_factory=list)
    import_nodes: list[ast.ImportFrom] = field(default_factory=list)


# Every name PyCk recognizes without an import: builtin functions, the
# four annotation type names, and the hardware/Limine/ISR intrinsic
# tables (imported here rather than duplicated).
def _builtin_names() -> set[str]:
    from .typechecker import HARDWARE_INTRINSICS, LIMINE_INTRINSICS, ISR_INTRINSICS
    names = {
        "print", "len", "hex", "chr", "range",
        "peek8", "peek16", "peek32", "peek64",
        "poke8", "poke16", "poke32", "poke64",
        "raw_memzero", "raw_memcpy",
        "_ASM", "_ASM1",
        "int", "bool", "str", "float",
        "True", "False", "None",
    }
    names |= set(HARDWARE_INTRINSICS)
    names |= set(LIMINE_INTRINSICS)
    names |= set(ISR_INTRINSICS)
    return names


def _collect_bound_names(tree: ast.Module) -> set[str]:
    """Every name a file binds at its own top level: function defs,
    class defs, and assigned globals. Doesn't descend into function/
    class bodies -- those are local to whoever defines them, not part
    of what this file exports."""
    names = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name):
                names.add(stmt.target.id)
    return names


def _tag_nodes_with_file(tree: ast.AST, filename: str) -> None:
    """Stamp every node in `tree` with which source file it actually
    came from, so an error found after merging multiple files' ASTs
    together still points at the right one (see typechecker.py's
    _error and ast_to_ir.py's IRBuildError)."""
    for node in ast.walk(tree):
        node._pyck_file = filename


def _parse_module(path: Path) -> ModuleInfo:
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    _tag_nodes_with_file(tree, str(path))
    mod = ModuleInfo(name=path.stem, path=path, tree=tree)
    mod.bound_names = _collect_bound_names(tree)
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom):
            if stmt.level != 0:
                raise ModuleResolutionError(
                    f"{path}:{stmt.lineno}: relative imports ('.') are not supported")
            if stmt.module is None:
                raise ModuleResolutionError(f"{path}:{stmt.lineno}: invalid import")
            mod.import_nodes.append(stmt)
            for alias in stmt.names:
                if alias.name == "*":
                    mod.star_imports.append(stmt.module)
                else:
                    imported_as = alias.asname or alias.name
                    mod.imports[imported_as] = stmt.module
        elif isinstance(stmt, ast.Import):
            raise ModuleResolutionError(
                f"{path}:{stmt.lineno}: 'import {stmt.names[0].name}' is not "
                f"supported -- use 'from {stmt.names[0].name} import name1, "
                f"name2' instead")
    return mod


def resolve_program(entry_path: Path) -> list[ModuleInfo]:
    """Parse `entry_path` and every file it (transitively) imports from,
    validate the import graph, and return every reachable module in
    dependency order (a module always appears after every module it
    imports from; `entry_path` itself is always last). Raises
    ModuleResolutionError for a missing file, a circular import, an
    unsupported import form, or `from X import name` where `name`
    doesn't actually exist at X's top level."""
    entry_path = entry_path.resolve()
    base_dir = entry_path.parent
    modules: dict[str, ModuleInfo] = {}
    order: list[str] = []
    visiting: set[str] = set()

    def visit(name: str, stack: list[str]) -> None:
        if name in modules:
            return
        if name in visiting:
            cycle = " -> ".join(stack + [name])
            raise ModuleResolutionError(f"circular import: {cycle}")
        path = base_dir / f"{name}.py"
        if not path.exists():
            importer = stack[-1] if stack else str(entry_path)
            raise ModuleResolutionError(
                f"cannot import '{name}' (needed by {importer}): "
                f"no such file {path}")
        visiting.add(name)
        mod = _parse_module(path)
        deps = sorted(set(mod.imports.values()) | set(mod.star_imports))
        for dep in deps:
            visit(dep, stack + [name])
        visiting.discard(name)
        modules[name] = mod
        order.append(name)

    visit(entry_path.stem, [])

    for mod in modules.values():
        for imported_name, src_name in mod.imports.items():
            src_mod = modules[src_name]
            if imported_name not in src_mod.bound_names:
                node = next(n for n in mod.import_nodes if n.module == src_name)
                raise ModuleResolutionError(
                    f"{mod.path}:{node.lineno}: '{src_name}' has no top-level "
                    f"'{imported_name}' to import")

    _check_scopes(modules)

    return [modules[name] for name in order]


def _visible_names(mod: ModuleInfo, modules: dict[str, ModuleInfo]) -> set[str]:
    visible = set(mod.bound_names) | set(mod.imports) | _builtin_names()
    for star_src in mod.star_imports:
        visible |= modules[star_src].bound_names
    return visible


class _ScopeWalker(ast.NodeVisitor):
    """Walks one file's AST checking every loaded name against that
    file's own visible-names set (its top-level defs/classes/globals,
    whatever it imported, and builtins) -- catching a reference to a
    name that exists *somewhere* in the compiled program but that this
    particular file never declared a dependency on."""

    def __init__(self, filename: str, allowed: set[str], own_bound_names: set[str]):
        self.filename = filename
        self.allowed = allowed
        # Real Python's `global x` only ever reaches into the *current*
        # module's own namespace -- it can't rebind another file's
        # variable through an import (that would just create/shadow a
        # same-named local in this file instead). So `global` gets
        # checked against this narrower set, not the full `allowed`
        # (which also includes imports and builtins).
        self.own_bound_names = own_bound_names
        self.local_stack: list[set[str]] = [set()]
        self.errors: list[str] = []

    def _bind(self, name: str) -> None:
        self.local_stack[-1].add(name)

    def _is_visible(self, name: str) -> bool:
        if name in self.allowed:
            return True
        return any(name in scope for scope in self.local_stack)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and not self._is_visible(node.id):
            self.errors.append(
                f"{self.filename}:{node.lineno}:{node.col_offset}: error: "
                f"name '{node.id}' is not defined in this file "
                f"(forgot a 'from ... import {node.id}'?)")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.local_stack.append(set())
        for arg in node.args.args:
            self._bind(arg.arg)
        for stmt in node.body:
            self.visit(stmt)
        self.local_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for stmt in node.body:
            self.visit(stmt)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for t in node.targets:
            if isinstance(t, ast.Name):
                self._bind(t.id)
            else:
                self.visit(t)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        if isinstance(node.target, ast.Name):
            self._bind(node.target.id)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        if isinstance(node.target, ast.Name):
            self._bind(node.target.id)
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        pass  # already handled by resolve_program; nothing to check here

    def visit_Import(self, node: ast.Import) -> None:
        pass  # resolve_program already rejected this form

    def visit_Global(self, node: ast.Global) -> None:
        for name in node.names:
            if name not in self.own_bound_names:
                self.errors.append(
                    f"{self.filename}:{node.lineno}:{node.col_offset}: error: "
                    f"'{name}' is declared global here but isn't assigned "
                    f"anywhere at this file's own top level (an imported name "
                    f"can't be reached through 'global')")


def _check_scopes(modules: dict[str, ModuleInfo]) -> None:
    errors: list[str] = []
    for mod in modules.values():
        allowed = _visible_names(mod, modules)
        walker = _ScopeWalker(str(mod.path), allowed, mod.bound_names)
        for stmt in mod.tree.body:
            walker.visit(stmt)
        errors.extend(walker.errors)
    if errors:
        raise ModuleResolutionError("\n".join(errors))


def merge_modules(modules: list[ModuleInfo]) -> ast.Module:
    """Concatenate every module's top-level statements, in the order
    resolve_program already put them in, into one ast.Module -- the
    same flattening PythonOS/build.py used to do by pasting source text
    together, just done on already-parsed trees instead."""
    body = []
    for mod in modules:
        body.extend(mod.tree.body)
    merged = ast.Module(body=body, type_ignores=[])
    return merged
