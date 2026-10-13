from satanic import print_satanic, int_to_str
from memory import limine_response, unsigned64

MODULE_MAGIC_A = 0x3e7e279702be32af
MODULE_MAGIC_B = 0xca1c4f3bd1280cee

HEADER_SIZE = 110

def _hex_digit_value(c: int):
    if c >= 48 and c <= 57:
        return c - 48
    if c >= 97 and c <= 102:
        return c - 97 + 10
    if c >= 65 and c <= 70:
        return c - 65 + 10
    return 0

def _read_hex8(addr: int):
    value = 0
    i = 0
    while i < 8:
        value = value * 16 + _hex_digit_value(peek8(addr + i))
        i = i + 1
    return value

def _align4(n: int):
    rem = n % 4
    if rem == 0:
        return n
    return n + (4 - rem)

def _read_name(addr: int, namesize: int):
    s = ""
    i = 0
    while i < namesize - 1:
        s = s + chr(peek8(addr + i))
        i = i + 1
    return s

def find_module():
    response = limine_response(MODULE_MAGIC_A, MODULE_MAGIC_B)
    if response == 0:
        return 0
    module_count = peek64(response + 8)
    if module_count == 0:
        return 0
    modules_ptr = peek64(response + 16)
    return peek64(modules_ptr)

def list_cpio_files(file_addr: int):
    base = unsigned64(peek64(file_addr + 8))
    size = unsigned64(peek64(file_addr + 16))
    print_satanic("initramfs module at:")
    print_satanic(hex(base))
    print_satanic("initramfs module size:")
    print_satanic(int_to_str(size))

    offset = 0
    while offset < size:
        header = base + offset
        mode = _read_hex8(header + 14)
        filesize = _read_hex8(header + 54)
        namesize = _read_hex8(header + 94)
        name = _read_name(header + HEADER_SIZE, namesize)
        if name == "TRAILER!!!":
            offset = size
        else:
            print_satanic(name + "  " + int_to_str(filesize) +
                           " bytes  mode " + hex(mode))
            data_start = _align4(HEADER_SIZE + namesize)
            offset = offset + _align4(data_start + filesize)

class CpioFile:
    def __init__(self, addr: int, size: int, header_addr: int, mode: int):
        self.addr = addr
        self.size = size
        self.header_addr = header_addr
        self.mode = mode

def entry_name_at(header_addr: int):
    namesize = _read_hex8(header_addr + 94)
    return _read_name(header_addr + HEADER_SIZE, namesize)

def find_file(base: int, size: int, target_name: str):
    offset = 0
    while offset < size:
        header = base + offset
        mode = _read_hex8(header + 14)
        filesize = _read_hex8(header + 54)
        namesize = _read_hex8(header + 94)
        name = _read_name(header + HEADER_SIZE, namesize)
        data_start = _align4(HEADER_SIZE + namesize)
        if name == target_name:
            return CpioFile(header + data_start, filesize, header, mode)
        if name == "TRAILER!!!":
            return CpioFile(0, 0, 0, 0)
        offset = offset + _align4(data_start + filesize)
    return CpioFile(0, 0, 0, 0)

def find_module_file(target_name: str):
    file_addr = find_module()
    if file_addr == 0:
        return CpioFile(0, 0, 0, 0)
    base = unsigned64(peek64(file_addr + 8))
    size = unsigned64(peek64(file_addr + 16))
    return find_file(base, size, target_name)

def cpio_demo():
    file_addr = find_module()
    if file_addr == 0:
        print_satanic("No initramfs module found")
    else:
        list_cpio_files(file_addr)
