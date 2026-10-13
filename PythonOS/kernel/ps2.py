from threads import wake_all_blocked

# Standard AT keyboard controller I/O ports -- QEMU's emulated 8042
# reports plain scan code set 1 over these with no controller-side
# configuration needed, so init_ps2() only has to allocate this
# driver's own buffers, not talk to the hardware at all.
PS2_DATA_PORT = 0x60
PS2_STATUS_PORT = 0x64

SC_LSHIFT = 0x2A
SC_RSHIFT = 0x36
SC_BACKSPACE = 0x0E
SC_ENTER = 0x1C
SC_ESCAPE = 0x01

LINE_BUF_SIZE = 256
STDIN_BUF_SIZE = 4096

# line_buf accumulates a row of typed (echoed) characters between
# keystrokes; only once Enter commits it does it get copied into
# stdin_buf (the ring buffer SYS_READ actually drains) -- the ordinary
# canonical/line-buffered behavior a real terminal gives a program that
# never puts the tty into raw mode. See tty_icanon/tty_echo below and
# tty_input_char() for the case where one does (see syscall.py's
# SYS_IOCTL, TCSETS2 and friends).
line_buf = 0
line_len = 0

# c_lflag's ICANON/ECHO bits, as last set via SYS_IOCTL's TCSETS2 (or
# TCSETSW2/TCSETSF2 -- treated identically, since there's no separate
# input queue to drain or flush differently between them). Both default
# on: a program that never calls tcsetattr() at all gets exactly the
# canonical, echoing behavior a real freshly-opened tty starts in.
# tty_input_char() (below) is the one place that reads these -- shared
# by both this file's own on_keyboard_irq() and usb_hid.py's
# on_hid_input(), so a PS/2 and a USB keyboard behave identically
# under raw mode.
tty_icanon = 1
tty_echo = 1

def tty_set_icanon(value: int):
    global tty_icanon
    tty_icanon = value

def tty_set_echo(value: int):
    global tty_echo
    tty_echo = value

def tty_get_icanon():
    return tty_icanon

def tty_get_echo():
    return tty_echo

stdin_buf = 0
stdin_head = 0
stdin_tail = 0
stdin_count = 0

shift_held = 0
# 0xE0-prefixed scan codes (arrow keys, etc.) are two bytes; this kernel
# doesn't support any of them yet, so the prefix is only tracked long
# enough to correctly discard the byte that follows it, rather than
# misreading that follow-up byte as an ordinary key.
pending_extended = 0

def init_ps2(va):
    global line_buf
    global stdin_buf
    line_buf = va.alloc(LINE_BUF_SIZE)
    stdin_buf = va.alloc(STDIN_BUF_SIZE)

def _stdin_push(byte: int):
    global stdin_tail
    global stdin_count
    if stdin_count < STDIN_BUF_SIZE:
        poke8(stdin_buf + stdin_tail, byte)
        stdin_tail = (stdin_tail + 1) % STDIN_BUF_SIZE
        stdin_count = stdin_count + 1

def _stdin_pop():
    global stdin_head
    global stdin_count
    byte = peek8(stdin_buf + stdin_head)
    stdin_head = (stdin_head + 1) % STDIN_BUF_SIZE
    stdin_count = stdin_count - 1
    return byte

def stdin_available():
    return stdin_count

def stdin_read(buf: int, count: int):
    n = 0
    while n < count and stdin_count > 0:
        poke8(buf + n, _stdin_pop())
        n = n + 1
    return n

def _flush_line():
    global line_len
    i = 0
    while i < line_len:
        _stdin_push(peek8(line_buf + i))
        i = i + 1
    _stdin_push(10)  # '\n'
    line_len = 0
    wake_all_blocked()

def tty_input_char(ascii_code: int):
    # Shared policy for "the keyboard driver just decoded this ASCII
    # byte from a keypress -- what happens to it?", called by both this
    # file's own on_keyboard_irq() (below) and usb_hid.py's
    # on_hid_input(), so a PS/2 and a USB keyboard behave identically
    # under raw mode (tty_icanon), and so a program's own tcsetattr()
    # ECHO choice (tty_echo) is honored no matter which one produced
    # the byte.
    global line_len
    if tty_icanon == 0:
        # Raw/non-canonical: every key is delivered to stdin the
        # instant it's pressed, no line buffering at all -- Enter and
        # Backspace are just ordinary data bytes now, not special
        # editing commands, since a full-screen program (this is what
        # busybox vi asks for) does its own line editing once it can
        # see every keystroke as it happens instead of only ever
        # getting a finished line. Whether that keystroke is also drawn
        # to the screen is a completely separate question the program
        # already answered via ECHO -- most raw-mode programs turn it
        # off and draw their own echo (their own ANSI cursor writes),
        # which is exactly why this doesn't unconditionally print here
        # the way canonical mode below always does.
        _stdin_push(ascii_code)
        wake_all_blocked()
        if tty_echo != 0:
            print_char(ascii_code)
        return
    # Canonical (line-buffered) mode -- the original, only behavior
    # this kernel had before tty_icanon existed.
    if ascii_code == 10:  # Enter
        if tty_echo != 0:
            print_char(10)
        _flush_line()
        return
    if ascii_code == 8:  # Backspace
        if line_len > 0:
            line_len = line_len - 1
        return
    if ascii_code != 0 and line_len < LINE_BUF_SIZE:
        poke8(line_buf + line_len, ascii_code)
        line_len = line_len + 1
        if tty_echo != 0:
            print_char(ascii_code)

