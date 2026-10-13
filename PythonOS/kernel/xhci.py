from satanic import print_satanic, int_to_str
from memory import unsigned64, PAGE_SIZE
from vmm import PAGE_WRITABLE, PAGE_PCD
from pci import (pci_get_bar0, pci_enable_bus_master,
                 pci_enable_memory_space, pci_read_bar, pci_is_mmio,
                 pci_get_interrupt_line)
from usb_hid import on_hid_input

def debugcon(ch: int):
    outb(0xE9, ch)

def debugcon_hex(v: int):
    debugcon(48)  # '0'
    debugcon(120) # 'x'
    i = 7
    while i >= 0:
        n = (v >> (i * 4)) & 0xF
        if n < 10:
            debugcon(n + 48)
        else:
            debugcon(n + 87)
        i = i - 1
    debugcon(32)  # space
    debugcon(10)  # newline

# xHCI Completion Codes (spec 6.4.5) worth naming in failure prints --
# just the ones actually seen on this controller/device so far, not the
# full table.
def _cc_name(cc: int):
    if cc == 0:
        return "INVALID"
    if cc == 1:
        return "SUCCESS"
    if cc == 2:
        return "DATA_BUFFER_ERROR"
    if cc == 3:
        return "BABBLE"
    if cc == 4:
        return "USB_TRANSACTION_ERROR"
    if cc == 5:
        return "TRB_ERROR"
    if cc == 6:
        return "STALL"
    if cc == 11:
        return "RESOURCE_ERROR"
    if cc == 13:
        return "SHORT_PACKET"
    if cc == 17:
        return "PARAMETER_ERROR"
    if cc == 19:
        return "CONTEXT_STATE_ERROR"
    return "cc" + int_to_str(cc)

# ---------------------------------------------------------------------------
# XHCI register offsets from MMIO base
# ---------------------------------------------------------------------------
XHCI_CAPLENGTH = 0x00
XHCI_HCSPARAMS1 = 0x04
XHCI_HCCPARAMS1 = 0x10
XHCI_RTSOFF = 0x18
XHCI_DBOFF = 0x14

# Operational register offsets (from mmio_base + cap_length)
XHCI_USBCMD = 0x00
XHCI_USBSTS = 0x04
XHCI_CRCR = 0x18
XHCI_DCBAAP = 0x30
XHCI_CONFIG = 0x38

# Port Status and Control
XHCI_PORTSC = 0x400

# XHCI_CMD / XHCI_STS bits
XHCI_CMD_RS = 1
XHCI_CMD_HCRST = 2
XHCI_CMD_INTE = 4
XHCI_STS_HCH = 1
# EINT (bit 3): a system-wide, RW1C "event interrupt" roll-up bit,
# separate from any one interrupter's own IMAN.IP. Linux's xhci_irq()
# (drivers/usb/host/xhci-ring.c) checks and write-1-to-clears this in
# USBSTS on every interrupt, in addition to (not instead of) clearing
# the interrupter's own IMAN.IP -- something this driver never did.
# Left set, it appears to be what was silently suppressing every
# interrupt after the very first one even once EHB-in-ERDP and the
# IOAPIC's edge-vs-level trigger mode were both already fixed: this
# driver's own on_xhci_irq() only ever touched IMAN, never USBSTS.
XHCI_STS_EINT = 8
XHCI_CRCR_CS = 2

# PORTSC bits
XHCI_PORT_CCS = 1
XHCI_PORT_PR = 16
XHCI_PORT_PRC = 2097152

# USB speed values -- PORTSC bits 13:10 / Slot Context Speed field, xHCI's
# own *default* Port Speed ID table (spec 7.2.2.1.1): 1=Full, 2=Low,
# 3=High, 4=SuperSpeed Gen1 x1. (Previously SS=3/HS=2/FS=1 here -- off by
# one against the real table, with Low Speed not represented at all --
# so a High-Speed device like this session's usb-kbd, whose very first
# attach line says "devspeed full+high" i.e. never SuperSpeed, read back
# as raw speed 3 and got misread as "SS", handing its EP0 Context an
# illegal Max Packet Size of 512 instead of 64. That's what Address
# Device's CC_TRB_ERROR was actually reacting to.)
XHCI_SPEED_FS = 1
XHCI_SPEED_LS = 2
XHCI_SPEED_HS = 3
XHCI_SPEED_SS = 4

# TRB type codes
TRB_TYPE_NORMAL = 1
TRB_TYPE_SETUP = 2
TRB_TYPE_DATA = 3
TRB_TYPE_STATUS = 4
TRB_TYPE_LINK = 6
TRB_TYPE_TRANSFER_EVENT = 32
TRB_TYPE_COMMAND_COMPLETION = 33
TRB_IOC = 32
TRB_DIR_IN = 65536
TRB_LINK_TOGGLE_CYCLE = 2

# Event Ring Dequeue Pointer register, bit 3: Event Handler Busy (RW1C).
# Must be written back as 1 every time ERDP is updated, or the xHC will
# never post another interrupt for this interrupter again.
XHCI_ERDP_EHB = 8

# Endpoint 1 IN's Device Context Index (DCI) -- used both as the
# doorbell target and to recognize its Transfer Events.
EP1_IN_DCI = 3

# Command TRB types
XHCI_TRB_ENABLE_SLOT = 9
XHCI_TRB_ADDRESS_DEVICE = 11
XHCI_TRB_CONFIG_ENDPOINT = 12

# USB request constants
USB_REQ_TYPE_DIR_OUT = 0
USB_REQ_TYPE_DIR_IN = 128
USB_REQ_TYPE_TYPE_STANDARD = 0
USB_REQ_TYPE_TYPE_CLASS = 32
USB_REQ_TYPE_RCPT_DEVICE = 0
USB_REQ_TYPE_RCPT_INTERFACE = 1
USB_REQ_GET_DESCRIPTOR = 6
USB_REQ_SET_CONFIGURATION = 9
USB_DESC_DEVICE = 1
USB_DESC_CONFIGURATION = 2
USB_DESC_INTERFACE = 4
USB_DESC_ENDPOINT = 5
HID_REQ_SET_IDLE = 10
HID_REQ_SET_PROTOCOL = 11

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
xhci_mmio = 0
xhci_cap_len = 0
xhci_op = 0
xhci_rt = 0
xhci_db = 0

xhci_max_slots = 0
xhci_max_ports = 0
xhci_ctx_size = 64

