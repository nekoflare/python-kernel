from memory import limine_response

FRAMEBUFFER_MAGIC_A = 0x9d5827dcd881dd75
FRAMEBUFFER_MAGIC_B = 0xa3148604f6fab11b

# Inverted-cross proportions (see draw_inverted_cross() below) -- tweak
# these directly rather than digging through the method itself.
CROSS_THICKNESS = 6
CROSS_ARM_HALF_WIDTH = 16
# Vertical bar length above the crossbar, and below it -- together
# these set both the vertical bar's total height (their sum) and where
# the crossbar sits along it (their ratio): bigger CROSS_LONG_ARM
# relative to CROSS_SHORT_ARM pushes the crossbar higher up the bar.
CROSS_LONG_ARM = 28
CROSS_SHORT_ARM = 12

class Framebuffer:
    def __init__(self):
        response = limine_response(FRAMEBUFFER_MAGIC_A, FRAMEBUFFER_MAGIC_B)
        framebuffers_ptr = peek64(response + 16)
        fb0 = peek64(framebuffers_ptr)
        self.addr = peek64(fb0)
        self.width = peek64(fb0 + 8)
        self.height = peek64(fb0 + 16)
        self.pitch = peek64(fb0 + 24)
        self.pitch_px = self.pitch // 4

    def put_pixel(self, x: int, y: int, color: int):
        if x < 0 or y < 0 or x >= self.width or y >= self.height:
            return
        offset = (y * self.pitch_px + x) * 4
        poke32(self.addr + offset, color)

    def fill_rect(self, x0: int, y0: int, x1: int, y1: int, color: int):
        if x0 > x1:
            t = x0
            x0 = x1
            x1 = t
        if y0 > y1:
            t = y0
            y0 = y1
            y1 = t
        y = y0
        while y <= y1:
            self._fill_hline(x0, x1, y, color)
            y = y + 1

    def fill_triangle(self, x0: int, y0: int, x1: int, y1: int, x2: int, y2: int, color: int):
        if y0 > y1:
            tx = x0
            ty = y0
            x0 = x1
            y0 = y1
            x1 = tx
            y1 = ty
        if y1 > y2:
            tx = x1
            ty = y1
            x1 = x2
            y1 = y2
            x2 = tx
            y2 = ty
        if y0 > y1:
            tx = x0
            ty = y0
            x0 = x1
            y0 = y1
            x1 = tx
            y1 = ty
        if y0 == y2:
            return
        if y1 == y2:
            self._fill_flat_bottom(x0, y0, x1, y1, x2, y2, color)
        else:
            if y0 == y1:
                self._fill_flat_top(x0, y0, x1, y1, x2, y2, color)
            else:
                x3 = x0 + (x2 - x0) * (y1 - y0) // (y2 - y0)
                self._fill_flat_bottom(x0, y0, x1, y1, x3, y1, color)
                self._fill_flat_top(x1, y1, x3, y1, x2, y2, color)

    def _fill_flat_bottom(self, x0: int, y0: int, x1: int, y1: int, x2: int, y2: int, color: int):
        dy = y1 - y0
        if dy == 0:
            return
        y = y0
        while y <= y1:
            xa = x0 + (x1 - x0) * (y - y0) // dy
            xb = x0 + (x2 - x0) * (y - y0) // dy
            self._fill_hline(xa, xb, y, color)
            y = y + 1

    def _fill_flat_top(self, x0: int, y0: int, x1: int, y1: int, x2: int, y2: int, color: int):
        dy = y2 - y0
        if dy == 0:
            return
        y = y0
        while y <= y2:
            xa = x0 + (x2 - x0) * (y - y0) // dy
            xb = x1 + (x2 - x1) * (y - y0) // dy
            self._fill_hline(xa, xb, y, color)
            y = y + 1

    def _fill_hline(self, xa: int, xb: int, y: int, color: int):
        if xa > xb:
            t = xa
            xa = xb
            xb = t
        x = xa
        while x <= xb:
            self.put_pixel(x, y, color)
            x = x + 1

    def fill_pentagram(self, cx: int, cy: int, color: int):
        # Point-down (inverted) rather than point-up: every vertex is the
        # same point-up pentagram's offset negated, i.e. rotated 180
        # degrees around the center.
        a0x = cx
        a0y = cy + 60
        a1x = cx - 57
        a1y = cy + 19
        a2x = cx - 35
        a2y = cy - 49
        a3x = cx + 35
        a3y = cy - 49
        a4x = cx + 57
        a4y = cy + 19
        b0x = cx - 13
        b0y = cy + 19
        b1x = cx - 22
        b1y = cy - 7
        b2x = cx
        b2y = cy - 23
        b3x = cx + 22
        b3y = cy - 7
        b4x = cx + 13
        b4y = cy + 19
        self.fill_triangle(cx, cy, a0x, a0y, b0x, b0y, color)
        self.fill_triangle(cx, cy, b0x, b0y, a1x, a1y, color)
        self.fill_triangle(cx, cy, a1x, a1y, b1x, b1y, color)
        self.fill_triangle(cx, cy, b1x, b1y, a2x, a2y, color)
        self.fill_triangle(cx, cy, a2x, a2y, b2x, b2y, color)
        self.fill_triangle(cx, cy, b2x, b2y, a3x, a3y, color)
        self.fill_triangle(cx, cy, a3x, a3y, b3x, b3y, color)
        self.fill_triangle(cx, cy, b3x, b3y, a4x, a4y, color)
        self.fill_triangle(cx, cy, a4x, a4y, b4x, b4y, color)
        self.fill_triangle(cx, cy, b4x, b4y, a0x, a0y, color)

    def draw_line_2px(self, x1: int, y1: int, x2: int, y2: int, color: int):
        dx = x2 - x1
        if dx < 0:
            dx = 0 - dx
        dy = y2 - y1
        if dy < 0:
            dy = 0 - dy
        sx = 1
        if x1 >= x2:
            sx = -1
        sy = 1
        if y1 >= y2:
            sy = -1
        err = dx - dy
        x = x1
        y = y1
        while True:
            self.put_pixel(x, y, color)
            self.put_pixel(x + 1, y, color)
            self.put_pixel(x, y + 1, color)
            self.put_pixel(x + 1, y + 1, color)
            if x == x2 and y == y2:
                break
            e2 = 2 * err
            if e2 > 0 - dy:
                err = err - dy
                x = x + sx
            if e2 < dx:
                err = err + dx
                y = y + sy

    def _plot_circle_octants(self, cx: int, cy: int, x: int, y: int, color: int):
        self.put_pixel(cx + x, cy + y, color)
        self.put_pixel(cx - x, cy + y, color)
        self.put_pixel(cx + x, cy - y, color)
        self.put_pixel(cx - x, cy - y, color)
        self.put_pixel(cx + y, cy + x, color)
        self.put_pixel(cx - y, cy + x, color)
        self.put_pixel(cx + y, cy - x, color)
        self.put_pixel(cx - y, cy - x, color)

    def draw_circle(self, cx: int, cy: int, radius: int, color: int):
        # Midpoint circle algorithm -- integer-only, same style as the
        # triangle fill above, no trig/float needed for a pixel outline.
        x = radius
        y = 0
        err = 1 - radius
        while x >= y:
            self._plot_circle_octants(cx, cy, x, y, color)
            y = y + 1
            if err < 0:
                err = err + 2 * y + 1
            else:
                x = x - 1
                err = err + 2 * (y - x) + 1

    def draw_ring(self, cx: int, cy: int, radius: int, color: int):
        # A 2px-thick ring (two adjacent 1px circles) -- matches
        # draw_pentagram_border's own 2px line weight, so the circle
        # holds its own against the star it encloses instead of
        # reading as a thin scratch around it.
        self.draw_circle(cx, cy, radius, color)
        self.draw_circle(cx, cy, radius - 1, color)

    def draw_inverted_cross(self, cx: int, cy: int, color: int):
        # `cx, cy` is the crossbar's own center. A right-side-up Latin
        # cross has its crossbar in the *upper* third (long foot below,
        # short head above); inverted just swaps which side is long --
        # long arm above the crossbar here, short stub below. Proportions
        # are the module-level CROSS_* constants above, not local
        # variables, so they're adjustable without editing this method.
        half_thick = CROSS_THICKNESS // 2
        self.fill_rect(cx - half_thick, cy - CROSS_LONG_ARM, cx + half_thick, cy + CROSS_SHORT_ARM, color)
        self.fill_rect(cx - CROSS_ARM_HALF_WIDTH, cy - half_thick, cx + CROSS_ARM_HALF_WIDTH, cy + half_thick, color)

    def draw_pentagram_border(self, cx: int, cy: int, color: int):
        # Same point-down vertices as fill_pentagram -- the border has to
        # trace the exact same silhouette the fill uses.
        a0x = cx
        a0y = cy + 60
        a1x = cx - 57
        a1y = cy + 19
        a2x = cx - 35
        a2y = cy - 49
        a3x = cx + 35
        a3y = cy - 49
        a4x = cx + 57
        a4y = cy + 19
        b0x = cx - 13
        b0y = cy + 19
        b1x = cx - 22
        b1y = cy - 7
        b2x = cx
        b2y = cy - 23
        b3x = cx + 22
        b3y = cy - 7
        b4x = cx + 13
        b4y = cy + 19
        self.draw_line_2px(a0x, a0y, b0x, b0y, color)
        self.draw_line_2px(b0x, b0y, a1x, a1y, color)
        self.draw_line_2px(a1x, a1y, b1x, b1y, color)
        self.draw_line_2px(b1x, b1y, a2x, a2y, color)
        self.draw_line_2px(a2x, a2y, b2x, b2y, color)
        self.draw_line_2px(b2x, b2y, a3x, a3y, color)
        self.draw_line_2px(a3x, a3y, b3x, b3y, color)
        self.draw_line_2px(b3x, b3y, a4x, a4y, color)
        self.draw_line_2px(a4x, a4y, b4x, b4y, color)
        self.draw_line_2px(b4x, b4y, a0x, a0y, color)

def pentagram_demo():
    fb = Framebuffer()
    cx = fb.width - 80
    cy = fb.height - 80
    # A pentacle, not just a pentagram: blood-red fill, a fiery
    # red-orange outline and matching ring binding it -- one accent
    # color ties the circle and the star together into a single sigil
    # instead of two unrelated shapes. Ring radius (68) sits just
    # outside the star's own reach (its points sit ~60px out), close
    # enough to read as "drawn around it" rather than floating free.
    accent_color = 0x00FF1800
    fill_color = 0x008B0000
    ring_radius = 68
    fb.draw_ring(cx, cy, ring_radius, accent_color)
    fb.fill_pentagram(cx, cy, fill_color)
    fb.draw_pentagram_border(cx, cy, accent_color)

def crosses_demo():
    fb = Framebuffer()
    color = 0x00FF1800
    cy = 70
    spacing = 55
    right_margin = 60
    i = 0
    while i < 3:
        cx = fb.width - right_margin - i * spacing
        fb.draw_inverted_cross(cx, cy, color)
        i = i + 1
