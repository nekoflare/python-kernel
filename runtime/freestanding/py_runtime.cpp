/* Freestanding runtime implementation for PyCk.
 *
 * Provides bignum, float, bool, tuple, refcounting, and allocator.
 * Adapted from CPython's longobject.c for arbitrary-precision integers.
 */
#include "pyck_runtime.h"
#include <string.h>

// ---------------------------------------------------------------------------
// Forward declarations
// ---------------------------------------------------------------------------

static void _pyck_long_dealloc(PyObject* self);
static void _pyck_float_dealloc(PyObject* self);
static void _pyck_char_dealloc(PyObject* self);

// ---------------------------------------------------------------------------
// Singletons
// ---------------------------------------------------------------------------

PyObject _pyck_none_instance = { 1, &_pyck_type_bool };
PyObject _pyck_true_instance = { 1, &_pyck_type_bool };
PyObject _pyck_false_instance = { 1, &_pyck_type_bool };

PyTypeObject _pyck_type_int = {
    { 1, 0 },
    "int",
    _pyck_long_dealloc,
};

PyTypeObject _pyck_type_float = {
    { 1, 0 },
    "float",
    _pyck_float_dealloc,
};

PyTypeObject _pyck_type_bool = {
    { 1, 0 },
    "bool",
    0,  // bool singletons are never deallocated
};

PyTypeObject _pyck_type_str = {
    { 1, 0 },
    "str",
    _pyck_char_dealloc,
};

// ---------------------------------------------------------------------------
// Minimal runtime string ("str") object -- see pyck_runtime.h
// ---------------------------------------------------------------------------

static void _pyck_char_dealloc(PyObject* self) {
    py_free(self);
}

static PyStrObject* _pyck_str_alloc(Py_ssize_t length) {
    size_t alloc_size = sizeof(PyStrObject) + (length > 1 ? (size_t)(length - 1) : 0);
    PyStrObject* obj = (PyStrObject*)py_alloc(alloc_size);
    obj->ob_base.ob_refcnt = 1;
    obj->ob_base.ob_type = &_pyck_type_str;
    obj->length = length;
    return obj;
}

PyObject* _pyck_char_from_code(int64_t code) {
    PyStrObject* obj = _pyck_str_alloc(1);
    obj->data[0] = (char)(code & 0xFF);
    return (PyObject*)obj;
}

PyObject* _pyck_str_concat(PyObject* a, PyObject* b) {
    PyStrObject* sa = (PyStrObject*)a;
    PyStrObject* sb = (PyStrObject*)b;
    PyStrObject* obj = _pyck_str_alloc(sa->length + sb->length);
    for (Py_ssize_t i = 0; i < sa->length; i++) obj->data[i] = sa->data[i];
    for (Py_ssize_t i = 0; i < sb->length; i++) obj->data[sa->length + i] = sb->data[i];
    return (PyObject*)obj;
}

Py_ssize_t _pyck_str_length(PyObject* obj) {
    return ((PyStrObject*)obj)->length;
}

const char* _pyck_str_data(PyObject* obj) {
    return ((PyStrObject*)obj)->data;
}

PyObject* _pyck_str_from_cstr(const char* s, Py_ssize_t len) {
    PyStrObject* obj = _pyck_str_alloc(len);
    for (Py_ssize_t i = 0; i < len; i++) obj->data[i] = s[i];
    return (PyObject*)obj;
}

PyObject* _pyck_str_getitem(PyObject* s, int64_t index) {
    PyStrObject* so = (PyStrObject*)s;
    int64_t len = so->length;
    if (index < 0) index += len;
    if (index < 0 || index >= len) {
        py_fatal_error("string index out of range");
    }
    return _pyck_char_from_code((unsigned char)so->data[index]);
}