xhci_dcbaa = 0
xhci_cmd_ring = 0
xhci_event_ring = 0
xhci_event_rst = 0

xhci_cmd_cycle = 1
xhci_cmd_enqueue = 0
xhci_event_dequeue = 0
xhci_event_cycle = 1

xhci_hhdm = 0

xhci_ep0_rings = 0
# EP0 control-transfer ring's own enqueue cursor/cycle state. Every
# _control_xfer() call used to write its Setup/Data/Status TRBs at a
# fixed ring+0/+16/+32 -- fine for the very first call ever made on a
# fresh ring, but the xHC's own dequeue pointer advances past those
# offsets after processing them, so every call after the first rang
# the doorbell over stale/already-consumed ring space and the xHC
# found nothing there (confirmed in trace: silent ep_kick with no
# fetch_trb at all, for every GET_DESCRIPTOR after the very first).
xhci_ep0_enqueue = 0
xhci_ep0_cycle = 1

xhci_hid_slot = 0
xhci_hid_inited = 0
xhci_hid_report = 0

# EP1 (HID interrupt IN) transfer ring: one data TRB followed by a Link
# TRB, so re-arming after every completed report wraps the ring
# indefinitely instead of dying once a page's worth of reports passes.
xhci_ep1_ring = 0
xhci_ep1_enqueue = 0
xhci_ep1_cycle = 1
xhci_hid_ep1_len = 8

# Output variables for _wait_event
_event_type = 0
_event_p0 = 0
_event_p1 = 0
_event_p2 = 0
_event_ctrl = 0

# Last command completion code (Dword 2 bits 15:0)
_last_cc = 0

# Output for _control_xfer
_xfer_ctrl = 0

# ---------------------------------------------------------------------------
# MMIO helpers
# ---------------------------------------------------------------------------
def _op_read(offset: int):
    return peek32(xhci_op + offset)

def _op_write(offset: int, val: int):
    poke32(xhci_op + offset, val)

def _rt_read(offset: int):
    return peek32(xhci_rt + offset)

def _rt_write(offset: int, val: int):
    poke32(xhci_rt + offset, val)

# ---------------------------------------------------------------------------
# TRB helpers
# ---------------------------------------------------------------------------
def _trb_write(trb_addr: int, p1: int, p2: int, p3: int, control: int):
    poke32(trb_addr, p1)
    poke32(trb_addr + 4, p2)
    poke32(trb_addr + 8, p3)
    poke32(trb_addr + 12, control)

# ---------------------------------------------------------------------------
# Event ring handling
# ---------------------------------------------------------------------------
def _wait_event(timeout: int):
    global _event_type, _event_p0, _event_p1, _event_p2, _event_ctrl
    global xhci_event_dequeue, xhci_event_cycle
    i = 0
    while i < timeout:
        trb = xhci_event_ring + xhci_event_dequeue
        ctrl = peek32(trb + 12)
        if (ctrl & 1) == xhci_event_cycle:
            xhci_event_dequeue = xhci_event_dequeue + 16
            if xhci_event_dequeue >= PAGE_SIZE:
                xhci_event_dequeue = 0
                xhci_event_cycle = 1 - xhci_event_cycle
            erdp_phys = unsigned64(xhci_event_ring + xhci_event_dequeue) - unsigned64(xhci_hhdm)
            _rt_write(0x38, (erdp_phys & 0xFFFFFFFF) | XHCI_ERDP_EHB)
            _rt_write(0x3C, (erdp_phys >> 32) & 0xFFFFFFFF)
            _event_type = (ctrl >> 10) & 0x3F
            _event_p0 = peek32(trb)
            _event_p1 = peek32(trb + 4)
            _event_p2 = peek32(trb + 8)
            _event_ctrl = ctrl
            return
        i = i + 1
    _event_type = 0
    _event_p0 = 0
    _event_p1 = 0
    _event_p2 = 0
    _event_ctrl = 0

def _wait_cmd_complete():
    global _last_cc
    i = 0
    while i < 100000:
        _wait_event(1)
        if _event_type == TRB_TYPE_COMMAND_COMPLETION:
            _last_cc = (_event_p2 >> 24) & 0xFF
            return (_event_ctrl >> 24) & 0xFF
        i = i + 1
    _last_cc = 0
    return 0

# ---------------------------------------------------------------------------
# Command submission
# ---------------------------------------------------------------------------
# `param2` is always a pre-shifted Slot ID (slot_id << 24) from the two
# call sites that need one (Address Device, Configure Endpoint) or plain
# 0 from the one that doesn't (Enable Slot, which allocates a slot
# rather than addressing an existing one). Per spec, every Command TRB
# that takes a Slot ID carries it in Dword 3 (the control dword) bits
# 31:24 -- NOT Dword 2, which for these two commands is RsvdZ. This used
# to write param2 into Dword 2 and leave Dword 3's Slot ID permanently
# 0, so every Address Device / Configure Endpoint command silently
# addressed slot 0 (never an enabled slot) regardless of which slot_id
# the caller actually passed -- that's what was producing CC_TRB_ERROR
# even after the Input Context itself became fully correct.
def _submit_cmd(param0: int, param1: int, param2: int, trb_type: int):
    global xhci_cmd_enqueue, xhci_cmd_cycle
    ctrl = (trb_type << 10) | (xhci_cmd_cycle & 1) | param2
    trb_addr = xhci_cmd_ring + xhci_cmd_enqueue
    _trb_write(trb_addr, param0, param1, 0, ctrl)
    print_satanic("  cmd TRB @" + hex(trb_addr) + " p0=" + hex(param0) + " p1=" + hex(param1) + " ctrl=" + hex(ctrl))
    xhci_cmd_enqueue = xhci_cmd_enqueue + 16
    if xhci_cmd_enqueue >= PAGE_SIZE:
        xhci_cmd_enqueue = 0
        xhci_cmd_cycle = 1 - xhci_cmd_cycle
    cmd_phys = unsigned64(xhci_cmd_ring) - unsigned64(xhci_hhdm)
    poke32(xhci_db, 0)

