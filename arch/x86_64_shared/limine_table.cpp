/* A single table of every Limine request this kernel makes, shared by
 * both Limine-booted x86_64 targets (x86_64 and x86_64_graphical).
 *
 * Every Limine request struct -- regardless of feature -- starts with
 * the exact same header:
 *
 *   uint64_t id[4];       // +0  a 4-word magic value, unique per feature
 *   uint64_t revision;    // +32
 *   <response type>* response;  // +40, written by Limine before our
 *                                  kernel's entry point is ever called
 *
 * (see limine.h's struct definitions for memmap/hhdm/rsdp/etc. -- every
 * one of them opens with exactly these three fields in this order).
 * That uniform shape is what makes ONE generic mechanism possible: PyCk
 * exposes exactly one builtin, _LIMINE(), returning the address of a
 * null-terminated array of request addresses. PyCk source (see
 * PythonOS/kernel/) walks that array itself, reads each entry's id via
 * peek64, and compares against the feature's known magic id to find the
 * one it wants -- there is no per-feature builtin, and adding a new
 * Limine feature later means adding one request declaration and one
 * table entry here, nothing else.
 */
#include "limine.h"
#include <stdint.h>

__attribute__((used, section(".limine_requests")))
static volatile struct limine_memmap_request memmap_request = {
    .id = LIMINE_MEMMAP_REQUEST_ID,
    .revision = 0,
};

__attribute__((used, section(".limine_requests")))
static volatile struct limine_hhdm_request hhdm_request = {
    .id = LIMINE_HHDM_REQUEST_ID,
    .revision = 0,
};

__attribute__((used, section(".limine_requests")))
static volatile struct limine_rsdp_request rsdp_request = {
    .id = LIMINE_RSDP_REQUEST_ID,
    .revision = 0,
};

__attribute__((used, section(".limine_requests")))
static volatile struct limine_executable_address_request executable_address_request = {
    .id = LIMINE_EXECUTABLE_ADDRESS_REQUEST_ID,
    .revision = 0,
};

__attribute__((used, section(".limine_requests")))
static volatile struct limine_bootloader_info_request bootloader_info_request = {
    .id = LIMINE_BOOTLOADER_INFO_REQUEST_ID,
    .revision = 0,
};

__attribute__((used, section(".limine_requests")))
static volatile struct limine_firmware_type_request firmware_type_request = {
    .id = LIMINE_FIRMWARE_TYPE_REQUEST_ID,
    .revision = 0,
};

/* Modules: revision 0 is enough for a module supplied via limine.conf's
 * own module_path directive (see PythonOS/build.py) -- Limine populates
 * .response->modules[] the same way regardless of whether a module got
 * there via module_path or the newer internal_module_count/
 * internal_modules request-revision-1 fields, which this kernel doesn't
 * use. */
__attribute__((used, section(".limine_requests")))
static volatile struct limine_module_request module_request = {
    .id = LIMINE_MODULE_REQUEST_ID,
    .revision = 0,
};

/* Framebuffer: only x86_64_graphical actually has a framebuffer console,
 * and its entry.cpp needs this same request's response at boot (before
 * pyck_main() ever runs) to set up graphics_init(). Limine panics
 * ("Conflict detected for request ID") if the same request ID is
 * declared twice, so this can't just be a second, separate request the
 * way every other feature above is -- it's *not* static, and
 * arch/x86_64_graphical/entry.cpp `extern`s this exact symbol instead of
 * declaring its own. On the plain x86_64 (serial-only) target, nothing
 * references the extern and Limine's answer just sits unused in the
 * table. */
__attribute__((used, section(".limine_requests")))
volatile struct limine_framebuffer_request framebuffer_request = {
    .id = LIMINE_FRAMEBUFFER_REQUEST_ID,
    .revision = 0,
};

/* Null-terminated so PyCk source can walk it without also needing a
 * separate "how many entries" builtin. */
static void* const limine_request_table[] = {
    (void*)&memmap_request,
    (void*)&hhdm_request,
    (void*)&rsdp_request,
    (void*)&executable_address_request,
    (void*)&bootloader_info_request,
    (void*)&firmware_type_request,
    (void*)&framebuffer_request,
    (void*)&module_request,
    nullptr,
};

extern "C" uint64_t _pyck_limine_table_addr(void) {
    return (uint64_t)(uintptr_t)&limine_request_table[0];
}