PyObject* _pyck_str_slice(PyObject* s, int64_t start, int64_t stop, int64_t step,
                           int has_start, int has_stop, int has_step) {
    // Faithfully reproduces CPython's slice index-adjustment algorithm
    // (PySlice_AdjustIndices): default start/stop depend on step's sign,
    // out-of-range bounds clamp instead of erroring, and the resulting
    // length is computed directly rather than by stepping and counting.
    PyStrObject* so = (PyStrObject*)s;
    int64_t len = so->length;
    int64_t st = has_step ? step : 1;
    if (st == 0) {
        py_fatal_error("slice step cannot be zero");
    }
    // Omitted bounds default to extreme sentinels (INT64_MAX / -INT64_MAX),
    // not directly to len-1/-1: run through the exact same clamp below as
    // an explicit index would, they collapse to the right default (e.g.
    // a huge negative "stop" plus len is still deeply negative, so it
    // still hits the "clamp to -1" branch). Using -1/len-1 directly here
    // instead breaks when the *other* bound is a real negative index,
    // since -1 alone is indistinguishable from "user explicitly wrote -1".
    int64_t a = has_start ? start : (st < 0 ? INT64_MAX : (int64_t)0);
    int64_t b = has_stop ? stop : (st < 0 ? -INT64_MAX : INT64_MAX);
    if (a < 0) { a += len; if (a < 0) a = (st < 0) ? -1 : 0; }
    else if (a >= len) { a = (st < 0) ? len - 1 : len; }
    if (b < 0) { b += len; if (b < 0) b = (st < 0) ? -1 : 0; }
    else if (b >= len) { b = (st < 0) ? len - 1 : len; }

    Py_ssize_t result_len = 0;
    if (st < 0) { if (b < a) result_len = (a - b - 1) / (-st) + 1; }
    else { if (a < b) result_len = (b - a - 1) / st + 1; }

    PyStrObject* obj = _pyck_str_alloc(result_len);
    int64_t idx = a;
    for (Py_ssize_t i = 0; i < result_len; i++) {
        obj->data[i] = so->data[idx];
        idx += st;
    }
    return (PyObject*)obj;
}

int _pyck_str_cmp(PyObject* a, PyObject* b) {
    // strcmp-style: <0, 0, >0. Lexicographic on raw (ASCII) bytes.
    PyStrObject* sa = (PyStrObject*)a;
    PyStrObject* sb = (PyStrObject*)b;
    Py_ssize_t n = sa->length < sb->length ? sa->length : sb->length;
    for (Py_ssize_t i = 0; i < n; i++) {
        unsigned char ca = (unsigned char)sa->data[i];
        unsigned char cb = (unsigned char)sb->data[i];
        if (ca != cb) return (int)ca - (int)cb;
    }
    return (int)(sa->length - sb->length);
}

PyObject* _pyck_str_repeat(PyObject* s, int64_t n) {
    PyStrObject* so = (PyStrObject*)s;
    if (n < 0) n = 0;
    PyStrObject* obj = _pyck_str_alloc(so->length * n);
    Py_ssize_t pos = 0;
    for (int64_t i = 0; i < n; i++) {
        for (Py_ssize_t j = 0; j < so->length; j++) {
            obj->data[pos++] = so->data[j];
        }
    }
    return (PyObject*)obj;
}

// ---------------------------------------------------------------------------
// Recursion depth
// ---------------------------------------------------------------------------

int64_t _pyck_max_recursion_depth = 10000;

// ---------------------------------------------------------------------------
// Fatal error (arch kit must override)
// ---------------------------------------------------------------------------

__attribute__((weak))
void py_fatal_error(const char* message) {
    // Fallback: infinite loop (arch kit should provide a real implementation)
    (void)message;
    while(1) {}
}

// ---------------------------------------------------------------------------
// Bignum helpers (adapted from CPython longobject.c)
// ---------------------------------------------------------------------------

static PyLongObject* _long_alloc(Py_ssize_t ndigits) {
    size_t size = sizeof(PyLongObject) + (ndigits > 1 ? (ndigits - 1) * sizeof(digit) : 0);
    PyLongObject* obj = (PyLongObject*)py_alloc(size);
    obj->ob_base.ob_refcnt = 1;
    obj->ob_base.ob_type = &_pyck_type_int;
    obj->ob_size = 0;
    memset(obj->ob_digit, 0, ndigits * sizeof(digit));
    return obj;
}

static void _pyck_long_dealloc(PyObject* self) {
    PyLongObject* lo = (PyLongObject*)self;
    py_free(lo);
}

static PyFloatObject* _float_alloc(double value) {
    PyFloatObject* obj = (PyFloatObject*)py_alloc(sizeof(PyFloatObject));
    obj->ob_base.ob_refcnt = 1;
    obj->ob_base.ob_type = &_pyck_type_float;
    obj->ob_fval = value;
    return obj;
}

static void _pyck_float_dealloc(PyObject* self) {
    PyFloatObject* fo = (PyFloatObject*)self;
    py_free(fo);
}

// Count used digits (strip leading zeros)
static Py_ssize_t _long_normalize(PyLongObject* v) {
    Py_ssize_t i = v->ob_size;
    if (i == 0) return 0;
    while (i > 0 && v->ob_digit[i - 1] == 0) i--;
    v->ob_size = (v->ob_size < 0) ? -i : i;
    return i;
}