# ---------------------------------------------------------------------------
# Control transfer on EP0
# ---------------------------------------------------------------------------
# Shared enqueue cursor for the EP0 ring, same wrap-with-Link-TRB pattern
# as _ep1_queue() below. _control_xfer() used to write every Setup/Data/
# Status TD at the same fixed ring+0/+16/+32 regardless of how many
# calls came before it -- fine for the very first control transfer ever
# issued on a fresh ring (the xHC's own dequeue pointer starts there
# too), but after processing it the xHC's dequeue pointer moves past
# those offsets, so every *subsequent* call rang the doorbell over
# stale ring space the xHC had already passed and found nothing to do
# -- confirmed in trace: the first GET_DESCRIPTOR succeeded outright,
# every one after it was a silent ep_kick with no fetch_trb at all.
def _ep0_enqueue_trb(ring: int, p1: int, p2: int, p3: int, type_bits: int):
    global xhci_ep0_enqueue, xhci_ep0_cycle
    ctrl = type_bits | (xhci_ep0_cycle & 1)
    _trb_write(ring + xhci_ep0_enqueue, p1, p2, p3, ctrl)
    xhci_ep0_enqueue = xhci_ep0_enqueue + 16
    if xhci_ep0_enqueue >= PAGE_SIZE - 16:
        ring_phys = unsigned64(ring) - unsigned64(xhci_hhdm)
        link_ctrl = (TRB_TYPE_LINK << 10) | TRB_LINK_TOGGLE_CYCLE | (xhci_ep0_cycle & 1)
        _trb_write(ring + xhci_ep0_enqueue, ring_phys & 0xFFFFFFFF, (ring_phys >> 32) & 0xFFFFFFFF, 0, link_ctrl)
        xhci_ep0_enqueue = 0
        xhci_ep0_cycle = 1 - xhci_ep0_cycle

def _control_xfer(slot_id: int, bm_req: int, b_req: int,
                  w_val: int, w_idx: int, buf: int, w_len: int):
    global _xfer_ctrl
    ring = xhci_ep0_rings + (slot_id - 1) * PAGE_SIZE
    direction = bm_req & 0x80

    # Setup stage. Dword 2 is the TRB Transfer Length, which for a Setup
    # Stage TRB is always literally 8 (the fixed size of a USB Setup
    # packet) -- NOT wLength (that's already packed into Dword 1 above).
    # The control dword needs two things this code never set, both
    # confirmed directly against QEMU's xhci_fire_ctl_transfer() (it
    # sanity-checks exactly these two and, on failure, returns -1 with
    # *no event posted at all* -- no CC, nothing -- which is exactly
    # the "xhci_xfer_start fires, then silence forever" shape seen in
    # every GET_DESCRIPTOR attempt so far):
    #  - TRB_TR_IDT (bit 6, 0x40): mandatory for every Setup Stage TRB.
    #    It tells the xHC the 8 Setup packet bytes are carried directly
    #    in Dwords 0-1 ("immediate data") rather than via a separate
    #    buffer pointer -- there's no separate pointer for a Setup
    #    packet at all, so without this bit QEMU just refuses the TRB.
    #  - TRT (bits 17:16): whether a Data Stage follows and its
    #    direction (2=OUT, 3=IN). Left at its default of 0 ("no data
    #    stage"), the xHC runs Setup+Status only and never dispatches
    #    the Data Stage TRB to the device -- the same silent-hang shape.
    has_data = w_len > 0 and buf != 0
    trt = 0
    if has_data:
        if direction != 0:
            trt = 3  # IN Data Stage
        else:
            trt = 2  # OUT Data Stage
    p1 = bm_req | (b_req << 8) | (w_val << 16)
    p2 = w_idx | (w_len << 16)
    _ep0_enqueue_trb(ring, p1, p2, 8, (TRB_TYPE_SETUP << 10) | (trt << 16) | 0x40)

    # Data stage. Dword 2 is this TRB's own TRB Transfer Length -- the
    # actual byte count to move -- not 0; a 0-length Data Stage TRB
    # moves no data even once dispatched. No IOC here: only the last
    # TRB of a TD should raise a completion event. Setting it on the
    # Data stage too (as this code used to) posted *two* Transfer
    # Events per control transfer with a data stage -- confirmed in
    # trace (idx 3 and idx 4 for one GET_DESCRIPTOR) -- and since
    # _control_xfer()'s wait loop below only consumes one event and
    # returns, the second sat in the event ring to be wrongly picked up
    # by the *next* call's wait loop as if it were that call's own
    # completion. SeaBIOS's own reference Data stage TRB in this same
    # trace has no IOC either -- only its Status stage TRB does.
    if has_data:
        d = 0
        if direction != 0:
            d = TRB_DIR_IN
        _ep0_enqueue_trb(ring, buf, 0, w_len, (TRB_TYPE_DATA << 10) | d)

    # Status stage
    d = TRB_DIR_IN
    if direction != 0:
        d = 0
    _ep0_enqueue_trb(ring, 0, 0, 0, (TRB_TYPE_STATUS << 10) | d | TRB_IOC)

    # Ring doorbell EP0 (endpoint 0 OUT). Doorbell Array entries are 4
    # bytes each, one per slot -- slot_id*4 is this slot's register;
    # slot_id*128 (the old stride here) missed the real EP0 ring
    # entirely, so every control transfer silently timed out.
    poke32(xhci_db + slot_id * 4, 1)

    # Wait for transfer event
    i = 0
    while i < 100000:
        _wait_event(1)
        if _event_type == TRB_TYPE_TRANSFER_EVENT:
            _xfer_ctrl = _event_ctrl
            return
        i = i + 1
    _xfer_ctrl = 0

# ---------------------------------------------------------------------------
# USB request helpers
# ---------------------------------------------------------------------------
def _get_desc(slot_id, dtype, dindex, buf, blen):
    bm = USB_REQ_TYPE_DIR_IN | USB_REQ_TYPE_TYPE_STANDARD | USB_REQ_TYPE_RCPT_DEVICE
    _control_xfer(slot_id, bm, USB_REQ_GET_DESCRIPTOR, (dtype << 8) | dindex, 0, buf, blen)

def _set_config(slot_id, val):
    bm = USB_REQ_TYPE_DIR_OUT | USB_REQ_TYPE_TYPE_STANDARD | USB_REQ_TYPE_RCPT_DEVICE
    _control_xfer(slot_id, bm, USB_REQ_SET_CONFIGURATION, val, 0, 0, 0)

