/* Linker script for PyCk freestanding x86 kernel (Multiboot1 ELF).
 * Targets i386 flat binary.
 */

OUTPUT_FORMAT("elf32-i386")
OUTPUT_ARCH(i386)
ENTRY(_start)

SECTIONS
{
    . = 0x01000000;

    .text ALIGN(4K) :
    {
        *(.multiboot_header)
        *(.text)
        *(.text.*)
    }

    .rodata ALIGN(4K) :
    {
        *(.rodata)
        *(.rodata.*)
    }

    .data ALIGN(4K) :
    {
        *(.data)
        *(.data.*)
    }

    .bss ALIGN(4K) :
    {
        *(.bss)
        *(.bss.*)
        *(COMMON)
    }

    _bss_start = ADDR(.bss);
    _bss_end = .;
    _kernel_end = .;
}