// Absolute comparison: |a| vs |b|, returns -1/0/1
static int _long_abs_compare(const PyLongObject* a, const PyLongObject* b) {
    Py_ssize_t asize = a->ob_size < 0 ? -a->ob_size : a->ob_size;
    Py_ssize_t bsize = b->ob_size < 0 ? -b->ob_size : b->ob_size;
    if (asize < bsize) return -1;
    if (asize > bsize) return 1;
    for (Py_ssize_t i = asize - 1; i >= 0; i--) {
        if (a->ob_digit[i] < b->ob_digit[i]) return -1;
        if (a->ob_digit[i] > b->ob_digit[i]) return 1;
    }
    return 0;
}

// Add magnitudes: result = |a| + |b|
static PyLongObject* _long_add_magnitudes(const PyLongObject* a, const PyLongObject* b) {
    Py_ssize_t asize = a->ob_size < 0 ? -a->ob_size : a->ob_size;
    Py_ssize_t bsize = b->ob_size < 0 ? -b->ob_size : b->ob_size;
    Py_ssize_t maxsize = asize > bsize ? asize : bsize;
    PyLongObject* z = _long_alloc(maxsize + 1);
    digit carry = 0;
    for (Py_ssize_t i = 0; i < maxsize; i++) {
        digit av = (i < asize) ? a->ob_digit[i] : 0;
        digit bv = (i < bsize) ? b->ob_digit[i] : 0;
        digit sum = av + bv + carry;
        z->ob_digit[i] = sum & PyLong_MASK;
        carry = sum >> PyLong_SHIFT;
    }
    z->ob_digit[maxsize] = carry;
    z->ob_size = maxsize + 1;
    _long_normalize(z);
    return z;
}

// Subtract magnitudes: |a| - |b| (caller ensures |a| >= |b|)
static PyLongObject* _long_sub_magnitudes(const PyLongObject* a, const PyLongObject* b) {
    Py_ssize_t asize = a->ob_size < 0 ? -a->ob_size : a->ob_size;
    Py_ssize_t bsize = b->ob_size < 0 ? -b->ob_size : b->ob_size;
    PyLongObject* z = _long_alloc(asize);
    digit borrow = 0;
    for (Py_ssize_t i = 0; i < asize; i++) {
        digit av = a->ob_digit[i];
        digit bv = (i < bsize) ? b->ob_digit[i] : 0;
        digit diff = av - bv - borrow;
        // Handle underflow
        if (diff > av) {
            diff += PyLong_BASE;
            borrow = 1;
        } else {
            borrow = 0;
        }
        z->ob_digit[i] = diff;
    }
    z->ob_size = asize;
    _long_normalize(z);
    return z;
}

// Negate a bignum
static PyLongObject* _long_negate(const PyLongObject* a) {
    Py_ssize_t asize = a->ob_size;
    if (asize == 0) return _long_alloc(0);
    PyLongObject* z = _long_alloc(asize < 0 ? -asize : asize);
    for (Py_ssize_t i = 0; i < (asize < 0 ? -asize : asize); i++) {
        z->ob_digit[i] = a->ob_digit[i];
    }
    z->ob_size = (asize > 0) ? -asize : -asize;  // flip sign
    // Actually: if a is positive, result is negative (size = -abs_size)
    if (asize > 0) z->ob_size = -asize;
    else z->ob_size = -asize;  // double negative = positive
    _long_normalize(z);
    return z;
}

// Multiply two bignums
static PyLongObject* _long_mul(const PyLongObject* a, const PyLongObject* b) {
    Py_ssize_t asize = a->ob_size < 0 ? -a->ob_size : a->ob_size;
    Py_ssize_t bsize = b->ob_size < 0 ? -b->ob_size : b->ob_size;
    PyLongObject* z = _long_alloc(asize + bsize);
    for (Py_ssize_t i = 0; i < asize; i++) {
        digit carry = 0;
        for (Py_ssize_t j = 0; j < bsize; j++) {
            uint64_t prod = (uint64_t)a->ob_digit[i] * b->ob_digit[j]
                            + z->ob_digit[i + j] + carry;
            z->ob_digit[i + j] = (digit)(prod & PyLong_MASK);
            carry = (digit)(prod >> PyLong_SHIFT);
        }
        z->ob_digit[i + bsize] = carry;
    }
    // Normalize the (always non-negative) magnitude size *before* applying
    // the sign -- _long_normalize()'s trim loop only works on a
    // non-negative size; feeding it a pre-negated size (as this used to
    // do) silently flips it straight back to positive, same bug that used
    // to be in _pyck_bignum_sub/_pyck_bignum_neg.
    z->ob_size = asize + bsize;
    _long_normalize(z);
    int neg = (a->ob_size < 0) ^ (b->ob_size < 0);
    if (neg) z->ob_size = -z->ob_size;
    return z;
}

