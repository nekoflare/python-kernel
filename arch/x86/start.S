/* x86 _start stub for PyCk freestanding kernel.
 * Multiboot1 entry point. Sets up stack, BSS, IDT, and calls pyck_main().
 * Then halts.
 */
.section .multiboot_header
.align 4
    .long 0x1BADB002          /* magic */
    .long 0                   /* flags */
    .long -(0x1BADB002 + 0)   /* checksum */

.section .text
.global _start
.extern pyck_main
.extern _bss_start
.extern _bss_end

_start:
    cli
    lea stack_top, %esp

    /* Zero BSS */
    lea _bss_start, %edi
    lea _bss_end, %ecx
    sub %edi, %ecx
    shr $2, %ecx
    xor %eax, %eax
    rep stosl

    /* Mask all IRQs via PIC1 */
    mov $0xFF, %al
    out %al, $0x21

    /* Disable NMI by setting bit 7 of port 0x61 */
    in $0x61, %al
    or $0x80, %al
    out %al, $0x61

    /* Build a full IDT: all 256 entries point to iret stub.
     * This prevents triple-fault from any spurious/APIC interrupt.
     * Each IDT gate descriptor is 8 bytes:
     *   [0..15]  offset low
     *   [16..31] segment selector
     *   [32..39] reserved (0)
     *   [40..47] type + DPL + present
     *   [48..63] offset high
     */
    mov $_idt, %edi
    mov $256, %ecx
    mov $idt_iret_stub, %ebx
.Lfill_idt:
    mov %bx, (%edi)
    movw $0x08, 2(%edi)
    movb $0, 4(%edi)
    movb $0x8E, 5(%edi)
    mov %ebx, %eax
    shr $16, %eax
    movw %ax, 6(%edi)
    movw $0, 8(%edi)
    add $8, %edi
    dec %ecx
    jnz .Lfill_idt

    lidt _idt_desc

    /* Call the Python main function */
    call pyck_main

    /* Halt forever */
halt:
    cli
    hlt
    jmp halt

/* Minimal interrupt stub: just iret */
idt_iret_stub:
    iret

.section .data
.align 4
_idt_desc:
    .word 256 * 8 - 1       /* limit */
    .long _idt               /* base */

.section .bss
.align 4
_idt:
    .space 256 * 8

.align 16
stack_bottom:
    .space 16384
stack_top:
