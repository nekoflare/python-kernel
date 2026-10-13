from satanic import print_satanic, bool_to_str

class CPU:
    def __init__(self):
        self.max_ext_leaf = cpuid_eax(0x80000000)
        self.feature_ecx = cpuid_ecx(1)
        self.feature_edx = cpuid_edx(1)

    def chunk(self, leaf: int):
        a = cpuid_eax(leaf)
        b = cpuid_ebx(leaf)
        c = cpuid_ecx(leaf)
        d = cpuid_edx(leaf)
        s = chr(a & 0xFF) + chr((a >> 8) & 0xFF) + chr((a >> 16) & 0xFF) + chr((a >> 24) & 0xFF)
        s = s + chr(b & 0xFF) + chr((b >> 8) & 0xFF) + chr((b >> 16) & 0xFF) + chr((b >> 24) & 0xFF)
        s = s + chr(c & 0xFF) + chr((c >> 8) & 0xFF) + chr((c >> 16) & 0xFF) + chr((c >> 24) & 0xFF)
        s = s + chr(d & 0xFF) + chr((d >> 8) & 0xFF) + chr((d >> 16) & 0xFF) + chr((d >> 24) & 0xFF)
        return s

    def vendor_id(self):
        b = cpuid_ebx(0)
        d = cpuid_edx(0)
        c = cpuid_ecx(0)
        s = chr(b & 0xFF) + chr((b >> 8) & 0xFF) + chr((b >> 16) & 0xFF) + chr((b >> 24) & 0xFF)
        s = s + chr(d & 0xFF) + chr((d >> 8) & 0xFF) + chr((d >> 16) & 0xFF) + chr((d >> 24) & 0xFF)
        s = s + chr(c & 0xFF) + chr((c >> 8) & 0xFF) + chr((c >> 16) & 0xFF) + chr((c >> 24) & 0xFF)
        return s

    def has_model_string(self):
        return self.max_ext_leaf >= 0x80000004

    def model_string(self):
        if self.has_model_string():
            return self.chunk(0x80000002) + self.chunk(0x80000003) + self.chunk(0x80000004)
        return "unknown"

    def has_sse(self):
        return ((self.feature_edx >> 25) & 1) == 1

    def has_sse2(self):
        return ((self.feature_edx >> 26) & 1) == 1

    def has_sse3(self):
        return (self.feature_ecx & 1) == 1

    def __str__(self):
        if self.has_model_string():
            return self.vendor_id() + " - " + self.model_string()
        return self.vendor_id()

    def print_info(self):
        print_satanic("Vendor ID:")
        print_satanic(self.vendor_id())
        if self.has_model_string():
            print_satanic("CPU model:")
            print_satanic(self.model_string())
        else:
            print_satanic("CPU model string not supported")
        print_satanic("Has SSE: " + bool_to_str(self.has_sse()))
        print_satanic("Has SSE2: " + bool_to_str(self.has_sse2()))
        print_satanic("Has SSE3: " + bool_to_str(self.has_sse3()))
        print_satanic("CPU: " + self.__str__())
