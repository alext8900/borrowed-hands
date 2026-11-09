/* Hostile probe helper, compiled by probe.sh at build time with the job's own clang.
   usage: ipc <shm|sem|sysvshm|sysvsem|sysvmsg|groups> [keep] ; exit 0 = the operation worked. */
#include <fcntl.h>
#include <semaphore.h>
#include <stdio.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/mman.h>
#include <sys/msg.h>
#include <sys/sem.h>
#include <sys/shm.h>
#include <unistd.h>
int main(int argc, char **argv) {
    char name[64];
    snprintf(name, sizeof name, "/bhprobe.%d", (int)getpid());
    if (argc < 2) return 2;
    if (!strcmp(argv[1], "shm")) {
        int fd = shm_open(name, O_CREAT | O_RDWR, 0600);
        if (fd < 0) return 1;
        close(fd); shm_unlink(name); return 0;
    }
    if (!strcmp(argv[1], "sem")) {
        sem_t *s = sem_open(name, O_CREAT, 0600, 1);
        if (s == SEM_FAILED) return 1;
        sem_close(s); sem_unlink(name); return 0;
    }
    if (!strcmp(argv[1], "sysvshm")) {
        /* fixed key: a segment like this outlives the process and could carry data between jobs */
        int id = shmget((key_t)0x42480001, 4096, IPC_CREAT | 0600);
        if (id < 0) return 1;
        char *p = shmat(id, 0, 0);
        int ok = p != (char *)-1;
        if (ok) { p[0] = 'x'; shmdt(p); }
        if (argc < 3) shmctl(id, IPC_RMID, 0);
        return ok ? 0 : 1;
    }
    if (!strcmp(argv[1], "sysvsem")) {
        int id = semget((key_t)0x42480002, 1, IPC_CREAT | 0600);
        if (id < 0) return 1;
        struct sembuf op = {0, 1, 0};
        int ok = semop(id, &op, 1) == 0;
        if (argc < 3) semctl(id, 0, IPC_RMID);
        return ok ? 0 : 1;
    }
    if (!strcmp(argv[1], "sysvmsg")) {
        int id = msgget((key_t)0x42480003, IPC_CREAT | 0600);
        if (id < 0) return 1;
        struct { long mtype; char mtext[8]; } m = {1, "bhprobe"};
        int ok = msgsnd(id, &m, sizeof m.mtext, IPC_NOWAIT) == 0;
        if (argc < 3) msgctl(id, IPC_RMID, 0);
        return ok ? 0 : 1;
    }
    if (!strcmp(argv[1], "groups")) {   /* kernel credential groups of this process */
        gid_t g[64]; int n = getgroups(64, g);
        for (int i = 0; i < n; i++) printf("%s%d", i ? "," : "", (int)g[i]);
        printf("\n"); return 0;
    }
    return 2;
}
