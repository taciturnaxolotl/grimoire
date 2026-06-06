/* uinject.c — Inject pen strokes or erase along paths via evdev for reMarkable 2 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <linux/input.h>
#include <sys/ioctl.h>
#include <errno.h>

static int g_delay_us = 5000;  /* default 5ms between points */
static int g_verbose = 0;      /* -v enables info logging to stderr */

#define vlog(...) do { if (g_verbose) fprintf(stderr, __VA_ARGS__); } while (0)

static void emit_event(int fd, int type, int code, int value) {
    struct input_event ev;
    memset(&ev, 0, sizeof(ev));
    ev.type = type;
    ev.code = code;
    ev.value = value;
    write(fd, &ev, sizeof(ev));
}

static void emit_syn(int fd) {
    emit_event(fd, EV_SYN, SYN_REPORT, 0);
}

/* Convert .rm v6 coordinates to Wacom digitizer coordinates */
static void rm_to_wacom(float rm_x, float rm_y, int *wx, int *wy) {
    float screen_x = rm_x + 702.0f;
    float screen_y = rm_y;
    *wx = (int)((1872.0f - screen_y) * 20966.0f / 1872.0f);
    *wy = (int)(screen_x * 15725.0f / 1404.0f);
}

static int rm_pressure_to_wacom(int rm_pressure) {
    return rm_pressure * 4095 / 255;
}

/* Erase by replaying stroke paths with the eraser tool.
 * Makes multiple passes with slight offsets for full coverage. */