// floor_div and mod using Knuth's algorithm D (simplified)
static void _long_divmod(const PyLongObject* a, const PyLongObject* b,
                          PyLongObject** quotient, PyLongObject** remainder) {
    // For simplicity, use repeated subtraction for small values
    // and the full algorithm for larger ones
    int a_neg = a->ob_size < 0;
    int b_neg = b->ob_size < 0;
    Py_ssize_t asize = a_neg ? -a->ob_size : a->ob_size;
    Py_ssize_t bsize = b_neg ? -b->ob_size : b->ob_size;

    if (bsize == 0) {
        py_fatal_error("division by zero");
        *quotient = _long_alloc(0);
        *remainder = _long_alloc(0);
        return;
    }

    // Use the Python floor division algorithm:
    // q = floor(a / b), r = a - q * b
    // For positive numbers, this is straightforward.
    // For negative numbers, floor division rounds toward -infinity.

    // Simple approach: compute truncating div, then adjust for floor
    // For now, use a simple loop-based approach for correctness
    // (performance optimization comes later)

    // Determine sign of result
    int result_neg = a_neg ^ b_neg;

    // Work with absolute values
    PyLongObject* abs_a = _long_alloc(asize);
    PyLongObject* abs_b = _long_alloc(bsize);
    memcpy(abs_a->ob_digit, a->ob_digit, asize * sizeof(digit));
    abs_a->ob_size = asize;
    memcpy(abs_b->ob_digit, b->ob_digit, bsize * sizeof(digit));
    abs_b->ob_size = bsize;

    // Binary long division via repeated doubling: each outer step finds
    // the largest value b*2^k that still fits under the remainder (by
    // doubling a copy of b, and a matching power-of-two "1*2^k", in
    // lockstep as long as it fits), then subtracts that doubled divisor
    // and adds its doubled-quotient contribution. Doubling is just
    // "add a number to itself" (_long_add_magnitudes(x, x)), so this
    // reuses the exact same magnitude add/sub/compare helpers used
    // everywhere else in this file -- no separate bit-shift primitive
    // needed.
    //
    // Each outer step at least halves what's left of the remainder, so
    // the whole division takes O(log(a/b)) outer steps (each with an
    // O(log(a/b)) inner doubling loop) -- not O(a/b) like the repeated
    // *single-unit* subtraction this replaced, which needed one
    // allocating iteration per unit of the quotient's value (142857 of
    // them for 1000000 // 7) and could exhaust the heap on an entirely
    // ordinary division.
    PyLongObject* q = _long_alloc(1);
    q->ob_digit[0] = 0;
    q->ob_size = 1;
    PyLongObject* r = abs_a;

    while (_long_abs_compare(r, abs_b) >= 0) {
        PyLongObject* shifted_b = _long_alloc(bsize);
        memcpy(shifted_b->ob_digit, abs_b->ob_digit, bsize * sizeof(digit));
        shifted_b->ob_size = bsize;
        PyLongObject* shifted_q = _long_alloc(1);
        shifted_q->ob_digit[0] = 1;
        shifted_q->ob_size = 1;

        for (;;) {
            PyLongObject* doubled_b = _long_add_magnitudes(shifted_b, shifted_b);
            if (_long_abs_compare(doubled_b, r) > 0) {
                py_free(doubled_b);
                break;
            }
            PyLongObject* doubled_q = _long_add_magnitudes(shifted_q, shifted_q);
            py_free(shifted_b);
            py_free(shifted_q);
            shifted_b = doubled_b;
            shifted_q = doubled_q;
        }

        PyLongObject* new_r = _long_sub_magnitudes(r, shifted_b);
        py_free(r);
        r = new_r;
        PyLongObject* new_q = _long_add_magnitudes(q, shifted_q);
        py_free(q);
        q = new_q;
        py_free(shifted_b);
        py_free(shifted_q);
    }
    // q and r already come out of _long_add_magnitudes/_long_sub_magnitudes
    // normalized -- no extra _long_normalize() call needed (or safe to
    // make redundantly: both take a non-negative size at this point).

    // Apply floor division semantics (Python's sign convention):
    // result has the same sign as the divisor
    if (result_neg && r->ob_size != 0) {
        // r != 0 and result should be negative → adjust
        PyLongObject* one = _long_alloc(1);
        one->ob_digit[0] = 1;
        one->ob_size = 1;
        PyLongObject* new_q = _long_add_magnitudes(q, one);
        py_free(q);
        q = new_q;
        PyLongObject* new_r = _long_sub_magnitudes(abs_b, r);
        py_free(r);
        r = new_r;
        r->ob_size = -r->ob_size;  // negate r
        py_free(one);
    }

    // Set signs
    q->ob_size = result_neg ? -q->ob_size : q->ob_size;
    if (r->ob_size != 0) {
        r->ob_size = a_neg ? -r->ob_size : r->ob_size;
    }

    py_free(abs_a);
    py_free(abs_b);

    *quotient = q;
    *remainder = r;
}