def _letter_ascii(key: int, shift: int):
    # 0x10-0x19 qwertyuiop, 0x1E-0x26 asdfghjkl, 0x2C-0x32 zxcvbnm --
    # scan code set 1's three letter rows, each contiguous.
    base = 0
    if key >= 0x10 and key <= 0x19:
        row = "qwertyuiop"
        base = key - 0x10
    elif key >= 0x1E and key <= 0x26:
        row = "asdfghjkl"
        base = key - 0x1E
    elif key >= 0x2C and key <= 0x32:
        row = "zxcvbnm"
        base = key - 0x2C
    else:
        return 0
    lower = row[base]
    if shift != 0:
        return _ascii_upper(lower)
    return _ascii_lower(lower)

def _ascii_lower(c: str):
    return _ascii_of(c)

def _ascii_upper(c: str):
    return _ascii_of(c) - 32

def _ascii_of(c: str):
    letters = "abcdefghijklmnopqrstuvwxyz"
    i = 0
    while i < 26:
        if letters[i] == c:
            return 97 + i
        i = i + 1
    return 0

def _digit_row_ascii(key: int, shift: int):
    if key == 0x02:
        if shift != 0:
            return 33  # !
        return 49  # 1
    elif key == 0x03:
        if shift != 0:
            return 64  # @
        return 50  # 2
    elif key == 0x04:
        if shift != 0:
            return 35  # #
        return 51  # 3
    elif key == 0x05:
        if shift != 0:
            return 36  # $
        return 52  # 4
    elif key == 0x06:
        if shift != 0:
            return 37  # %
        return 53  # 5
    elif key == 0x07:
        if shift != 0:
            return 94  # ^
        return 54  # 6
    elif key == 0x08:
        if shift != 0:
            return 38  # &
        return 55  # 7
    elif key == 0x09:
        if shift != 0:
            return 42  # *
        return 56  # 8
    elif key == 0x0A:
        if shift != 0:
            return 40  # (
        return 57  # 9
    elif key == 0x0B:
        if shift != 0:
            return 41  # )
        return 48  # 0
    return 0

def _punct_ascii(key: int, shift: int):
    if key == 0x0C:
        if shift != 0:
            return 95  # _
        return 45  # -
    elif key == 0x0D:
        if shift != 0:
            return 43  # +
        return 61  # =
    elif key == 0x1A:
        if shift != 0:
            return 123  # {
        return 91  # [
    elif key == 0x1B:
        if shift != 0:
            return 125  # }
        return 93  # ]
    elif key == 0x27:
        if shift != 0:
            return 58  # :
        return 59  # ;
    elif key == 0x28:
        if shift != 0:
            return 34  # "
        return 39  # '
    elif key == 0x29:
        if shift != 0:
            return 126  # ~
        return 96  # `
    elif key == 0x2B:
        if shift != 0:
            return 124  # |
        return 92  # backslash
    elif key == 0x33:
        if shift != 0:
            return 60  # <
        return 44  # ,
    elif key == 0x34:
        if shift != 0:
            return 62  # >
        return 46  # .
    elif key == 0x35:
        if shift != 0:
            return 63  # ?
        return 47  # /
    elif key == 0x39:
        return 32  # space
    return 0

def _scancode_to_ascii(key: int, shift: int):
    ch = _letter_ascii(key, shift)
    if ch != 0:
        return ch
    ch = _digit_row_ascii(key, shift)
    if ch != 0:
        return ch
    return _punct_ascii(key, shift)

def on_keyboard_irq():
    global shift_held
    global pending_extended
    code = inb(PS2_DATA_PORT)
    if code == 0xE0:
        pending_extended = 1
        return
    if pending_extended != 0:
        pending_extended = 0
        return
    released = (code & 0x80) != 0
    key = code & 0x7F
    if key == SC_LSHIFT or key == SC_RSHIFT:
        if released:
            shift_held = 0
        else:
            shift_held = 1
        return
    if released:
        return
    if key == SC_ENTER:
        tty_input_char(10)
        return
    if key == SC_BACKSPACE:
        tty_input_char(8)
        return
    if key == SC_ESCAPE:
        tty_input_char(27)
        return
    ascii_code = _scancode_to_ascii(key, shift_held)
    if ascii_code != 0:
        tty_input_char(ascii_code)