# USB_REQ_TYPE_TYPE_STANDARD/_CLASS (above) are already the real
# bmRequestType bits 6:5 values (0 and 32) -- this used to shift them
# left by 5 *again*, which for STANDARD (0<<5=0) was a harmless no-op
# but for CLASS (32<<5=1024=0x400) corrupted the packed bmRequestType/
# bRequest/wValue dword: bit 10 of that stray 0x400 lands squarely
# inside bRequest's own byte once OR'd together in _control_xfer(),
# turning HID_REQ_SET_PROTOCOL (0x0B) into 0x0F on the wire. Confirmed
# directly in a real boot's xHCI trace: the device STALLed that exact
# malformed request and halted EP0 (harmless here only because nothing
# downstream -- Configure Endpoint, EP1 -- depends on EP0 afterward).
def _set_idle(slot_id):
    bm = USB_REQ_TYPE_DIR_OUT | USB_REQ_TYPE_TYPE_CLASS | USB_REQ_TYPE_RCPT_INTERFACE
    _control_xfer(slot_id, bm, HID_REQ_SET_IDLE, 0, 0, 0, 0)

def _set_protocol(slot_id, proto):
    bm = USB_REQ_TYPE_DIR_OUT | USB_REQ_TYPE_TYPE_CLASS | USB_REQ_TYPE_RCPT_INTERFACE
    _control_xfer(slot_id, bm, HID_REQ_SET_PROTOCOL, proto, 0, 0, 0)

# ---------------------------------------------------------------------------
# Controller initialization
# ---------------------------------------------------------------------------
def init_xhci(va, pm, pci_bus: int, pci_slot: int, pci_func: int):
    global xhci_mmio, xhci_cap_len, xhci_op, xhci_rt, xhci_db
    global xhci_max_slots, xhci_max_ports, xhci_ctx_size
    global xhci_dcbaa, xhci_cmd_ring, xhci_event_ring, xhci_event_rst
    global xhci_cmd_cycle, xhci_cmd_enqueue, xhci_event_dequeue, xhci_event_cycle
    global xhci_ep0_rings, xhci_hid_report, xhci_hhdm

    xhci_hhdm = pm.hhdm

    print_satanic("XHCI: initializing controller")

    bar0_val = pci_get_bar0(pci_bus, pci_slot, pci_func)
    if not pci_is_mmio(bar0_val):
        print_satanic("XHCI: BAR0 not MMIO")
        return 0

    bar0_phys = pci_read_bar(bar0_val)
    pci_enable_memory_space(pci_bus, pci_slot, pci_func)
    pci_enable_bus_master(pci_bus, pci_slot, pci_func)

    print_satanic("  BAR0 phys=" + hex(bar0_phys))
    # Map 64KB MMIO (standard XHCI size) - must map each page individually
    xhci_mmio = va.reserve(64 * 1024)
    print_satanic("  vaddr=" + hex(xhci_mmio))
    map_pages = 64 * 1024 // PAGE_SIZE
    mi = 0
    while mi < map_pages:
        va.vm.map(xhci_mmio + mi * PAGE_SIZE, bar0_phys + mi * PAGE_SIZE, PAGE_WRITABLE | PAGE_PCD, False)
        mi = mi + 1
    print_satanic("  map done")

    # Read capability registers
    print_satanic("  reading cap_len")
    xhci_cap_len = peek8(xhci_mmio + XHCI_CAPLENGTH)
    print_satanic("  cap_len=" + int_to_str(xhci_cap_len))
    hcsparams1 = peek32(xhci_mmio + XHCI_HCSPARAMS1)
    hccparams1 = peek32(xhci_mmio + XHCI_HCCPARAMS1)
    rtsoff = peek32(xhci_mmio + XHCI_RTSOFF) & 0x3FFF

    xhci_max_slots = hcsparams1 & 0xFF
    xhci_max_ports = (hcsparams1 >> 24) & 0xFF
    if xhci_max_ports == 0:
        xhci_max_ports = (hcsparams1 >> 16) & 0xFF
    if (hccparams1 & 1) != 0:
        xhci_ctx_size = 64
    else:
        xhci_ctx_size = 32

    xhci_op = xhci_mmio + xhci_cap_len
    xhci_rt = xhci_mmio + rtsoff
    dboff = peek32(xhci_mmio + XHCI_DBOFF) & 0xFFFFFFFC
    xhci_db = xhci_mmio + dboff

    print_satanic("  cap_length=" + hex(xhci_cap_len))
    print_satanic("  dboff=" + hex(dboff) + " db=" + hex(xhci_db))
    print_satanic("  max_slots=" + int_to_str(xhci_max_slots))
    print_satanic("  max_ports=" + int_to_str(xhci_max_ports))
    print_satanic("  hccparams1=" + hex(hccparams1) + " ctx_size=" + int_to_str(xhci_ctx_size))

    # Reset controller
    _op_write(XHCI_USBCMD, 0)
    i = 0
    while i < 100000:
        if (_op_read(XHCI_USBSTS) & XHCI_STS_HCH) != 0:
            break
        i = i + 1

    _op_write(XHCI_USBCMD, XHCI_CMD_HCRST)
    i = 0
    while i < 100000:
        if (_op_read(XHCI_USBCMD) & XHCI_CMD_HCRST) == 0:
            break
        i = i + 1
    print_satanic("  reset complete")

    # Allocate controller structures
    xhci_dcbaa = pm.alloc_page()
    raw_memzero(xhci_dcbaa, PAGE_SIZE)

    xhci_cmd_ring = pm.alloc_page()
    raw_memzero(xhci_cmd_ring, PAGE_SIZE)
    xhci_cmd_cycle = 1
    xhci_cmd_enqueue = 0

    xhci_event_ring = pm.alloc_page()
    raw_memzero(xhci_event_ring, PAGE_SIZE)
    xhci_event_rst = pm.alloc_page()
    raw_memzero(xhci_event_rst, PAGE_SIZE)
    xhci_event_dequeue = 0
    xhci_event_cycle = 1

    # Event Ring Segment Table
    ev_phys = unsigned64(xhci_event_ring) - unsigned64(pm.hhdm)
    poke64(xhci_event_rst, ev_phys)
    poke64(xhci_event_rst + 8, 256)

    # Slot output contexts
    slot_ctx = pm.alloc_page()
    raw_memzero(slot_ctx, PAGE_SIZE)
    ctx_phys = unsigned64(slot_ctx) - unsigned64(pm.hhdm)

    # EP0 transfer rings: one PAGE_SIZE per slot
    xhci_ep0_rings = pm.alloc_page()
    raw_memzero(xhci_ep0_rings, PAGE_SIZE)

    # Fill DCBAA
    s = 0
    while s < xhci_max_slots:
        poke64(xhci_dcbaa + s * 8, ctx_phys + s * xhci_ctx_size)
        s = s + 1

    # Write DCBAAP
    dcbaap = unsigned64(xhci_dcbaa) - unsigned64(pm.hhdm)
    _op_write(XHCI_DCBAAP, dcbaap & 0xFFFFFFFF)
    _op_write(XHCI_DCBAAP + 4, (dcbaap >> 32) & 0xFFFFFFFF)

    # Write command ring address to CRCR
    cmd_phys = unsigned64(xhci_cmd_ring) - unsigned64(pm.hhdm)
    _op_write(XHCI_CRCR, (cmd_phys & 0xFFFFFFC0) | 1)
    _op_write(XHCI_CRCR + 4, 0)
    print_satanic("  cmd_ring_phys=" + hex(cmd_phys) + " crcr=" + hex(_op_read(XHCI_CRCR)))

    # Set up interrupter 0
    erst_phys = unsigned64(xhci_event_rst) - unsigned64(pm.hhdm)
    _rt_write(0x28, 1)
    _rt_write(0x30, erst_phys & 0xFFFFFFFF)
    _rt_write(0x34, (erst_phys >> 32) & 0xFFFFFFFF)
    erdp_phys = unsigned64(xhci_event_ring) - unsigned64(pm.hhdm)
    _rt_write(0x38, erdp_phys & 0xFFFFFFFF)
    _rt_write(0x3C, (erdp_phys >> 32) & 0xFFFFFFFF)
    _rt_write(0x20, 3)

    # Configure and start
    _op_write(XHCI_CONFIG, xhci_max_slots)
    _op_write(XHCI_USBSTS, 0xFFFFFFFF)
    _op_write(XHCI_USBCMD, XHCI_CMD_RS | XHCI_CMD_INTE)

    i = 0
    while i < 100000:
        if (_op_read(XHCI_USBSTS) & XHCI_STS_HCH) == 0:
            break
        i = i + 1
    print_satanic("  controller started")

    # HID report buffer
    xhci_hid_report = pm.alloc_page()
    raw_memzero(xhci_hid_report, PAGE_SIZE)

    debugcon_hex(xhci_cap_len)
    debugcon_hex(dboff)
    debugcon_hex(rtsoff)
    debugcon_hex(xhci_op - xhci_mmio)
    debugcon_hex(xhci_db - xhci_mmio)
    debugcon_hex(cmd_phys)
    debugcon_hex(unsigned64(xhci_event_ring) - unsigned64(pm.hhdm))
    debugcon_hex(dcbaap)
    debugcon_hex(_op_read(XHCI_USBSTS))
    debugcon_hex(_op_read(XHCI_USBCMD))
    debugcon_hex(_op_read(XHCI_CRCR))
    debugcon_hex(_op_read(XHCI_CRCR + 4))

    print_satanic("  rings ready")
    return 1

