"""Unit tests for pyck.types: PyType predicates and the unify() rules."""
import unittest

from pyck.types import (
    PYBOOL, PYFLOAT, PYINT, PYNONE, PYSTRING, PYUNKNOWN, unify,
)


class TestPyTypePredicates(unittest.TestCase):
    def test_is_numeric(self):
        self.assertTrue(PYINT.is_numeric())
        self.assertTrue(PYFLOAT.is_numeric())
        self.assertTrue(PYBOOL.is_numeric())
        self.assertFalse(PYSTRING.is_numeric())
        self.assertFalse(PYNONE.is_numeric())

    def test_str(self):
        self.assertEqual(str(PYINT), "int")
        self.assertEqual(str(PYFLOAT), "float")
        self.assertEqual(str(PYBOOL), "bool")
        self.assertEqual(str(PYNONE), "none")

    def test_is_assignable_from_same_kind(self):
        self.assertTrue(PYINT.is_assignable_from(PYINT))
        self.assertTrue(PYSTRING.is_assignable_from(PYSTRING))

    def test_int_bool_mutually_assignable(self):
        self.assertTrue(PYINT.is_assignable_from(PYBOOL))
        self.assertTrue(PYBOOL.is_assignable_from(PYINT))

    def test_float_widens_from_int_and_bool(self):
        self.assertTrue(PYFLOAT.is_assignable_from(PYINT))
        self.assertTrue(PYFLOAT.is_assignable_from(PYBOOL))

    def test_int_does_not_narrow_from_float(self):
        self.assertFalse(PYINT.is_assignable_from(PYFLOAT))
        self.assertFalse(PYBOOL.is_assignable_from(PYFLOAT))

    def test_string_not_assignable_from_numeric(self):
        self.assertFalse(PYSTRING.is_assignable_from(PYINT))
        self.assertFalse(PYSTRING.is_assignable_from(PYFLOAT))


class TestUnify(unittest.TestCase):
    def test_same_kind(self):
        self.assertEqual(unify(PYINT, PYINT), PYINT)
        self.assertEqual(unify(PYFLOAT, PYFLOAT), PYFLOAT)
        self.assertEqual(unify(PYBOOL, PYBOOL), PYBOOL)

    def test_unknown_propagates_other_operand(self):
        self.assertEqual(unify(PYUNKNOWN, PYINT), PYINT)
        self.assertEqual(unify(PYINT, PYUNKNOWN), PYINT)
        self.assertEqual(unify(PYUNKNOWN, PYFLOAT), PYFLOAT)

    def test_int_bool_unifies_to_int(self):
        self.assertEqual(unify(PYINT, PYBOOL), PYINT)
        self.assertEqual(unify(PYBOOL, PYINT), PYINT)

    def test_int_float_unifies_to_float(self):
        self.assertEqual(unify(PYINT, PYFLOAT), PYFLOAT)
        self.assertEqual(unify(PYFLOAT, PYINT), PYFLOAT)

    def test_bool_float_unifies_to_float(self):
        self.assertEqual(unify(PYBOOL, PYFLOAT), PYFLOAT)
        self.assertEqual(unify(PYFLOAT, PYBOOL), PYFLOAT)

    def test_incompatible_kinds_raise(self):
        with self.assertRaises(TypeError):
            unify(PYSTRING, PYINT)
        with self.assertRaises(TypeError):
            unify(PYNONE, PYINT)
        with self.assertRaises(TypeError):
            unify(PYSTRING, PYFLOAT)


if __name__ == "__main__":
    unittest.main()
