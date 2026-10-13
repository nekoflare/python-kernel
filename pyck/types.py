"""Type system for the supported Python subset (v1).

Everything is a PyObject*. The type system tracks which concrete CPython C API
(or freestanding-runtime) operations each expression needs.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto


class PyTypeKind(Enum):
    INT = auto()
    FLOAT = auto()
    BOOL = auto()
    STR = auto()    # string literals only (print args); not a first-class v1 type
    NONE = auto()
    CLASS = auto()  # a user-defined class instance; see PyType.class_name
    UNKNOWN = auto()  # inference placeholder


@dataclass(frozen=True)
class PyType:
    kind: PyTypeKind
    class_name: str | None = None  # only meaningful when kind == CLASS

    def __str__(self) -> str:
        if self.kind == PyTypeKind.CLASS:
            return self.class_name or "class"
        return self.kind.name.lower()

    def is_numeric(self) -> bool:
        return self.kind in (PyTypeKind.INT, PyTypeKind.FLOAT, PyTypeKind.BOOL)

    def is_assignable_from(self, other: PyType) -> bool:
        if self.kind == PyTypeKind.CLASS or other.kind == PyTypeKind.CLASS:
            return self.kind == other.kind and self.class_name == other.class_name
        if self.kind == other.kind:
            return True
        # int/bool are compatible (bool subtype of int, like Python)
        if {self.kind, other.kind} <= {PyTypeKind.INT, PyTypeKind.BOOL}:
            return True
        # int + float -> float is allowed on assignment (widening)
        if self.kind == PyTypeKind.FLOAT and other.kind in (PyTypeKind.INT, PyTypeKind.BOOL):
            return True
        return False


# Singleton types
PYINT = PyType(PyTypeKind.INT)
PYFLOAT = PyType(PyTypeKind.FLOAT)
PYBOOL = PyType(PyTypeKind.BOOL)
PYSTRING = PyType(PyTypeKind.STR)
PYNONE = PyType(PyTypeKind.NONE)
PYUNKNOWN = PyType(PyTypeKind.UNKNOWN)


def unify(a: PyType, b: PyType) -> PyType:
    """Unify two types: return the common supertype, or raise if incompatible."""
    if a.kind == PyTypeKind.UNKNOWN:
        return b
    if b.kind == PyTypeKind.UNKNOWN:
        return a
    if a.kind == PyTypeKind.CLASS or b.kind == PyTypeKind.CLASS:
        if a.kind == b.kind and a.class_name == b.class_name:
            return a
        raise TypeError(f"Cannot unify {a} and {b}")
    if a.kind == b.kind:
        return a
    # bool + int -> int
    if {a.kind, b.kind} <= {PyTypeKind.INT, PyTypeKind.BOOL}:
        return PYINT
    # int + float -> float
    if a.kind == PyTypeKind.INT and b.kind == PyTypeKind.FLOAT:
        return PYFLOAT
    if a.kind == PyTypeKind.FLOAT and b.kind == PyTypeKind.INT:
        return PYFLOAT
    # bool + float -> float
    if a.kind == PyTypeKind.BOOL and b.kind == PyTypeKind.FLOAT:
        return PYFLOAT
    if a.kind == PyTypeKind.FLOAT and b.kind == PyTypeKind.BOOL:
        return PYFLOAT
    raise TypeError(f"Cannot unify {a} and {b}")
