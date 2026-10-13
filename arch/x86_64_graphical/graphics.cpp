/* Graphical framebuffer console for PythonOS, plus serial (COM1) output
 * kept alongside it for logging/automated verification.
 *
 * Draws text with the public-domain font8x8_basic bitmap font (see
 * font8x8_basic.h) onto the linear framebuffer Limine hands us. Understands
 * a real (if bounded) subset of ANSI/VT100: 16-color and 24-bit truecolor
 * SGR (foreground *and* background, bold-brightens standard colors,
 * reverse video), absolute/relative cursor addressing (CUP/HVP/CUU/CUD/
 * CUF/CUB), and erase-display/erase-line (ED/EL) -- see the "--- ANSI ---"
 * section below for the actual command set. This is what lets a real
 * full-screen program (busybox vi, less, ...) draw and redraw a screen in
 * place instead of only ever scrolling new lines. Private-mode CSI
 * sequences (leading '?' -- cursor visibility, alternate screen buffer,
 * bracketed paste, ...) are recognized enough to consume without printing
 * garbage, but not actually implemented: harmless no-ops.
 *
 * PyCk source builds these escape sequences itself (see
 * PythonOS/kernel/satanic.py's print_satanic()), there is no special
 * compiler support for color at all. Serial output needs no equivalent
 * parser: raw escape bytes just pass through _serial_write() unchanged,
 * and any real terminal watching that serial line already interprets
 * ANSI escapes natively.
 *
 * Scrolling (when the cursor runs off the bottom row) still just shifts
 * every row up by one and clears the newly exposed bottom row, rather
 * than wrapping back to the top and blanking everything.
 */
#include "../../runtime/freestanding/pyck_runtime.h"
#include "limine.h"
#include "font8x8_basic.h"
#include <stdint.h>

#define SERIAL_PORT 0x3F8
/* QEMU's "isa-debugcon" device: any byte written here just gets echoed
 * to wherever `-debugcon` points (see PythonOS/build.py), no UART
 * programming or IRQs involved -- a second, dead-simple logging path
 * alongside the real COM1 serial port. Not a real piece of hardware;
 * writes to it on real silicon are just ignored (unmapped I/O port). */
#define DEBUGCON_PORT 0xE9
#define GLYPH_SIZE 8
#define SCALE 1  /* native 8x8 glyph size -- scaled down 2x from the previous 2x upscale */
#define CELL_W (GLYPH_SIZE * SCALE)
#define CELL_H (GLYPH_SIZE * SCALE)

extern "C" uint8_t inb(uint16_t port);
extern "C" void outb(uint16_t port, uint8_t val);
/* Defined in support.cpp; declared here since freestanding builds have
 * no <cstring> to pull the prototype from. */
extern "C" void* memset(void* dest, int ch, size_t count);
extern "C" void* memcpy(void* dest, const void* src, size_t count);

/* --- Serial --- */

static inline void _serial_write(char c) {
    while ((inb(SERIAL_PORT + 5) & 0x20) == 0) {}
    outb(SERIAL_PORT, c);
    outb(DEBUGCON_PORT, (uint8_t)c);
}

