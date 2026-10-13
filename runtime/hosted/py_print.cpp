"""Default hosted py_print implementation for PyCk.

Uses CPython's own object-printing machinery to implement print().
This is linked automatically in hosted mode.
"""
#include <Python.h>
#include <cstdint>

extern "C" void py_print(PyObject* args, PyObject* sep, PyObject* end) {
    Py_ssize_t nargs = PyTuple_GET_SIZE(args);
    for (Py_ssize_t i = 0; i < nargs; i++) {
        if (i > 0) {
            PyObject_Print(sep, stdout, 0);
        }
        PyObject_Print(PyTuple_GET_ITEM(args, i), stdout, 0);
    }
    PyObject_Print(end, stdout, 0);
}

// Default allocator for hosted mode (uses system malloc)
extern "C" {
    void init_heap(void* base, size_t size) {
        // No-op in hosted mode; heap is provided by the OS
        (void)base; (void)size;
    }

    void* py_alloc(size_t size) {
        return malloc(size);
    }

    void py_free(void* ptr) {
        free(ptr);
    }
}