// ---------------------------------------------------------------------------
// Public bignum API
// ---------------------------------------------------------------------------

PyObject* _pyck_long_from_long(int64_t value) {
    if (value == 0) {
        PyLongObject* z = _long_alloc(0);
        return (PyObject*)z;
    }
    int neg = value < 0;
    uint64_t absval = neg ? (uint64_t)(-(value + 1)) + 1 : (uint64_t)value;

    // Count digits needed
    Py_ssize_t ndigits = 0;
    uint64_t tmp = absval;
    do {
        tmp /= PyLong_BASE;
        ndigits++;
    } while (tmp > 0);

    PyLongObject* z = _long_alloc(ndigits);
    tmp = absval;
    for (Py_ssize_t i = 0; i < ndigits; i++) {
        z->ob_digit[i] = (digit)(tmp & PyLong_MASK);
        tmp >>= PyLong_SHIFT;
    }
    // Normalize the non-negative magnitude size *before* applying the
    // sign -- see the identical fix (and explanation) in _long_mul above.
    z->ob_size = ndigits;
    _long_normalize(z);
    if (neg) z->ob_size = -z->ob_size;
    return (PyObject*)z;
}

int64_t _pyck_long_as_long(PyObject* obj) {
    if (obj == _pyck_true) return 1;
    if (obj == _pyck_false) return 0;
    if (obj == _pyck_none) return 0;

    PyLongObject* lo = (PyLongObject*)obj;
    if (lo->ob_base.ob_type != &_pyck_type_int) {
        if (lo->ob_base.ob_type == &_pyck_type_float) {
            return (int64_t)((PyFloatObject*)obj)->ob_fval;
        }
        return 0;
    }
    Py_ssize_t size = lo->ob_size;
    int neg = size < 0;
    Py_ssize_t asize = neg ? -size : size;
    if (asize == 0) return 0;

    uint64_t result = 0;
    for (Py_ssize_t i = asize - 1; i >= 0; i--) {
        result = (result << PyLong_SHIFT) | lo->ob_digit[i];
    }
    if (neg) result = (uint64_t)(-(int64_t)result);
    return (int64_t)result;
}

int _pyck_int_is_negative(PyObject* obj) {
    if (obj->ob_type != &_pyck_type_int) return 0;
    return ((PyLongObject*)obj)->ob_size < 0;
}

PyObject* _pyck_int_to_hex(PyObject* obj) {
    // Extracts the magnitude directly from the bignum's own digit array
    // (already stored unsigned) rather than going through
    // _pyck_long_as_long's signed int64_t -- that used to make hex() of
    // any value in [2^63, 2^64) (e.g. a canonical higher-half address
    // like 0xffff800000000000) raise/misbehave, since such a value has
    // no representation as a *signed* 64-bit integer at all despite
    // fitting perfectly in 64 bits unsigned.
    int neg = _pyck_int_is_negative(obj);
    uint64_t v;
    if (obj == _pyck_true) {
        v = 1;
    } else if (obj == _pyck_false || obj == _pyck_none) {
        v = 0;
    } else {
        PyLongObject* lo = (PyLongObject*)obj;
        Py_ssize_t size = lo->ob_base.ob_type == &_pyck_type_int ? lo->ob_size : 0;
        Py_ssize_t asize = size < 0 ? -size : size;
        v = 0;
        for (Py_ssize_t i = asize - 1; i >= 0; i--) {
            v = (v << PyLong_SHIFT) | lo->ob_digit[i];
        }
    }

    char digits[32];
    int n = 0;
    if (v == 0) {
        digits[n++] = '0';
    } else {
        while (v > 0) {
            int d = (int)(v & 0xF);
            digits[n++] = (d < 10) ? ('0' + d) : ('a' + d - 10);
            v >>= 4;
        }
    }
    char out[40];
    int j = 0;
    if (neg) out[j++] = '-';
    out[j++] = '0';
    out[j++] = 'x';
    for (int k = n - 1; k >= 0; k--) out[j++] = digits[k];
    return _pyck_str_from_cstr(out, j);
}