static int do_erase_strokes(int fd, const char *json, long fsize) {
    vlog("[uinject] Erasing along stroke paths\n");

    /* Offset patterns for multi-pass erase (x,y in rm coords).
     * The eraser footprint is much wider than the swirl's pen width,
     * so three tight passes give full coverage without the extra time
     * (and cutoff risk) of a six-pass sweep. */
    static const float offsets[][2] = {
        {0, 0}, {-6, 0}, {6, 0},
    };
    int num_passes = sizeof(offsets) / sizeof(offsets[0]);

    int total_emitted = 0;

    for (int pass = 0; pass < num_passes; pass++) {
        float ox = offsets[pass][0];
        float oy = offsets[pass][1];
        char *p = json;  /* Reset to start of JSON for each pass */
        vlog("[uinject] Pass %d/%d\n", pass+1, num_passes);
        int pass_emitted = 0;

        while ((p = strstr(p, "\"points\"")) != NULL) {
            p = strchr(p, '[');
            if (!p) break;
            p++;

            /* Peek first point for hover frame */
            float hx, hy;
            int hs, hw, hd, hp;
            char *saved_p = p;
            if (sscanf(p, "[%f,%f,%d,%d,%d,%d]", &hx, &hy, &hs, &hw, &hd, &hp) == 6) {
                int hwx, hwy;
                rm_to_wacom(hx + ox, hy + oy, &hwx, &hwy);
                emit_event(fd, EV_KEY, BTN_TOOL_RUBBER, 1);
                emit_event(fd, EV_ABS, ABS_X, hwx);
                emit_event(fd, EV_ABS, ABS_Y, hwy);
                emit_event(fd, EV_ABS, ABS_DISTANCE, 60);
                emit_syn(fd);
                usleep(2000);
            } else {
                emit_event(fd, EV_KEY, BTN_TOOL_RUBBER, 1);
                emit_event(fd, EV_ABS, ABS_DISTANCE, 60);
                emit_syn(fd);
                usleep(2000);
            }
            p = saved_p;
            emit_event(fd, EV_KEY, BTN_TOUCH, 1);

            int bracket_depth = 1;  /* we're inside the outer [ */
            float last_x = 1e9f, last_y = 1e9f;
            const float min_dist_sq = 15.0f * 15.0f;

            while (*p && bracket_depth > 0) {
                if (*p == '[') {
                    /* Start of a point: [x,y,speed,width,direction,pressure] */
                    float x, y;
                    int speed, width, direction, pressure;
                    if (sscanf(p, "[%f,%f,%d,%d,%d,%d]", &x, &y, &speed, &width, &direction, &pressure) == 6) {
                        float dx = (x + ox) - last_x;
                        float dy = (y + oy) - last_y;
                        if (dx*dx + dy*dy >= min_dist_sq || pass_emitted == 0) {
                            int wx, wy;
                            rm_to_wacom(x + ox, y + oy, &wx, &wy);

                            emit_event(fd, EV_ABS, ABS_X, wx);
                            emit_event(fd, EV_ABS, ABS_Y, wy);
                            emit_event(fd, EV_ABS, ABS_PRESSURE, 4095);
                            emit_event(fd, EV_ABS, ABS_DISTANCE, 0);
                            emit_syn(fd);

                            last_x = x + ox;
                            last_y = y + oy;
                            pass_emitted++;
                            total_emitted++;
                            usleep(5000);  /* 5ms between points */
                        }
                    }
                    /* Skip past this inner [...] */
                    char *close = strchr(p + 1, ']');
                    if (close) p = close + 1;
                    else break;
                } else if (*p == ']') {
                    bracket_depth--;
                    if (bracket_depth <= 0) break;
                    p++;
                } else {
                    p++;
                }
            }

            emit_event(fd, EV_ABS, ABS_PRESSURE, 0);
            emit_event(fd, EV_ABS, ABS_DISTANCE, 86);
            emit_syn(fd);
            emit_event(fd, EV_KEY, BTN_TOUCH, 0);
            emit_event(fd, EV_KEY, BTN_TOOL_RUBBER, 0);
            emit_syn(fd);

            usleep(10000);  /* 10ms between strokes */
        }
        vlog("[uinject] Pass %d emitted %d points\n", pass+1, pass_emitted);
    }

    vlog("[uinject] Erased %d passes (total emitted=%d)\n", num_passes, total_emitted);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s [--speed MS] <strokes.json>\n", argv[0]);
        fprintf(stderr, "       %s [--speed MS] --erase-strokes <strokes.json>\n", argv[0]);
        fprintf(stderr, "       %s --erase x,y,w,h\n", argv[0]);
        fprintf(stderr, "  --speed MS   Delay between points in ms (default: 5)\n");
        fprintf(stderr, "  -v           Verbose: print progress info to stderr\n");
        return 1;
    }

    /* Parse flags anywhere in args */
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--speed") == 0 && i + 1 < argc) {
            g_delay_us = atoi(argv[i + 1]) * 1000;
            if (g_delay_us < 0) g_delay_us = 0;
            for (int j = i; j + 2 < argc; j++) argv[j] = argv[j + 2];
            argc -= 2; i--;
        } else if (strcmp(argv[i], "-v") == 0 || strcmp(argv[i], "--verbose") == 0) {
            g_verbose = 1;
            for (int j = i; j + 1 < argc; j++) argv[j] = argv[j + 1];
            argc -= 1; i--;
        }
    }

    const char *dev_path = "/dev/input/event1";
    int fd = open(dev_path, O_WRONLY);
    if (fd < 0) {
        perror("open /dev/input/event1");
        return 1;
    }
    vlog("[uinject] Writing to %s (delay=%dus)\n", dev_path, g_delay_us);

    /* Region erase mode */
    if (strcmp(argv[1], "--erase") == 0 && argc >= 3) {
        float ex, ey, ew, eh;
        if (sscanf(argv[2], "%f,%f,%f,%f", &ex, &ey, &ew, &eh) != 4) {
            fprintf(stderr, "[uinject] Bad erase region format. Use: x,y,w,h\n");
            close(fd);
            return 1;
        }
        vlog("[uinject] Erasing region: x=%.0f y=%.0f w=%.0f h=%.0f\n", ex, ey, ew, eh);

        float step = 8.0f;
        int wx, wy;
        /* Hover to start position before touching down */
        rm_to_wacom(ex, ey, &wx, &wy);
        emit_event(fd, EV_KEY, BTN_TOOL_RUBBER, 1);
        emit_event(fd, EV_ABS, ABS_X, wx);
        emit_event(fd, EV_ABS, ABS_Y, wy);
        emit_event(fd, EV_ABS, ABS_DISTANCE, 60);
        emit_syn(fd);
        usleep(2000);
        emit_event(fd, EV_KEY, BTN_TOUCH, 1);

        for (float row = ey; row <= ey + eh; row += step) {
            rm_to_wacom(ex, row, &wx, &wy);
            emit_event(fd, EV_ABS, ABS_X, wx);
            emit_event(fd, EV_ABS, ABS_Y, wy);
            emit_event(fd, EV_ABS, ABS_PRESSURE, 4095);
            emit_event(fd, EV_ABS, ABS_DISTANCE, 0);
            emit_syn(fd);

            rm_to_wacom(ex + ew, row, &wx, &wy);
            emit_event(fd, EV_ABS, ABS_X, wx);
            emit_event(fd, EV_ABS, ABS_Y, wy);
            emit_event(fd, EV_ABS, ABS_PRESSURE, 4095);
            emit_event(fd, EV_ABS, ABS_DISTANCE, 0);
            emit_syn(fd);

            usleep(g_delay_us);
        }

        emit_event(fd, EV_ABS, ABS_PRESSURE, 0);
        emit_event(fd, EV_ABS, ABS_DISTANCE, 86);
        emit_syn(fd);
        emit_event(fd, EV_KEY, BTN_TOUCH, 0);
        emit_event(fd, EV_KEY, BTN_TOOL_RUBBER, 0);
        emit_syn(fd);

        vlog("[uinject] Region erase done\n");
        close(fd);
        return 0;
    }

    /* Stroke-based erase mode */
    if (strcmp(argv[1], "--erase-strokes") == 0 && argc >= 3) {
        FILE *fp = fopen(argv[2], "r");
        if (!fp) { perror("fopen"); close(fd); return 1; }
        fseek(fp, 0, SEEK_END);
        long fsize = ftell(fp);
        fseek(fp, 0, SEEK_SET);
        char *json = malloc(fsize + 1);
        fread(json, 1, fsize, fp);
        json[fsize] = '\0';
        fclose(fp);

        int ret = do_erase_strokes(fd, json, fsize);
        free(json);
        close(fd);
        return ret;
    }

    /* Normal pen injection mode */
    FILE *fp = fopen(argv[1], "r");
    if (!fp) {
        perror("fopen");
        close(fd);
        return 1;
    }

    fseek(fp, 0, SEEK_END);
    long fsize = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    char *json = malloc(fsize + 1);
    fread(json, 1, fsize, fp);
    json[fsize] = '\0';
    fclose(fp);

    vlog("[uinject] Loaded %ld bytes from %s\n", fsize, argv[1]);

    char *p = json;
    int stroke_count = 0;

    while ((p = strstr(p, "\"points\"")) != NULL) {
        p = strchr(p, '[');
        if (!p) break;
        p++;

        /* Peek first point for hover frame */
        float hx, hy;
        int hs, hw, hd, hp;
        if (sscanf(p, "[%f,%f,%d,%d,%d,%d]", &hx, &hy, &hs, &hw, &hd, &hp) == 6) {
            int hwx, hwy;
            rm_to_wacom(hx, hy, &hwx, &hwy);
            emit_event(fd, EV_KEY, BTN_TOOL_PEN, 1);
            emit_event(fd, EV_ABS, ABS_X, hwx);
            emit_event(fd, EV_ABS, ABS_Y, hwy);
            emit_event(fd, EV_ABS, ABS_DISTANCE, 60);
            emit_syn(fd);
            usleep(2000);
        } else {
            emit_event(fd, EV_KEY, BTN_TOOL_PEN, 1);
            emit_event(fd, EV_ABS, ABS_DISTANCE, 60);
            emit_syn(fd);
            usleep(2000);
        }
        emit_event(fd, EV_KEY, BTN_TOUCH, 1);

        int point_count = 0;
        while (*p && *p != ']') {
            float x, y;
            int speed, width, direction, pressure;
            if (sscanf(p, "[%f,%f,%d,%d,%d,%d]", &x, &y, &speed, &width, &direction, &pressure) == 6) {
                int wx, wy;
                rm_to_wacom(x, y, &wx, &wy);
                int wp = rm_pressure_to_wacom(pressure);

                emit_event(fd, EV_ABS, ABS_X, wx);
                emit_event(fd, EV_ABS, ABS_Y, wy);
                emit_event(fd, EV_ABS, ABS_PRESSURE, wp);
                emit_event(fd, EV_ABS, ABS_DISTANCE, 0);
                emit_syn(fd);
                point_count++;

                usleep(g_delay_us);
            }

            p = strchr(p, ']');
            if (p) p++;
            while (*p && (*p == ',' || *p == ' ' || *p == '\n' || *p == '\r')) p++;
        }

        emit_event(fd, EV_ABS, ABS_PRESSURE, 0);
        emit_event(fd, EV_ABS, ABS_DISTANCE, 86);
        emit_syn(fd);
        emit_event(fd, EV_KEY, BTN_TOUCH, 0);
        emit_event(fd, EV_KEY, BTN_TOOL_PEN, 0);
        emit_syn(fd);

        stroke_count++;
        vlog("[uinject] Stroke %d: %d points\n", stroke_count, point_count);

        usleep(50000);
    }

    vlog("[uinject] Done: %d strokes injected\n", stroke_count);

    free(json);
    close(fd);

    return 0;
}
