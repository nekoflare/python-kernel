#include <stdio.h>

/* A second, separate binary -- packaged into the initramfs as
 * /bin/child (see PythonOS/build.py) -- so the shell (apps/hello/
 * hello.c) has something real to fork()+execve() to prove those
 * syscalls actually launch an independent program, not just built-in
 * commands handled inline. */

int main(int argc, char **argv) {
    printf("child process running (argv[0]=%s)\n", argc > 0 ? argv[0] : "?");
    return 42;
}
