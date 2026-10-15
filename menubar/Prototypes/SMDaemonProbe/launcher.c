/* Throwaway: the BundleProgram. Records who launchd ran it as, then execs argv[2...].
   argv[1] is the file to write. */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <time.h>
int main(int argc, char **argv) {
    if (argc < 2) return 2;
    FILE *f = fopen(argv[1], "a");
    if (f) {
        fprintf(f, "time=%ld uid=%d euid=%d gid=%d ppid=%d HOME=%s USER=%s\n", (long)time(NULL),
                getuid(), geteuid(), getgid(), getppid(), getenv("HOME") ? getenv("HOME") : "",
                getenv("USER") ? getenv("USER") : "");
        fclose(f);
    }
    if (argc > 2) { execv(argv[2], argv + 2); perror("execv"); return 1; }
    return 0;
}
