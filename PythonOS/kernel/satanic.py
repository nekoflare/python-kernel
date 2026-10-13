ESC = chr(27)

def int_to_str(n: int):
    if n == 0:
        return "0"
    neg = n < 0
    if neg:
        n = 0 - n
    digits = ""
    while n > 0:
        d = n % 10
        digits = chr(48 + d) + digits
        n = n // 10
    if neg:
        digits = "-" + digits
    return digits

def bool_to_str(b: bool):
    if b:
        return "True"
    return "False"

def satanic_color_at(i: int):
    phase = (i * 24) % 512
    green = phase
    if phase >= 256:
        green = 511 - phase
    return ESC + "[38;2;255;" + int_to_str(green) + ";0m"

def print_satanic(text: str):
    n = len(text)
    i = 0
    out = ""
    while i < n:
        out = out + satanic_color_at(i) + text[i]
        i = i + 1
    out = out + ESC + "[0m"
    print(out)

def print_cursed(text: str):
    # \x1b[666m -- not a real SGR code, this console's own (see
    # arch/x86_64_graphical/graphics.cpp's ansi_apply_sgr()): corrodes
    # every glyph printed after it into static/grain instead of a clean
    # bitmap font, until the trailing \x1b[0m resets it.
    print(ESC + "[666m" + text + ESC + "[0m")
