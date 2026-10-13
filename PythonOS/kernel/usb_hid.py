from satanic import print_satanic
from ps2 import tty_input_char

# HID boot keyboard report: 8 bytes
# byte 0: modifier bitmap
# byte 1: reserved
# bytes 2-7: up to 6 keycodes

HID_MOD_LSHIFT = 0x02
HID_MOD_RSHIFT = 0x20

hid_shift_held = 0
hid_inited = 0

# The 6 keycodes from the *previous* report on_hid_input() saw, kept so
# a still-held key can be told apart from a newly-pressed one. A HID
# boot report is a snapshot of every key currently down, not a single
# key-down event -- the device sends a fresh report each time that set
# changes at all, so pressing 'a' then 's' while 'a' is still held
# produces report [a] followed by report [a, s], both containing 'a'.
# Without this, every slot in every report got echoed unconditionally,
# so typing fast enough to overlap two or three keys re-echoed each
# already-typed character once per report it was still present in --
# holding 'a' through 's' and 'd' going down printed "a", "as", "asd"
# instead of "a", "s", "d". allocated in init_usb_hid(), same as
# ps2.py's line_buf.
hid_prev_keys = 0

def _hid_usage_to_ascii(usage: int, shift: int):
    # Letters: usage 0x04-0x1D -> a-z
    if usage >= 0x04 and usage <= 0x1D:
        base = usage - 0x04
        if shift != 0:
            return 65 + base
        return 97 + base

    # Numbers: usage 0x1E-0x27 -> 1-9, 0
    if usage == 0x1E:
        if shift != 0:
            return 33
        return 49
    if usage == 0x1F:
        if shift != 0:
            return 64
        return 50
    if usage == 0x20:
        if shift != 0:
            return 35
        return 51
    if usage == 0x21:
        if shift != 0:
            return 36
        return 52
    if usage == 0x22:
        if shift != 0:
            return 37
        return 53
    if usage == 0x23:
        if shift != 0:
            return 94
        return 54
    if usage == 0x24:
        if shift != 0:
            return 38
        return 55
    if usage == 0x25:
        if shift != 0:
            return 42
        return 56
    if usage == 0x26:
        if shift != 0:
            return 40
        return 57
    if usage == 0x27:
        if shift != 0:
            return 41
        return 48

    # Special keys
    if usage == 0x28:  # Enter
        return 10
    if usage == 0x29:  # Escape
        return 27
    if usage == 0x2A:  # Backspace
        return 8
    if usage == 0x2B:  # Tab
        return 9
    if usage == 0x2C:  # Space
        return 32

    # Punctuation
    if usage == 0x2D:
        if shift != 0:
            return 95
        return 45
    if usage == 0x2E:
        if shift != 0:
            return 43
        return 61
    if usage == 0x2F:
        if shift != 0:
            return 123
        return 91
    if usage == 0x30:
        if shift != 0:
            return 125
        return 93
    if usage == 0x31:
        if shift != 0:
            return 124
        return 92
    if usage == 0x33:
        if shift != 0:
            return 58
        return 59
    if usage == 0x34:
        if shift != 0:
            return 34
        return 39
    if usage == 0x35:
        if shift != 0:
            return 126
        return 96
    if usage == 0x36:
        if shift != 0:
            return 60
        return 44
    if usage == 0x37:
        if shift != 0:
            return 62
        return 46
    if usage == 0x38:
        if shift != 0:
            return 63
        return 47

    return 0

def _was_already_held(usage: int):
    j = 0
    while j < 6:
        if peek8(hid_prev_keys + j) == usage:
            return 1
        j = j + 1
    return 0

def on_hid_input(report_addr: int):
    global hid_shift_held

    if hid_inited == 0:
        return

    modifiers = peek8(report_addr)
    hid_shift_held = 0
    if (modifiers & HID_MOD_LSHIFT) != 0:
        hid_shift_held = 1
    if (modifiers & HID_MOD_RSHIFT) != 0:
        hid_shift_held = 1

    # Decode each keycode in bytes 2-7
    i = 0
    while i < 6:
        usage = peek8(report_addr + 2 + i)
        if usage == 0:
            i = i + 1
            continue

        # Skip modifier keycodes (0xE0-0xE7)
        if usage >= 0xE0:
            i = i + 1
            continue

        # Only a key that wasn't already down in the previous report
        # is a new keypress -- see hid_prev_keys' own comment above.
        if _was_already_held(usage) != 0:
            i = i + 1
            continue

        ascii_code = _hid_usage_to_ascii(usage, hid_shift_held)
        if ascii_code != 0:
            tty_input_char(ascii_code)

        i = i + 1

    # This report's keycodes become "already held" for next time.
    i = 0
    while i < 6:
        poke8(hid_prev_keys + i, peek8(report_addr + 2 + i))
        i = i + 1

def init_usb_hid(va):
    global hid_inited
    global hid_prev_keys
    hid_prev_keys = va.alloc(6)
    i = 0
    while i < 6:
        poke8(hid_prev_keys + i, 0)
        i = i + 1
    hid_inited = 1
    print_satanic("USB HID keyboard driver initialized")