static void _serial_string(const char* str) {
    while (*str) { _serial_write(*str); str++; }
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

static void _serial_init(void) {
    outb(SERIAL_PORT + 1, 0x00);
    outb(SERIAL_PORT + 3, 0x80);
    outb(SERIAL_PORT + 0, 0x03);
    outb(SERIAL_PORT + 1, 0x00);
    outb(SERIAL_PORT + 3, 0x03);
    outb(SERIAL_PORT + 2, 0xC7);
    outb(SERIAL_PORT + 4, 0x0B);
}

/* --- Graphical console --- */

static uint32_t* fb_addr = nullptr;
static uint64_t fb_width = 0;
static uint64_t fb_height = 0;
static uint64_t fb_pitch_px = 0;  /* pitch in pixels, not bytes */
static uint64_t console_cols = 0;
static uint64_t console_rows = 0;
static uint64_t cursor_col = 0;
static uint64_t cursor_row = 0;

/* Real firmware maps the linear framebuffer uncacheable (or at best
 * write-combining) -- unlike QEMU, which is just backing it with plain
 * host RAM, so on real silicon every *read* from fb_addr is a blocking
 * round trip with no speculation or prefetch, hundreds of cycles each.
 * scroll_up() used to memcpy() straight from fb_addr to fb_addr, which
 * is fine on QEMU and catastrophic on real hardware -- exactly why
 * scrolling was "extremely slow" there specifically.
 *
 * The fix is the standard one: never read the framebuffer back. Mirror
 * each cell's character + foreground color in ordinary (cacheable) RAM,
 * shift *that* on scroll, and only ever write to fb_addr. Sized for up
 * to 4096x2304 at the 8x8 native glyph size (CELL_W/CELL_H == 8); a
 * higher real resolution just clamps the usable text grid down to this
 * cap in graphics_init() rather than overflowing it. */
#define MAX_CONSOLE_COLS 512
#define MAX_CONSOLE_ROWS 288
static char shadow_char[MAX_CONSOLE_ROWS * MAX_CONSOLE_COLS];
static uint32_t shadow_fg[MAX_CONSOLE_ROWS * MAX_CONSOLE_COLS];
static uint32_t shadow_bg[MAX_CONSOLE_ROWS * MAX_CONSOLE_COLS];

static const uint32_t COLOR_BG = 0x00000000;
static const uint32_t COLOR_FG_DEFAULT = 0x00FFFFFF;
/* current_fg/current_bg are what actually gets drawn/recorded per cell --
 * derived from the "logical" SGR state (sgr_fg/sgr_bg/ansi_bold/
 * ansi_reverse below) by _ansi_recompute_colors() every time any of that
 * state changes, never written to directly anywhere else. */
static uint32_t current_fg = COLOR_FG_DEFAULT;
static uint32_t current_bg = COLOR_BG;

/* Forward declaration: both the ANSI erase commands (ED/EL, further down)
 * and scroll_up() need to re-raster an already-recorded cell out of the
 * shadow grid, write-only, without going through graphics_putchar()'s
 * cursor/newline handling. Defined for real down by grainy_rand(), which
 * it needs. */
static void draw_glyph_fg(uint64_t col, uint64_t row, char c, uint32_t fg, uint32_t bg);

/* --- ANSI ---
 *
 * A small CSI ("\x1b[...") state machine: ESC then '[' opens a sequence,
 * digits/';' accumulate 0 or more numeric parameters, and any byte in the
 * 0x40-0x7E "final byte" range both ends it and says what kind of command
 * it is (SGR color/attributes on 'm', cursor moves on 'A'/'B'/'C'/'D',
 * absolute positioning on 'H'/'f', erase on 'J'/'K'). A leading '?' right
 * after '[' marks a private-mode sequence (DEC cursor visibility,
 * alternate screen buffer, bracketed paste, ...) -- recognized just well
 * enough to consume silently rather than print its parameters as garbage
 * text, not actually implemented. Anything that stops looking like a
 * sequence partway through (an ESC not followed by '[', or a byte that's
 * neither a digit/';'/'?' nor a final byte) just resets the parser and
 * resumes normal character drawing from wherever it left off.
 *
 * graphics_putchar() feeds every character through this one at a time (it
 * has no way to look ahead), so all of this accumulates state across
 * calls until a final byte completes it.
 */
enum class AnsiState { NORMAL, ESC, PARAMS };
static AnsiState ansi_state = AnsiState::NORMAL;
static int ansi_params[8];
static int ansi_param_count = 0;
static int ansi_current_param = 0;
static bool ansi_have_digit = false;
static bool ansi_private = false;

static void ansi_reset(void) {
    ansi_state = AnsiState::NORMAL;
    ansi_param_count = 0;
    ansi_current_param = 0;
    ansi_have_digit = false;
    ansi_private = false;
}

static void ansi_push_param(void) {
    if (ansi_param_count < 8) {
        ansi_params[ansi_param_count++] = ansi_have_digit ? ansi_current_param : 0;
    }
    ansi_current_param = 0;
    ansi_have_digit = false;
}

/* Not a real SGR code -- 666 is this console's own private one, since
 * nothing in the actual standard means "make the glyphs grainy and
 * cursed". Same PyCk-side mechanism as truecolor (see satanic.py):
 * source just writes the raw escape bytes, no compiler support
 * involved.
 *
 * Defaults off: it made everything printed materially harder to read,
 * which stopped being a fun tradeoff once real interactive programs
 * (see PythonOS/kernel/ps2.py) started actually needing their output
 * read. \x1b[666m still turns it on for whoever wants the cursed look
 * back; it deliberately does *not* live in the p==0 (reset) branch
 * below -- print_satanic()'s own trailing \x1b[0m (used for nearly
 * every line) would otherwise turn it back off after the very first
 * thing printed once turned on, since color-reset and grain used to be
 * the same switch. There's currently no escape code that turns it back
 * off once enabled. */
static bool grainy_mode = false;

/* Standard 16-color ANSI palette (classic VGA-ish values), indexed 0-7 --
 * used for SGR 30-37/40-47 (normal intensity) and 90-97/100-107 (bright).
 * Bold (SGR 1) upgrades a *foreground* 30-37 to its bright equivalent at
 * the moment the color code is applied, matching how real 16-color
 * terminals without distinct bold glyphs rendered "bold" in the first
 * place -- it does not retroactively re-brighten a color already set
 * before the bold flag was turned on, only ones set after. Background
 * colors are never brightened by bold; only 100-107 (or truecolor) makes
 * a background bright, matching real terminal convention. */
static const uint32_t ansi_palette_normal[8] = {
    0x000000, 0xAA0000, 0x00AA00, 0xAA5500,
    0x0000AA, 0xAA00AA, 0x00AAAA, 0xAAAAAA,
};
static const uint32_t ansi_palette_bright[8] = {
    0x555555, 0xFF5555, 0x55FF55, 0xFFFF55,
    0x5555FF, 0xFF55FF, 0x55FFFF, 0xFFFFFF,
};

/* "Logical" SGR-requested colors, before reverse video (SGR 7) is
 * applied -- current_fg/current_bg (the ones actually drawn) are derived
 * from these plus ansi_reverse by _ansi_recompute_colors(). Keeping them
 * separate means turning reverse off restores whatever the program had
 * actually asked for, rather than whatever got drawn most recently. */
static uint32_t sgr_fg = COLOR_FG_DEFAULT;
static uint32_t sgr_bg = COLOR_BG;
static bool ansi_bold = false;
static bool ansi_reverse = false;

static void _ansi_recompute_colors(void) {
    if (ansi_reverse) {
        current_fg = sgr_bg;
        current_bg = sgr_fg;
    } else {
        current_fg = sgr_fg;
        current_bg = sgr_bg;
    }
}

static void ansi_apply_sgr(void) {
    int i = 0;
    while (i < ansi_param_count) {
        int p = ansi_params[i];
        if (p == 0) {
            sgr_fg = COLOR_FG_DEFAULT;
            sgr_bg = COLOR_BG;
            ansi_bold = false;
            ansi_reverse = false;
            i++;
        } else if (p == 1) {
            ansi_bold = true;
            i++;
        } else if (p == 22) {
            ansi_bold = false;
            i++;
        } else if (p == 7) {
            ansi_reverse = true;
            i++;
        } else if (p == 27) {
            ansi_reverse = false;
            i++;
        } else if (p == 39) {
            sgr_fg = COLOR_FG_DEFAULT;
            i++;
        } else if (p == 49) {
            sgr_bg = COLOR_BG;
            i++;
        } else if (p >= 30 && p <= 37) {
            sgr_fg = ansi_bold ? ansi_palette_bright[p - 30] : ansi_palette_normal[p - 30];
            i++;
        } else if (p >= 90 && p <= 97) {
            sgr_fg = ansi_palette_bright[p - 90];
            i++;
        } else if (p >= 40 && p <= 47) {
            sgr_bg = ansi_palette_normal[p - 40];
            i++;
        } else if (p >= 100 && p <= 107) {
            sgr_bg = ansi_palette_bright[p - 100];
            i++;
        } else if (p == 38 && i + 4 < ansi_param_count && ansi_params[i + 1] == 2) {
            uint32_t r = (uint32_t)ansi_params[i + 2] & 0xFF;
            uint32_t g = (uint32_t)ansi_params[i + 3] & 0xFF;
            uint32_t b = (uint32_t)ansi_params[i + 4] & 0xFF;
            sgr_fg = (r << 16) | (g << 8) | b;
            i += 5;
        } else if (p == 48 && i + 4 < ansi_param_count && ansi_params[i + 1] == 2) {
            uint32_t r = (uint32_t)ansi_params[i + 2] & 0xFF;
            uint32_t g = (uint32_t)ansi_params[i + 3] & 0xFF;
            uint32_t b = (uint32_t)ansi_params[i + 4] & 0xFF;
            sgr_bg = (r << 16) | (g << 8) | b;
            i += 5;
        } else if (p == 666) {
            grainy_mode = true;
            i++;
        } else {
            /* Recognized as a parameter, not as anything this console
             * renders differently -- 2/4/24 (dim/underline/no-underline)
             * land here: accepted so they don't garble the parser, but
             * this font has no distinct dim or underlined rendering. */
            i++;
        }
    }
    _ansi_recompute_colors();
}

static void _erase_cell(uint64_t col, uint64_t row) {
    /* Draws a blank space with the *current* colors (so an erased area
     * takes on whatever background is currently active, same as a real
     * terminal) and records it into the shadow grid exactly like
     * draw_glyph() does, without moving the cursor -- ED/EL erase
     * without otherwise disturbing where the next character goes. */
    if (row < console_rows && col < console_cols) {
        uint64_t cell = row * console_cols + col;
        shadow_char[cell] = ' ';
        shadow_fg[cell] = current_fg;
        shadow_bg[cell] = current_bg;
    }
    draw_glyph_fg(col, row, ' ', current_fg, current_bg);
}

static void ansi_erase_line(int mode) {
    uint64_t start = 0;
    uint64_t end = console_cols;
    if (mode == 0) {
        start = cursor_col;
    } else if (mode == 1) {
        end = cursor_col + 1;
        if (end > console_cols) end = console_cols;
    }
    /* mode == 2 (or anything unrecognized): whole line, start/end above
     * already cover it. */
    for (uint64_t c = start; c < end; c++) _erase_cell(c, cursor_row);
}

static void ansi_erase_display(int mode) {
    if (mode == 0) {
        /* Cursor to end of screen. */
        ansi_erase_line(0);
        for (uint64_t r = cursor_row + 1; r < console_rows; r++) {
            for (uint64_t c = 0; c < console_cols; c++) _erase_cell(c, r);
        }
    } else if (mode == 1) {
        /* Start of screen to cursor. */
        for (uint64_t r = 0; r < cursor_row; r++) {
            for (uint64_t c = 0; c < console_cols; c++) _erase_cell(c, r);
        }
        ansi_erase_line(1);
    } else {
        /* Entire screen (mode 2, or 3 -- no real scrollback buffer for
         * 3's "and scrollback" to also clear, so it behaves like 2). */
        for (uint64_t r = 0; r < console_rows; r++) {
            for (uint64_t c = 0; c < console_cols; c++) _erase_cell(c, r);
        }
    }
}

/* Dispatches one completed CSI sequence (params already accumulated in
 * ansi_params[0..ansi_param_count)) by its final byte. Cursor moves
 * default their count to 1 when no parameter (or a 0) was given, per the
 * real ANSI spec; cursor positioning defaults row/col to 1 (top-left)
 * the same way, and clamps into the actual screen instead of trusting an
 * out-of-range request. */
static void ansi_dispatch_csi(char final_byte) {
    if (ansi_private) {
        /* Private-mode sequence (DEC cursor show/hide, alternate screen,
         * bracketed paste, ...) -- see this section's own top comment:
         * consumed, not implemented. */
        return;
    }
    if (final_byte == 'm') {
        ansi_apply_sgr();
        return;
    }
    if (final_byte == 'H' || final_byte == 'f') {
        int row = (ansi_param_count > 0 && ansi_params[0] > 0) ? ansi_params[0] : 1;
        int col = (ansi_param_count > 1 && ansi_params[1] > 0) ? ansi_params[1] : 1;
        uint64_t r = (uint64_t)(row - 1);
        uint64_t c = (uint64_t)(col - 1);
        if (r >= console_rows) r = console_rows - 1;
        if (c >= console_cols) c = console_cols - 1;
        cursor_row = r;
        cursor_col = c;
        return;
    }
    if (final_byte == 'A') {
        uint64_t n = (ansi_param_count > 0 && ansi_params[0] > 0) ? (uint64_t)ansi_params[0] : 1;
        cursor_row = (n > cursor_row) ? 0 : cursor_row - n;
        return;
    }
    if (final_byte == 'B') {
        uint64_t n = (ansi_param_count > 0 && ansi_params[0] > 0) ? (uint64_t)ansi_params[0] : 1;
        cursor_row += n;
        if (cursor_row >= console_rows) cursor_row = console_rows - 1;
        return;
    }
    if (final_byte == 'C') {
        uint64_t n = (ansi_param_count > 0 && ansi_params[0] > 0) ? (uint64_t)ansi_params[0] : 1;
        cursor_col += n;
        if (cursor_col >= console_cols) cursor_col = console_cols - 1;
        return;
    }
    if (final_byte == 'D') {
        uint64_t n = (ansi_param_count > 0 && ansi_params[0] > 0) ? (uint64_t)ansi_params[0] : 1;
        cursor_col = (n > cursor_col) ? 0 : cursor_col - n;
        return;
    }
    if (final_byte == 'J') {
        ansi_erase_display(ansi_param_count > 0 ? ansi_params[0] : 0);
        return;
    }
    if (final_byte == 'K') {
        ansi_erase_line(ansi_param_count > 0 ? ansi_params[0] : 0);
        return;
    }
    /* Anything else that completed a well-formed CSI sequence (cursor
     * save/restore, scroll region, device status report, ...): consumed
     * silently, same "don't print it as garbage, don't implement it
     * either" treatment as a private-mode sequence. */
}

/* Returns true if `c` was consumed as part of an (in-progress or just
 * completed) escape sequence -- the caller should not draw it or
 * advance the cursor for it. */
static bool ansi_feed(char c) {
    if (ansi_state == AnsiState::NORMAL) {
        if (c == '\x1b') {
            ansi_state = AnsiState::ESC;
            return true;
        }
        return false;
    }
    if (ansi_state == AnsiState::ESC) {
        if (c == '[') {
            ansi_state = AnsiState::PARAMS;
            ansi_param_count = 0;
            ansi_current_param = 0;
            ansi_have_digit = false;
            ansi_private = false;
        } else {
            ansi_reset();
        }
        return true;
    }
    /* AnsiState::PARAMS */
    if (c == '?' && ansi_param_count == 0 && !ansi_have_digit) {
        ansi_private = true;
        return true;
    }
    if (c >= '0' && c <= '9') {
        ansi_current_param = ansi_current_param * 10 + (c - '0');
        ansi_have_digit = true;
        return true;
    }
    if (c == ';') {
        ansi_push_param();
        return true;
    }
    if (c >= 0x40 && c <= 0x7E) {
        ansi_push_param();
        ansi_dispatch_csi(c);
        ansi_reset();
        return true;
    }
    ansi_reset();
    return true;
}

static void clear_shadow(void) {
    uint64_t i = 0;
    uint64_t n = console_rows * console_cols;
    while (i < n) {
        shadow_char[i] = ' ';
        shadow_fg[i] = COLOR_FG_DEFAULT;
        shadow_bg[i] = COLOR_BG;
        i++;
    }
}

static void clear_screen(void) {
    /* One memset per row (each row is contiguous in the framebuffer)
     * instead of a per-pixel store with its own y*pitch+x multiply.
     * A one-time boot cost either way, and unlike scroll_up() below this
     * never needs to read fb_addr back, so it's cheap even on real
     * hardware's uncached framebuffer. */
    uint64_t row_bytes = fb_pitch_px * sizeof(uint32_t);
    for (uint64_t y = 0; y < fb_height; y++) {
        memset(&fb_addr[y * fb_pitch_px], 0, row_bytes);
    }
    clear_shadow();
}

static void scroll_up(void) {
    /* Shift the console's text area up by one text row. This only ever
     * touches shadow_char/shadow_fg/shadow_bg (ordinary cacheable RAM, so
     * the memmove-style shift is cheap) and then re-rasters every cell
     * from that shadow into fb_addr -- write-only, never reading fb_addr
     * back. See the shadow_char/shadow_fg comment above for why: on real
     * hardware the framebuffer is typically uncacheable, and reading it
     * back (the previous memcpy-from-fb approach) is what made scrolling
     * "extremely slow" there specifically, even though QEMU (plain host
     * RAM underneath) never showed the problem. */
    uint64_t row = 0;
    while (row < console_rows - 1) {
        uint64_t dst = row * console_cols;
        uint64_t src = (row + 1) * console_cols;
        memcpy(&shadow_char[dst], &shadow_char[src], console_cols * sizeof(char));
        memcpy(&shadow_fg[dst], &shadow_fg[src], console_cols * sizeof(uint32_t));
        memcpy(&shadow_bg[dst], &shadow_bg[src], console_cols * sizeof(uint32_t));
        row++;
    }
    uint64_t last = (console_rows - 1) * console_cols;
    uint64_t col = 0;
    while (col < console_cols) {
        shadow_char[last + col] = ' ';
        shadow_fg[last + col] = COLOR_FG_DEFAULT;
        shadow_bg[last + col] = COLOR_BG;
        col++;
    }
    row = 0;
    while (row < console_rows) {
        col = 0;
        while (col < console_cols) {
            uint64_t cell = row * console_cols + col;
            draw_glyph_fg(col, row, shadow_char[cell], shadow_fg[cell], shadow_bg[cell]);
            col++;
        }
        row++;
    }
}

/* xorshift32 -- cheap, deterministic (no real entropy source exists
 * this early/freestanding anyway), plenty good enough for cosmetic
 * static. Nonzero seed: xorshift is fixed-point at 0 forever otherwise. */
static uint32_t grainy_rng_state = 0x68656c6cu;

static inline uint32_t grainy_rand(void) {
    uint32_t x = grainy_rng_state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    grainy_rng_state = x;
    return x;
}

/* Pure raster: writes glyph pixels into fb_addr only, never reads it,
 * and never touches shadow_char/shadow_fg/shadow_bg -- used both for a
 * freshly typed character (via draw_glyph() below), for re-rastering an
 * already-recorded cell out of the shadow grid during scroll_up(), and
 * for an ANSI erase (_erase_cell(), which draws a blank space with
 * whatever the current colors are). */
static void draw_glyph_fg(uint64_t col, uint64_t row, char c, uint32_t fg, uint32_t bg) {
    unsigned char ch = (unsigned char)c;
    if (ch > 127) ch = '?';
    const uint8_t* glyph = (const uint8_t*)font8x8_basic[ch];
    uint64_t ox = col * CELL_W;
    uint64_t oy = row * CELL_H;
    /* One row-base pointer per (glyph scanline, scale row) pair, not
     * a fresh y*pitch+x multiply for every single pixel -- this runs
     * for every character any syscall ever prints, directly inside the
     * SYS_WRITE handler, so it's userspace's own wait time. */
    for (int gy = 0; gy < GLYPH_SIZE; gy++) {
        uint8_t bits = glyph[gy];
        for (int sy = 0; sy < SCALE; sy++) {
            uint32_t* row_ptr = &fb_addr[(oy + gy * SCALE + sy) * fb_pitch_px + ox];
            for (int gx = 0; gx < GLYPH_SIZE; gx++) {
                bool set = (bits & (1 << gx)) != 0;
                if (grainy_mode) {
                    /* Corrode the clean bitmap: drop some of the
                     * glyph's own pixels and sprinkle stray specks into
                     * what should be background, both at the same 1 in
                     * 24 rate. Not a power of 2, so a bitmask can't
                     * give this exact fraction -- modulo can, at the
                     * cost of a divide. */
                    uint32_t r = grainy_rand();
                    set = set ? ((r % 24) != 0) : ((r % 24) == 0);
                }
                uint32_t color = set ? fg : bg;
                for (int sx = 0; sx < SCALE; sx++) {
                    row_ptr[gx * SCALE + sx] = color;
                }
            }
        }
    }
}

static void draw_glyph(uint64_t col, uint64_t row, char c) {
    if (row < console_rows && col < console_cols) {
        uint64_t cell = row * console_cols + col;
        shadow_char[cell] = c;
        shadow_fg[cell] = current_fg;
        shadow_bg[cell] = current_bg;
    }
    draw_glyph_fg(col, row, c, current_fg, current_bg);
}

static void graphics_putchar(char c) {
    if (fb_addr == nullptr) return;
    if (ansi_feed(c)) return;

    if (c == '\n') {
        cursor_row++;
        cursor_col = 0;
    } else {
        draw_glyph(cursor_col, cursor_row, c);
        cursor_col++;
        if (cursor_col >= console_cols) {
            cursor_col = 0;
            cursor_row++;
        }
    }
    if (cursor_row >= console_rows) {
        scroll_up();
        cursor_row = console_rows - 1;
    }
}

static void graphics_string(const char* str) {
    while (*str) { graphics_putchar(*str); str++; }
}

static void graphics_long(int64_t value) {
    char buf[32];
    int i = 0;
    int neg = 0;
    if (value < 0) { neg = 1; value = -value; }
    if (value == 0) { buf[i++] = '0'; }
    else { while (value > 0) { buf[i++] = '0' + (value % 10); value /= 10; } }
    if (neg) buf[i++] = '-';
    for (int j = i - 1; j >= 0; j--) graphics_putchar(buf[j]);
}

extern "C" void graphics_init(struct limine_framebuffer* fb) {
    _serial_init();

    if (fb != nullptr && fb->bpp == 32) {
        fb_addr = (uint32_t*)fb->address;
        fb_width = fb->width;
        fb_height = fb->height;
        fb_pitch_px = fb->pitch / 4;
        console_cols = fb_width / CELL_W;
        console_rows = fb_height / CELL_H;
        /* Clamp to the shadow grid's static capacity (see
         * shadow_char/shadow_fg above) -- a real display beyond ~4K
         * just loses the outer strip of the screen to unused
         * framebuffer, same as the existing sub-CELL_H leftover strip. */
        if (console_cols > MAX_CONSOLE_COLS) console_cols = MAX_CONSOLE_COLS;
        if (console_rows > MAX_CONSOLE_ROWS) console_rows = MAX_CONSOLE_ROWS;
        clear_screen();
    }
}

/* --- Print a PyObject to both serial and the graphical console --- */

static void _print_item(PyObject* item) {
    if (item->ob_type == &_pyck_type_int) {
        int64_t v = _pyck_long_as_long(item);
        _serial_long(v);
        graphics_long(v);
    } else if (item->ob_type == &_pyck_type_float) {
        int64_t v = (int64_t)_pyck_float_as_double(item);
        _serial_long(v);
        graphics_long(v);
    } else if (item == _pyck_true) {
        _serial_string("True");
        graphics_string("True");
    } else if (item == _pyck_false) {
        _serial_string("False");
        graphics_string("False");
    } else if (item == _pyck_none) {
        _serial_string("None");
        graphics_string("None");
    } else if (item->ob_type == &_pyck_type_str) {
        Py_ssize_t n = _pyck_str_length(item);
        const char* data = _pyck_str_data(item);
        for (Py_ssize_t i = 0; i < n; i++) {
            _serial_write(data[i]);
            graphics_putchar(data[i]);
        }
    } else {
        /* Generic fallback for any type without dedicated print support
         * (e.g. a class instance): renders as "<TypeName object>" using
         * the type's own tp_name, same idea as CPython's default repr. */
        _serial_string("<");
        _serial_string(item->ob_type->tp_name);
        _serial_string(" object>");
        graphics_string("<");
        graphics_string(item->ob_type->tp_name);
        graphics_string(" object>");
    }
}

extern "C" void py_print(PyObject* args, PyObject* sep, PyObject* end) {
    Py_ssize_t nargs = _pyck_tuple_size(args);
    for (Py_ssize_t i = 0; i < nargs; i++) {
        if (i > 0) { _serial_write(' '); graphics_putchar(' '); }
        PyObject* item = _pyck_tuple_get(args, i);
        if (item) _print_item(item);
    }
    _serial_write('\n');
    graphics_putchar('\n');
}

extern "C" void _pyck_print_cstr(const char* str) {
    _serial_string(str);
    graphics_string(str);
}

/* print_char(code) builtin -- py_print() (above) always appends a
 * trailing '\n' with no way to suppress it (its own sep/end parameters
 * are accepted but never actually used), which is exactly wrong for
 * echoing keystrokes one at a time as they're typed (see
 * PythonOS/kernel/ps2.py's on_keyboard_irq()): every character landing
 * on its own line instead of accumulating on the current one. This
 * writes exactly one character, nothing else. */
extern "C" void _pyck_print_char(int64_t code) {
    char c = (char)code;
    _serial_write(c);
    graphics_putchar(c);
}

extern "C" void py_fatal_error(const char* message) {
    _serial_string("FATAL: ");
    _serial_string(message);
    _serial_write('\n');
    graphics_string("FATAL: ");
    graphics_string(message);
    graphics_putchar('\n');
    for (;;) {
        asm volatile("cli; hlt");
    }
}