# ---------------------------------------------------------------------------
# Port helpers
# ---------------------------------------------------------------------------
def _portsc_addr(port: int):
    return xhci_op + XHCI_PORTSC + port * 0x10

def _port_connected(port: int):
    return (peek32(_portsc_addr(port)) & XHCI_PORT_CCS) != 0

def _port_speed(port: int):
    return (peek32(_portsc_addr(port)) >> 10) & 0xF

def _port_reset(port: int):
    addr = _portsc_addr(port)
    poke32(addr, XHCI_PORT_PR)
    i = 0
    while i < 100000:
        v = peek32(addr)
        if (v & XHCI_PORT_PR) == 0 and (v & XHCI_PORT_PRC) != 0:
            break
        i = i + 1
    poke32(addr, XHCI_PORT_PRC)

# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------
def enumerate_usb_devices(pm, va):
    global xhci_hid_slot, xhci_hid_inited
    global xhci_ep1_ring, xhci_ep1_enqueue, xhci_ep1_cycle, xhci_hid_ep1_len
    global xhci_ep0_enqueue, xhci_ep0_cycle
    print_satanic("XHCI: enumerating USB devices")

    found = 0
    port = 0
    while port < xhci_max_ports:
        if not _port_connected(port):
            port = port + 1
            continue

        print_satanic("  port " + int_to_str(port) + " connected")
        print_satanic("  calling port_reset")
        _port_reset(port)
        print_satanic("  port_reset done")
        speed = _port_speed(port)
        # EP0 Max Packet Size is fixed by spec per negotiated speed --
        # 8 for Low/Full Speed (the first GET_DESCRIPTOR probe re-reads
        # the real device-reported value off the 8-byte descriptor
        # later for actual FS devices that use 16/32/64, but 8 is
        # always legal to start with), 64 for High Speed, 512 for
        # SuperSpeed. Getting this wrong (e.g. handing a High-Speed
        # device Max Packet Size 512) is an illegal Input Context that
        # a real xHC will reject on Address Device.
        max_pkt = 8
        spd = "unknown(" + int_to_str(speed) + ")"
        if speed == XHCI_SPEED_LS:
            spd = "LS"
        elif speed == XHCI_SPEED_FS:
            spd = "FS"
        elif speed == XHCI_SPEED_HS:
            spd = "HS"
            max_pkt = 64
        elif speed == XHCI_SPEED_SS:
            spd = "SS"
            max_pkt = 512
        print_satanic("  speed=" + spd + " (raw=" + int_to_str(speed) + ") max_pkt=" + int_to_str(max_pkt))

        # Enable slot
        debugcon_hex(xhci_cmd_enqueue)
        debugcon_hex(xhci_cmd_cycle)
        debugcon_hex(peek32(xhci_cmd_ring + xhci_cmd_enqueue))
        debugcon_hex(peek32(xhci_cmd_ring + xhci_cmd_enqueue + 4))
        debugcon_hex(peek32(xhci_cmd_ring + xhci_cmd_enqueue + 8))
        debugcon_hex(peek32(xhci_cmd_ring + xhci_cmd_enqueue + 12))
        _submit_cmd(0, 0, 0, XHCI_TRB_ENABLE_SLOT)
        debugcon_hex(_op_read(XHCI_USBSTS))
        slot_id = _wait_cmd_complete()
        debugcon_hex(slot_id)
        debugcon_hex(_last_cc)
        debugcon_hex(_event_type)
        debugcon_hex(_event_p0)
        debugcon_hex(_event_p1)
        debugcon_hex(_event_p2)
        debugcon_hex(_event_ctrl)
        debugcon_hex(_op_read(XHCI_USBSTS))
        if _last_cc != 1:
            print_satanic("  enable slot failed: cc=" + int_to_str(_last_cc) + " (" + _cc_name(_last_cc) + ")")
            port = port + 1
            continue
        print_satanic("  slot=" + int_to_str(slot_id))

        # Build input context for Address Device
        in_ctx = pm.alloc_page()
        raw_memzero(in_ctx, PAGE_SIZE)

        # Input Control Context (offset 0): A0(bit16)=Slot A1(bit17)=EP0
        # QEMU reads Dword 0 as Drop flags, Dword 1 as Add flags (A0|A1 = 3)
        poke32(in_ctx, 0)
        poke32(in_ctx + 4, 3)

        # Input Slot Context -- offset 32, ALWAYS, regardless of what
        # HCCPARAMS1's CSZ bit advertises (this box's reports CSZ=1, 64-
        # byte contexts, per "cap_length=0x40"/"ctx_size=64" above). The
        # xHCI spec says Input Context array entries should be
        # xhci_ctx_size wide, which is what the previous round's fix
        # scaled these offsets by (32 -> 64) -- but checked against
        # QEMU's own hw/usb/hcd-xhci.c (xhci_address_slot()), it reads
        # the Slot Context from a flatly hardcoded ictx+32 and EP0's
        # from ictx+64, never scaling by CSZ at all. So scaling by
        # xhci_ctx_size made this kernel spec-correct and QEMU-wrong:
        # every write landed 32 bytes past where QEMU actually looks,
        # which read back an untouched, all-zero Slot Context (Root Hub
        # Port Number 0, an invalid port) regardless of what was written
        # at the real offset 32 -- exactly what kept producing
        # CC_TRB_ERROR even after every *field* in that context (below)
        # was already correct.
        # Field layout within the Slot Context itself (xHCI 6.2.2):
        # Dword 0 bits 23:20 = Speed, bits 31:27 = Context Entries (highest
        # valid Device Context Index -- 1, EP0 only, until EP1 joins later)
        # Dword 1 bits 23:16 = Root Hub Port Number (1-based)
        # Dword 2/3: TT fields, Interrupter Target, device address/slot
        # state -- all left at 0/RO for this command.
        slot_ctx_off = 32
        poke32(in_ctx + slot_ctx_off, (speed << 20) | (1 << 27))
        poke32(in_ctx + slot_ctx_off + 4, (port + 1) << 16)
        poke32(in_ctx + slot_ctx_off + 8, 0)
        poke32(in_ctx + slot_ctx_off + 12, 0)

        # EP0 transfer ring -- freshly zeroed, so the enqueue cursor
        # _control_xfer() shares with it starts over from scratch too.
        ep0_ring = xhci_ep0_rings + (slot_id - 1) * PAGE_SIZE
        raw_memzero(ep0_ring, PAGE_SIZE)
        ring_phys = unsigned64(ep0_ring) - unsigned64(pm.hhdm)
        xhci_ep0_enqueue = 0
        xhci_ep0_cycle = 1

        # Input EP0 Context -- offset 64, always (see above: QEMU's own
        # xhci_address_slot() hardcodes ictx+64 regardless of CSZ).
        # Real field layout, verified against QEMU's own
        # xhci_init_epctx() (hw/usb/hcd-xhci.c), not just the spec:
        # Dword 0: EP State[2:0]=0, EP Type[5:3]=4(Control), Interval[15:8]
        # Dword 1: Max Packet Size[31:16] -- NOT [15:0]: epctx->max_psize
        #   = ctx[1]>>16, so an unshifted max_pkt here reads back as 0
        # Dword 2: DCS[0] | TR Dequeue Pointer Lo[31:4]
        # Dword 3: TR Dequeue Pointer Hi -- NOT Average TRB Length (that's
        #   a 5th dword, [15:0] of ep0_off+16, which this driver leaves
        #   zeroed): dequeue = xhci_addr64(ctx[2], ctx[3]), so writing
        #   max_pkt here OR'd a garbage high dword onto the ring address
        #   -- the xHC read TRBs from a bogus 64-bit pointer built from
        #   0x40/0x200 << 32, found nothing cycle-bit-valid there, and
        #   silently gave up (hw/usb/hcd-xhci.c's xhci_kick_epctx():
        #   xhci_ring_chain_length() <= 0 on a non-isochronous endpoint
        #   just breaks with no event at all -- exactly the doorbell-
        #   rings-but-nothing-happens behavior seen in the trace).
        # EP Type (Control Bidir=4) and CErr belong in Dword 1 bits
        # 5:3/2:1, not Dword 0 -- see the EP1 fix in enumerate_usb_devices
        # for why this placement matters for xhci_submit()'s type switch.
        # Harmless here only because xhci_fire_ctl_transfer() (used for
        # all EP0 control transfers) never reads epctx->type at all.
        ep0_off = 64
        poke32(in_ctx + ep0_off + 0, 0)
        poke32(in_ctx + ep0_off + 4, (max_pkt << 16) | (4 << 3) | (3 << 1))
        poke32(in_ctx + ep0_off + 8, ring_phys | 1)
        poke32(in_ctx + ep0_off + 12, 0)

        # Address Device command
        in_ctx_phys = unsigned64(in_ctx) - unsigned64(pm.hhdm)
        debugcon_hex(in_ctx_phys)
        debugcon_hex(slot_id)
        debugcon_hex(peek32(in_ctx))
        debugcon_hex(peek32(in_ctx + 4))
        debugcon_hex(peek32(in_ctx + slot_ctx_off))
        debugcon_hex(peek32(in_ctx + slot_ctx_off + 4))
        debugcon_hex(peek32(in_ctx + slot_ctx_off + 8))
        debugcon_hex(peek32(in_ctx + slot_ctx_off + 12))
        debugcon_hex(ep0_off)
        debugcon_hex(peek32(in_ctx + ep0_off))
        debugcon_hex(peek32(in_ctx + ep0_off + 4))
        debugcon_hex(peek32(in_ctx + ep0_off + 8))
        debugcon_hex(peek32(in_ctx + ep0_off + 12))
        _submit_cmd(in_ctx_phys, 0, slot_id << 24, XHCI_TRB_ADDRESS_DEVICE)
        result = _wait_cmd_complete()
        debugcon_hex(result)
        debugcon_hex(_last_cc)
        debugcon_hex(_event_p2)
        debugcon_hex(_op_read(XHCI_USBSTS))
        print_satanic("  addr result=" + int_to_str(result) + " cc=" + int_to_str(_last_cc) + " (" + _cc_name(_last_cc) + ") sts=" + hex(_op_read(XHCI_USBSTS)))
        if _last_cc != 1:
            print_satanic("  address failed: cc=" + int_to_str(_last_cc) + " (" + _cc_name(_last_cc) + ")")
            port = port + 1
            continue
        print_satanic("  addressed")

        # Get device descriptor (8 bytes first)
        desc_buf = pm.alloc_page()
        raw_memzero(desc_buf, PAGE_SIZE)
        _get_desc(slot_id, USB_DESC_DEVICE, 0, desc_buf, 8)

        # Get full device descriptor (18 bytes)
        raw_memzero(desc_buf, PAGE_SIZE)
        _get_desc(slot_id, USB_DESC_DEVICE, 0, desc_buf, 18)
        vid = peek16(desc_buf + 8)
        pid = peek16(desc_buf + 10)
        dev_class = peek8(desc_buf + 4)
        print_satanic("  vid=" + hex(vid) + " pid=" + hex(pid))
        print_satanic("  class=" + hex(dev_class))

        if dev_class == 0xEF:
            print_satanic("  composite, skip")
            port = port + 1
            continue

        # Get configuration descriptor
        raw_memzero(desc_buf, PAGE_SIZE)
        _get_desc(slot_id, USB_DESC_CONFIGURATION, 0, desc_buf, 256)
        cfg_len = peek16(desc_buf + 2)

        # Walk descriptors to find HID interface
        off = 9
        found_hid = 0
        hid_ep_interval = 0
        hid_ep_maxpkt = 0
        while off + 2 <= cfg_len and off < 256:
            b_len = peek8(desc_buf + off)
            b_type = peek8(desc_buf + off + 1)
            if b_len == 0:
                break
            if b_type == USB_DESC_INTERFACE:
                ic = peek8(desc_buf + off + 5)
                isub = peek8(desc_buf + off + 6)
                if ic == 0x03 and isub == 0x01:
                    found_hid = 1
                    print_satanic("  HID interface found")
            if b_type == USB_DESC_ENDPOINT and found_hid != 0:
                attrs = peek8(desc_buf + off + 3)
                if (attrs & 3) == 3:
                    hid_ep_maxpkt = peek16(desc_buf + off + 4) & 0x7FF
                    hid_ep_interval = peek8(desc_buf + off + 6)
                    print_satanic("  interrupt EP pkt=" + int_to_str(hid_ep_maxpkt) + " interval=" + int_to_str(hid_ep_interval))
            off = off + b_len

        if found_hid == 0:
            print_satanic("  no HID interface")
            port = port + 1
            continue

        # Set configuration
        _set_config(slot_id, 1)
        print_satanic("  config set")

        # Set HID boot protocol
        _set_protocol(slot_id, 0)
        print_satanic("  HID boot protocol")

        # Set idle
        _set_idle(slot_id)

        # Configure EP1 IN for interrupt transfers
        xhci_ep1_ring = pm.alloc_page()
        raw_memzero(xhci_ep1_ring, PAGE_SIZE)
        ep1_ring_phys = unsigned64(xhci_ep1_ring) - unsigned64(pm.hhdm)

        cfg_ctx = pm.alloc_page()
        raw_memzero(cfg_ctx, PAGE_SIZE)

        # Input Control Context: Dword0=Drop flags=0, Dword1=Add flags A0|A3
        poke32(cfg_ctx + 0, 0)
        poke32(cfg_ctx + 4, (1 << 0) | (1 << 3))

        # Input Slot Context: Context Entries (Dword 0 bits 31:27) must
        # become the highest valid DCI now that EP1 IN (DCI 3) is
        # joining EP0 (DCI 1) -- 3, not 4 (it's an index, not a count).
        # Offset is a hardcoded 32, not xhci_ctx_size -- QEMU's own
        # xhci_configure_slot() (hw/usb/hcd-xhci.c) reads the Input Slot
        # Context from a flat ictx+32 and every endpoint context from
        # ictx+32+32*DCI, regardless of what HCCPARAMS1's CSZ bit
        # advertises (same QEMU-vs-spec mismatch as Address Device's
        # Input Slot Context above -- scaling these by xhci_ctx_size
        # was spec-correct and QEMU-wrong).
        poke32(cfg_ctx + 32, 3 << 27)

        # Input EP1 IN Context -- offset 32 + 32*DCI, DCI=3 for EP1 IN.
        # Same field layout as EP0's above (verified against QEMU's
        # xhci_init_epctx()): Max Packet Size goes in Dword 1 bits
        # 31:16, and Dword 3 is the TR Dequeue Pointer's high 32 bits
        # (0 here, not Average TRB Length) -- both of these were wrong
        # here too, the same way they were for EP0's context.
        # Dword 0: Interval[23:16]
        # Dword 1: Max Packet Size[31:16] | Max Burst Size[15:8]=0 |
        #          EP Type[7:3]=7(Interrupt In) | CErr[2:1]=3
        # Dword 2: DCS[0] | TR Dequeue Pointer Lo[31:4]
        # Dword 3: TR Dequeue Pointer Hi
        #
        # QEMU's xhci_init_epctx() (hw/usb/hcd-xhci.c) decodes EP Type
        # from ctx[1] bits 5:3 and Interval from ctx[0] bits 23:16 --
        # this code used to pack both into Dword 0 instead (EP Type at
        # bits 5:3 of Dword 0, Interval at bits 15:8 of Dword 0), which
        # left Dword 1's EP Type bits at 0 (EP_TYPE_INVALID). Configure
        # Endpoint's xhci_enable_ep() never validates the type and always
        # returns CC_SUCCESS, so this looked like it worked -- but every
        # doorbell kick's xhci_submit() hit the `default:` case for an
        # unimplemented/invalid endpoint type and silently returned -1
        # with no event ever posted, which is exactly why no second
        # interrupt ever fired after the initial catch-up one.
        ep1_off = 32 + 32 * EP1_IN_DCI
        poke32(cfg_ctx + ep1_off + 0, hid_ep_interval << 16)
        poke32(cfg_ctx + ep1_off + 4, (hid_ep_maxpkt << 16) | (7 << 3) | (3 << 1))
        poke32(cfg_ctx + ep1_off + 8, ep1_ring_phys | 1)
        poke32(cfg_ctx + ep1_off + 12, 0)

        print_satanic("  cfg_ctx slot_dw0=" + hex(peek32(cfg_ctx + 32)) +
                       " ep1_off=" + hex(ep1_off) +
                       " ep1_dw0=" + hex(peek32(cfg_ctx + ep1_off)) +
                       " ep1_dw1=" + hex(peek32(cfg_ctx + ep1_off + 4)) +
                       " ep1_dw2=" + hex(peek32(cfg_ctx + ep1_off + 8)))

        cfg_phys = unsigned64(cfg_ctx) - unsigned64(pm.hhdm)
        _submit_cmd(cfg_phys, 0, slot_id << 24, XHCI_TRB_CONFIG_ENDPOINT)
        cfg_result = _wait_cmd_complete()
        print_satanic("  config EP result=" + int_to_str(cfg_result) + " cc=" + int_to_str(_last_cc) + " (" + _cc_name(_last_cc) + ")")
        if _last_cc != 1:
            print_satanic("  config EP failed: cc=" + int_to_str(_last_cc) + " (" + _cc_name(_last_cc) + ")")
            port = port + 1
            continue

        print_satanic("  EP1 IN configured")
        xhci_hid_slot = slot_id
        xhci_hid_inited = 1

        # Arm the first interrupt IN transfer so the device has somewhere
        # to deliver its first report -- configuring the endpoint alone
        # never gives the xHC a buffer to fill.
        xhci_ep1_enqueue = 0
        xhci_ep1_cycle = 1
        xhci_hid_ep1_len = hid_ep_maxpkt
        report_phys = unsigned64(xhci_hid_report) - unsigned64(pm.hhdm)
        _ep1_queue(report_phys & 0xFFFFFFFF, (report_phys >> 32) & 0xFFFFFFFF, hid_ep_maxpkt)
        print_satanic("  EP1 IN armed")

        found = found + 1
        port = port + 1

    print_satanic("XHCI: " + int_to_str(found) + " HID device(s)")
    return found