int _pyck_int_is_zero(PyObject* obj) {
    if (obj == _pyck_true) return 0;
    if (obj == _pyck_false) return 1;
    if (obj == _pyck_none) return 1;
    if (obj->ob_type != &_pyck_type_int) return 1;
    Py_ssize_t size = ((PyLongObject*)obj)->ob_size;
    return size == 0;
}

int _pyck_int_sign(PyObject* obj) {
    if (_pyck_int_is_zero(obj)) return 0;
    return _pyck_int_is_negative(obj) ? -1 : 1;
}

PyObject* _pyck_bignum_add(PyObject* a, PyObject* b) {
    // Handle float operands
    if (a->ob_type == &_pyck_type_float || b->ob_type == &_pyck_type_float) {
        double da = _pyck_float_as_double(a);
        double db = _pyck_float_as_double(b);
        return _pyck_float_from_double(da + db);
    }

    PyLongObject* al = (PyLongObject*)a;
    PyLongObject* bl = (PyLongObject*)b;
    int a_neg = al->ob_size < 0;
    int b_neg = bl->ob_size < 0;

    PyLongObject* result;
    if (a_neg == b_neg) {
        // Same sign: add magnitudes
        result = _long_add_magnitudes(al, bl);
        if (a_neg) result->ob_size = -(result->ob_size);
    } else {
        // Different signs: subtract smaller from larger
        int cmp = _long_abs_compare(al, bl);
        if (cmp == 0) {
            result = _long_alloc(0);
        } else if (cmp > 0) {
            result = _long_sub_magnitudes(al, bl);
            if (a_neg) result->ob_size = -(result->ob_size);
        } else {
            result = _long_sub_magnitudes(bl, al);
            if (b_neg) result->ob_size = -(result->ob_size);
        }
    }
    // No further _long_normalize() call here: each branch above already
    // normalizes the (always non-negative) magnitude *before* applying
    // the sign. Renormalizing afterward -- on a value that may now have
    // a negative ob_size -- silently flips it straight back to positive,
    // the same bug already fixed in _pyck_bignum_neg/_long_mul/
    // _pyck_long_from_long: _long_normalize()'s trim loop guard (`i > 0`)
    // never runs for a negative size, and its sign-reapplying line then
    // reads that same never-decremented negative `i` back out, inverting
    // the sign. Reproduced live: `3 - 5` printed `2`, `0 - 5` printed `5`.
    return (PyObject*)result;
}

PyObject* _pyck_bignum_sub(PyObject* a, PyObject* b) {
    // Handle float operands
    if (a->ob_type == &_pyck_type_float || b->ob_type == &_pyck_type_float) {
        double da = _pyck_float_as_double(a);
        double db = _pyck_float_as_double(b);
        return _pyck_float_from_double(da - db);
    }

    // Negate b (a plain copy with the sign flipped -- already normalized,
    // since it mirrors the digit layout of the already-normalized `b`)
    // and add.
    PyLongObject* bl = (PyLongObject*)b;
    Py_ssize_t bsize = bl->ob_size;
    PyLongObject* neg_b = _long_alloc(bsize < 0 ? -bsize : bsize);
    for (Py_ssize_t i = 0; i < (bsize < 0 ? -bsize : bsize); i++) {
        neg_b->ob_digit[i] = bl->ob_digit[i];
    }
    neg_b->ob_size = -bsize;  // flip sign
    PyObject* result = _pyck_bignum_add(a, (PyObject*)neg_b);
    py_free(neg_b);
    return result;
}

PyObject* _pyck_bignum_pow(PyObject* a, PyObject* b) {
    if (a->ob_type == &_pyck_type_float || b->ob_type == &_pyck_type_float) {
        double da = _pyck_float_as_double(a);
        double db = _pyck_float_as_double(b);
        double result = 1.0;
        for (int64_t i = 0, n = (int64_t)db; i < n; i++) result *= da;
        return _pyck_float_from_double(result);
    }
    // Non-negative integer exponent (v1 subset).
    int64_t exp = _pyck_long_as_long(b);
    PyObject* result = _pyck_long_from_long(1);
    for (int64_t i = 0; i < exp; i++) {
        PyObject* next = _pyck_bignum_mul(result, a);
        py_free(result);
        result = next;
    }
    return result;
}

