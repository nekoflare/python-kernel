; Minimal Linux x86-64 "Hello, world!" -- no libc, raw syscalls only.
;
; Build:
;   nasm -f elf64 hello.asm -o hello.o
;   ld hello.o -o hello
; Run:
;   ./hello

default abs

section .data
    msg     db "Hello, world!", 10
    msg_len equ $ - msg

section .text
    global _start

_start:
    ; write(1, msg, msg_len)
    mov     rax, 1          ; syscall: sys_write
    mov     rdi, 1          ; fd: stdout
    lea     rsi, [msg]      ; buf
    mov     rdx, msg_len    ; count
    syscall

    ; exit(0)
    mov     rax, 60         ; syscall: sys_exit
    xor     rdi, rdi        ; status = 0
    syscall