# ---------------------------------------------------------------------------
# EP1 (HID interrupt IN) transfer ring
# ---------------------------------------------------------------------------
def _ep1_queue(buf_lo: int, buf_hi: int, length: int):
    global xhci_ep1_enqueue, xhci_ep1_cycle
    ctrl = (TRB_TYPE_NORMAL << 10) | TRB_IOC | (xhci_ep1_cycle & 1)
    _trb_write(xhci_ep1_ring + xhci_ep1_enqueue, buf_lo, buf_hi, length, ctrl)
    xhci_ep1_enqueue = xhci_ep1_enqueue + 16
    if xhci_ep1_enqueue >= PAGE_SIZE - 16:
        # This ring is a full page, same as EP0's ring (_ep0_enqueue_trb)
        # -- the Link TRB only needs to be (re)written once every lap
        # around the whole page, not after every single post. The old
        # code here wrapped (and flipped the cycle bit) after every one
        # TRB, since the ring was treated as a single 32-byte slot --
        # that meant a fresh cycle bit had to land correctly on literally
        # every rearm, and any mismatch there (there was one) makes the
        # xHC's next kick find an invalid TRB and just silently stop,
        # which is exactly why only the very first report ever got
        # through before the endpoint went permanently quiet.
        ring_phys = unsigned64(xhci_ep1_ring) - unsigned64(xhci_hhdm)
        link_ctrl = (TRB_TYPE_LINK << 10) | TRB_LINK_TOGGLE_CYCLE | (xhci_ep1_cycle & 1)
        _trb_write(xhci_ep1_ring + xhci_ep1_enqueue, ring_phys & 0xFFFFFFFF, (ring_phys >> 32) & 0xFFFFFFFF, 0, link_ctrl)
        xhci_ep1_enqueue = 0
        xhci_ep1_cycle = 1 - xhci_ep1_cycle
    poke32(xhci_db + xhci_hid_slot * 4, EP1_IN_DCI)