PyObject* _pyck_bignum_mul(PyObject* a, PyObject* b) {
    if (a->ob_type == &_pyck_type_float || b->ob_type == &_pyck_type_float) {
        double da = _pyck_float_as_double(a);
        double db = _pyck_float_as_double(b);
        return _pyck_float_from_double(da * db);
    }
    PyLongObject* result = _long_mul((PyLongObject*)a, (PyLongObject*)b);
    return (PyObject*)result;
}

PyObject* _pyck_bignum_neg(PyObject* obj) {
    if (obj->ob_type == &_pyck_type_float) {
        return _pyck_float_from_double(-((PyFloatObject*)obj)->ob_fval);
    }
    PyLongObject* lo = (PyLongObject*)obj;
    Py_ssize_t size = lo->ob_size;
    if (size == 0) return obj;
    PyLongObject* z = _long_alloc(size < 0 ? -size : size);
    for (Py_ssize_t i = 0; i < (size < 0 ? -size : size); i++) {
        z->ob_digit[i] = lo->ob_digit[i];
    }
    z->ob_size = -size;  // flip sign
    // NOTE: do not call _long_normalize(z) here -- like the identical bug
    // that used to be in _pyck_bignum_sub, _long_normalize()'s trim loop
    // only works when handed a non-negative size to trim as a magnitude;
    // fed the already-negative size set above, it silently flips the sign
    // straight back to positive. z's digits are already normalized (a
    // plain copy of lo's, which is itself always normalized), so no
    // trimming is needed at all.
    return (PyObject*)z;
}

PyObject* _pyck_bignum_truediv(PyObject* a, PyObject* b) {
    double da = _pyck_float_as_double(a);
    double db = _pyck_float_as_double(b);
    return _pyck_float_from_double(da / db);
}

PyObject* _pyck_bignum_floordiv(PyObject* a, PyObject* b) {
    PyLongObject* q;
    PyLongObject* r;
    _long_divmod((PyLongObject*)a, (PyLongObject*)b, (PyLongObject**)&q, (PyLongObject**)&r);
    py_free(r);
    return (PyObject*)q;
}

PyObject* _pyck_bignum_mod(PyObject* a, PyObject* b) {
    PyLongObject* q;
    PyLongObject* r;
    _long_divmod((PyLongObject*)a, (PyLongObject*)b, (PyLongObject**)&q, (PyLongObject**)&r);
    py_free(q);
    return (PyObject*)r;
}

PyObject* _pyck_bignum_and(PyObject* a, PyObject* b) {
    // For simplicity, convert to long and back
    int64_t av = _pyck_long_as_long(a);
    int64_t bv = _pyck_long_as_long(b);
    return _pyck_long_from_long(av & bv);
}

PyObject* _pyck_bignum_or(PyObject* a, PyObject* b) {
    int64_t av = _pyck_long_as_long(a);
    int64_t bv = _pyck_long_as_long(b);
    return _pyck_long_from_long(av | bv);
}

PyObject* _pyck_bignum_xor(PyObject* a, PyObject* b) {
    int64_t av = _pyck_long_as_long(a);
    int64_t bv = _pyck_long_as_long(b);
    return _pyck_long_from_long(av ^ bv);
}

PyObject* _pyck_bignum_lshift(PyObject* a, PyObject* b) {
    int64_t av = _pyck_long_as_long(a);
    int64_t bv = _pyck_long_as_long(b);
    return _pyck_long_from_long(av << bv);
}

PyObject* _pyck_bignum_rshift(PyObject* a, PyObject* b) {
    int64_t av = _pyck_long_as_long(a);
    int64_t bv = _pyck_long_as_long(b);
    return _pyck_long_from_long(av >> bv);
}

// ---------------------------------------------------------------------------
// Bool
// ---------------------------------------------------------------------------

int _pyck_is_true(PyObject* obj) {
    if (obj == _pyck_true) return 1;
    if (obj == _pyck_false) return 0;
    if (obj == _pyck_none) return 0;
    if (obj->ob_type == &_pyck_type_int) {
        return !_pyck_int_is_zero(obj);
    }
    if (obj->ob_type == &_pyck_type_float) {
        return ((PyFloatObject*)obj)->ob_fval != 0.0;
    }
    return 1;
}

// ---------------------------------------------------------------------------
// Float
// ---------------------------------------------------------------------------

PyObject* _pyck_float_from_double(double value) {
    PyFloatObject* obj = _float_alloc(value);
    return (PyObject*)obj;
}

double _pyck_float_as_double(PyObject* obj) {
    if (obj->ob_type == &_pyck_type_float) {
        return ((PyFloatObject*)obj)->ob_fval;
    }
    return (double)_pyck_long_as_long(obj);
}

