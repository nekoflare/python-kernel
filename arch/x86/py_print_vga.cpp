/* Serial + VGA py_print for PyCk freestanding x86 kernel.
 * Writes output to both VGA (0xB8000) and serial port COM1 (0x3F8).
 * Serial output is visible in QEMU's -nographic mode.
 */
#include "../../runtime/freestanding/pyck_runtime.h"
#include <stdint.h>

/* VGA text-mode memory */
#define VGA_BUFFER ((volatile uint16_t*)0xB8000)
#define VGA_WIDTH  80
#define VGA_HEIGHT 25
#define VGA_COLOR  0x07

static int vga_row = 0;
static int vga_col = 0;

/* Serial port (COM1) */
#define SERIAL_PORT 0x3F8

/* x86 port I/O (defined in support.cpp) */
extern "C" uint8_t inb(uint16_t port);
extern "C" void outb(uint16_t port, uint8_t val);

static inline void _serial_write(char c) {
    /* Wait for transmit buffer empty */
    while ((inb(SERIAL_PORT + 5) & 0x20) == 0) {}
    outb(SERIAL_PORT, c);
}

static void _serial_string(const char* str) {
    while (*str) {
        _serial_write(*str);
        str++;
    }
}

static void _serial_long(int64_t value) {
    char buf[32];
    int i = 0;
    int neg = 0;
    if (value < 0) { neg = 1; value = -value; }
    if (value == 0) { buf[i++] = '0'; }
    else { while (value > 0) { buf[i++] = '0' + (value % 10); value /= 10; } }
    if (neg) buf[i++] = '-';
    for (int j = i - 1; j >= 0; j--) _serial_write(buf[j]);
}

/* VGA output helpers */
static void _vga_char(char c) {
    if (c == '\n') {
        vga_row++;
        vga_col = 0;
        if (vga_row >= VGA_HEIGHT) vga_row = 0;
        return;
    }
    int offset = vga_row * VGA_WIDTH + vga_col;
    VGA_BUFFER[offset] = (uint16_t)c | ((uint16_t)VGA_COLOR << 8);
    vga_col++;
    if (vga_col >= VGA_WIDTH) {
        vga_col = 0;
        vga_row++;
        if (vga_row >= VGA_HEIGHT) vga_row = 0;
    }
}

static void _vga_string(const char* str) {
    while (*str) { _vga_char(*str); str++; }
}

static void _vga_long(int64_t value) {
    char buf[32];
    int i = 0;
    int neg = 0;
    if (value < 0) { neg = 1; value = -value; }
    if (value == 0) { buf[i++] = '0'; }
    else { while (value > 0) { buf[i++] = '0' + (value % 10); value /= 10; } }
    if (neg) buf[i++] = '-';
    for (int j = i - 1; j >= 0; j--) _vga_char(buf[j]);
}

/* Initialize serial port COM1 */
static void _serial_init(void) {
    outb(SERIAL_PORT + 1, 0x00);  /* Disable interrupts */
    outb(SERIAL_PORT + 3, 0x80);  /* Enable DLAB */
    outb(SERIAL_PORT + 0, 0x03);  /* Set divisor lo (38400 baud) */
    outb(SERIAL_PORT + 1, 0x00);  /* Set divisor hi */
    outb(SERIAL_PORT + 3, 0x03);  /* 8 bits, no parity, one stop */
    outb(SERIAL_PORT + 2, 0xC7);  /* Enable FIFO */
    outb(SERIAL_PORT + 4, 0x0B);  /* IRQs enabled, RTS/DSR set */
}

static bool _serial_inited = false;

static void _ensure_serial(void) {
    if (!_serial_inited) {
        _serial_init();
        _serial_inited = true;
    }
}

/* Print a PyObject to both serial and VGA */
static void _print_item(PyObject* item) {
    if (item->ob_type == &_pyck_type_int) {
        int64_t v = _pyck_long_as_long(item);
        _serial_long(v);
        _vga_long(v);
    } else if (item->ob_type == &_pyck_type_float) {
        int64_t v = (int64_t)_pyck_float_as_double(item);
        _serial_long(v);
        _vga_long(v);
    } else if (item == _pyck_true) {
        _serial_string("True");
        _vga_string("True");
    } else if (item == _pyck_false) {
        _serial_string("False");
        _vga_string("False");
    } else if (item == _pyck_none) {
        _serial_string("None");
        _vga_string("None");
    } else if (item->ob_type == &_pyck_type_str) {
        Py_ssize_t n = _pyck_str_length(item);
        const char* data = _pyck_str_data(item);
        for (Py_ssize_t i = 0; i < n; i++) {
            _serial_write(data[i]);
            _vga_char(data[i]);
        }
    } else {
        /* Generic fallback for any type without dedicated print support
         * (e.g. a class instance): renders as "<TypeName object>" using
         * the type's own tp_name, same idea as CPython's default repr. */
        _serial_string("<");
        _serial_string(item->ob_type->tp_name);
        _serial_string(" object>");
        _vga_string("<");
        _vga_string(item->ob_type->tp_name);
        _vga_string(" object>");
    }
}

/* py_print: writes to both VGA and serial */
extern "C" void py_print(PyObject* args, PyObject* sep, PyObject* end) {
    _ensure_serial();
    Py_ssize_t nargs = _pyck_tuple_size(args);
    for (Py_ssize_t i = 0; i < nargs; i++) {
        if (i > 0) { _serial_write(' '); _vga_char(' '); }
        PyObject* item = _pyck_tuple_get(args, i);
        if (item) _print_item(item);
    }
    _serial_write('\n');
    _vga_char('\n');
}

/* _pyck_print_cstr: print a C string literal to both serial and VGA */
extern "C" void _pyck_print_cstr(const char* str) {
    _ensure_serial();
    _serial_string(str);
    _vga_string(str);
}
