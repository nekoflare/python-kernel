#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>

/* A real (if minimal) shell: a prompt, a blocking read, a few built-in
 * commands handled inline, and -- for anything else -- an actual
 * fork()+execve()+wait4() to launch a separate binary out of the
 * initramfs (/bin/<command>), the same way a real shell launches
 * external programs. */

static void run_builtin(const char *line) {
    if (strcmp(line, "help") == 0) {
        printf("commands: help, echo <text>, about, exit -- anything else "
               "is exec'd as /bin/<name>\n");
        return;
    }
    if (strcmp(line, "about") == 0) {
        printf("PythonOS -- a kernel written in Python (PyCk), running real "
               "static glibc binaries in ring3, with real fork()/execve().\n");
        return;
    }
    if (strncmp(line, "echo ", 5) == 0) {
        printf("%s\n", line + 5);
        return;
    }
    if (strcmp(line, "echo") == 0) {
        printf("\n");
        return;
    }
    printf("unknown builtin (trying to exec it): %s\n", line);
}

static int is_builtin(const char *line) {
    return strcmp(line, "help") == 0 || strcmp(line, "about") == 0 ||
           strncmp(line, "echo ", 5) == 0 || strcmp(line, "echo") == 0;
}

static void run_external(const char *line) {
    char path[300];
    snprintf(path, sizeof(path), "/bin/%s", line);

    pid_t pid = fork();
    if (pid < 0) {
        printf("fork failed\n");
        return;
    }
    if (pid == 0) {
        char *argv[] = {path, NULL};
        char *envp[] = {NULL};
        execve(path, argv, envp);
        printf("exec failed: %s\n", line);
        _exit(127);
    }
    int status = 0;
    waitpid(pid, &status, 0);
    printf("[pid %d exited with status %d]\n", (int)pid, WEXITSTATUS(status));
}

int main(void) {
    printf("PythonOS shell. Type 'help' for commands, 'exit' to quit.\n");

    char line[256];
    for (;;) {
        printf("$ ");
        if (fgets(line, sizeof(line), stdin) == NULL) {
            break;
        }
        size_t len = strlen(line);
        if (len > 0 && line[len - 1] == '\n') {
            line[len - 1] = '\0';
        }
        if (line[0] == '\0') {
            continue;
        }
        if (strcmp(line, "exit") == 0 || strcmp(line, "quit") == 0) {
            printf("bye\n");
            break;
        }
        if (is_builtin(line)) {
            run_builtin(line);
        } else {
            run_external(line);
        }
    }
    return 0;
}