PyObject* _pyck_float_add(PyObject* a, PyObject* b) {
    return _pyck_float_from_double(_pyck_float_as_double(a) + _pyck_float_as_double(b));
}

PyObject* _pyck_float_sub(PyObject* a, PyObject* b) {
    return _pyck_float_from_double(_pyck_float_as_double(a) - _pyck_float_as_double(b));
}

PyObject* _pyck_float_mul(PyObject* a, PyObject* b) {
    return _pyck_float_from_double(_pyck_float_as_double(a) * _pyck_float_as_double(b));
}

int _pyck_float_lt(PyObject* a, PyObject* b) {
    return _pyck_float_as_double(a) < _pyck_float_as_double(b);
}

int _pyck_float_lte(PyObject* a, PyObject* b) {
    return _pyck_float_as_double(a) <= _pyck_float_as_double(b);
}

int _pyck_float_gt(PyObject* a, PyObject* b) {
    return _pyck_float_as_double(a) > _pyck_float_as_double(b);
}

int _pyck_float_gte(PyObject* a, PyObject* b) {
    return _pyck_float_as_double(a) >= _pyck_float_as_double(b);
}

int _pyck_float_eq(PyObject* a, PyObject* b) {
    return _pyck_float_as_double(a) == _pyck_float_as_double(b);
}

int _pyck_float_neq(PyObject* a, PyObject* b) {
    return _pyck_float_as_double(a) != _pyck_float_as_double(b);
}

// ---------------------------------------------------------------------------
// Tuple (for print() args)
// ---------------------------------------------------------------------------

PyObject* _pyck_tuple_new(Py_ssize_t size) {
    size_t alloc_size = sizeof(PyTupleObject) + (size > 0 ? (size - 1) * sizeof(PyObject*) : 0);
    PyTupleObject* obj = (PyTupleObject*)py_alloc(alloc_size);
    obj->ob_base.ob_refcnt = 1;
    obj->ob_base.ob_type = &_pyck_type_int;  // reuse int type for tuples (hack)
    obj->ob_size = size;
    for (Py_ssize_t i = 0; i < size; i++) {
        obj->ob_item[i] = nullptr;
    }
    return (PyObject*)obj;
}

void _pyck_tuple_set(PyObject* tuple, Py_ssize_t index, PyObject* item) {
    PyTupleObject* t = (PyTupleObject*)tuple;
    if (index >= 0 && index < t->ob_size) {
        t->ob_item[index] = item;
    }
}

PyObject* _pyck_tuple_get(PyObject* tuple, Py_ssize_t index) {
    PyTupleObject* t = (PyTupleObject*)tuple;
    if (index >= 0 && index < t->ob_size) {
        return t->ob_item[index];
    }
    return nullptr;
}

Py_ssize_t _pyck_tuple_size(PyObject* tuple) {
    return ((PyTupleObject*)tuple)->ob_size;
}

void _pyck_tuple_decref(PyObject* tuple) {
    if (--tuple->ob_refcnt == 0) {
        py_free(tuple);
    }
}

// ---------------------------------------------------------------------------
// print() — default freestanding implementation (overridden by arch kit)
// ---------------------------------------------------------------------------

__attribute__((weak))
void py_print(PyObject* args, PyObject* sep, PyObject* end) {
    // Default: no-op (arch kit should override)
    // The arch kit's py_print should format numbers to the display.
    (void)args; (void)sep; (void)end;
}

__attribute__((weak))
void _pyck_print_cstr(const char* str) {
    // Default: no-op (arch kit should override)
    (void)str;
}

// ---------------------------------------------------------------------------
// raw_memzero()/raw_memcpy() -- bulk versions of poke8/peek8+poke8 (see
// pyck/typechecker.py's own comment on these). A PyCk while-loop doing
// the same work one byte per iteration is fine for something the size
// of an IDT entry, but not a several-hundred-KB ELF segment: this
// runtime's PyObject heap never frees (see arch/.../support.cpp's
// py_free()), so a million-plus boxed loop-counter/address allocations
// exhausts it long before such a loop finishes. These do the whole
// operation in a single call instead, with zero PyCk-level iteration.
// ---------------------------------------------------------------------------

extern "C" void _pyck_raw_memzero(unsigned long addr, unsigned long count) {
    memset((void*)addr, 0, (size_t)count);
}

extern "C" void _pyck_raw_memcpy(unsigned long dst, unsigned long src, unsigned long count) {
    memcpy((void*)dst, (void*)src, (size_t)count);
}