def _ep1_rearm():
    buf_phys = unsigned64(xhci_hid_report) - unsigned64(xhci_hhdm)
    _ep1_queue(buf_phys & 0xFFFFFFFF, (buf_phys >> 32) & 0xFFFFFFFF, xhci_hid_ep1_len)

# ---------------------------------------------------------------------------
# Event ring draining -- runs in interrupt context. Dispatches completed
# EP1 (keyboard) reports to on_hid_input() and immediately re-arms the
# endpoint for the next one; any other event (e.g. a stray command
# completion left over from enumeration) is just acknowledged.
# ---------------------------------------------------------------------------
def _drain_events():
    global xhci_event_dequeue, xhci_event_cycle
    while True:
        trb = xhci_event_ring + xhci_event_dequeue
        ctrl = peek32(trb + 12)
        if (ctrl & 1) != xhci_event_cycle:
            break
        p2 = peek32(trb + 8)
        etype = (ctrl >> 10) & 0x3F

        xhci_event_dequeue = xhci_event_dequeue + 16
        if xhci_event_dequeue >= PAGE_SIZE:
            xhci_event_dequeue = 0
            xhci_event_cycle = 1 - xhci_event_cycle
        erdp_phys = unsigned64(xhci_event_ring + xhci_event_dequeue) - unsigned64(xhci_hhdm)
        _rt_write(0x38, (erdp_phys & 0xFFFFFFFF) | XHCI_ERDP_EHB)
        _rt_write(0x3C, (erdp_phys >> 32) & 0xFFFFFFFF)

        if etype == TRB_TYPE_TRANSFER_EVENT:
            slot_id = (ctrl >> 24) & 0xFF
            ep_id = (ctrl >> 16) & 0x1F
            cc = (p2 >> 24) & 0xFF
            if xhci_hid_inited != 0 and slot_id == xhci_hid_slot and ep_id == EP1_IN_DCI:
                if cc == 1 or cc == 13:  # Success, or Short Packet (still valid data)
                    on_hid_input(xhci_hid_report)
                _ep1_rearm()

# ---------------------------------------------------------------------------
# IRQ handler
# ---------------------------------------------------------------------------
def on_xhci_irq():
    _op_write(XHCI_USBSTS, XHCI_STS_EINT)
    _rt_write(0x20, 3)
    _drain_events()
