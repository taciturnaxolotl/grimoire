#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include "xovi.h"

void registerGrimoire();
extern char *program_invocation_short_name;

static int is_worker = 0;

int isGrimoireWorker() { return is_worker; }

void _xovi_construct() {
    if (strstr(program_invocation_short_name, "worker") != NULL) {
        is_worker = 1;
        return;
    }
    printf("[grimoire] Main process (%s), registering\n", program_invocation_short_name);
    registerGrimoire();
}
