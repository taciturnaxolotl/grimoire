/* uinject.c — Inject pen strokes via evdev for reMarkable 2 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <linux/input.h>
#include <sys/ioctl.h>
#include <errno.h>

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
/* RM v6: x[-702,701] y[0,1871], screen portrait 1404x1872 */
/* Wacom digitizer: x[0,20966] y[0,15725], landscape orientation */
/* 90° CCW rotation: wacom_x = (screen_height - screen_y) * scale, wacom_y = screen_x * scale */
static void rm_to_wacom(float rm_x, float rm_y, int *wx, int *wy) {
    float screen_x = rm_x + 702.0f;  /* 0..1403 */
    float screen_y = rm_y;            /* 0..1871 */
    /* Rotate 90° CCW and scale to Wacom range */
    *wx = (int)((1872.0f - screen_y) * 20966.0f / 1872.0f);
    *wy = (int)(screen_x * 15725.0f / 1404.0f);
}

static int rm_pressure_to_wacom(int rm_pressure) {
    /* RM pressure: 0-255, Wacom: 0-4095 */
    return rm_pressure * 4095 / 255;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <strokes.json>\n", argv[0]);
        return 1;
    }

    /* Write directly to the real Wacom digitizer device */
    const char *dev_path = "/dev/input/event1";
    int fd = open(dev_path, O_WRONLY);
    if (fd < 0) {
        perror("open /dev/input/event1");
        return 1;
    }
    fprintf(stderr, "[uinject] Writing to %s\n", dev_path);

    /* Read JSON file - simple parser for our known format */
    FILE *fp = fopen(argv[1], "r");
    if (!fp) {
        perror("fopen");
        close(fd);
        return 1;
    }

    /* Read entire file */
    fseek(fp, 0, SEEK_END);
    long fsize = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    char *json = malloc(fsize + 1);
    fread(json, 1, fsize, fp);
    json[fsize] = '\0';
    fclose(fp);

    fprintf(stderr, "[uinject] Loaded %ld bytes from %s\n", fsize, argv[1]);

    /* Simple JSON parsing - find each stroke's points array */
    /* Format: [{"points":[[x,y,speed,width,direction,pressure],...], ...}, ...] */
    char *p = json;
    int stroke_count = 0;

    while ((p = strstr(p, "\"points\"")) != NULL) {
        p = strchr(p, '[');  /* Find start of points array */
        if (!p) break;
        p++; /* Skip [ */

        /* Pen down */
        emit_event(fd, EV_KEY, BTN_TOOL_PEN, 1);
        emit_event(fd, EV_KEY, BTN_TOUCH, 1);

        int point_count = 0;
        while (*p && *p != ']') {
            /* Parse [x, y, speed, width, direction, pressure] */
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

                /* Small delay between points (~5ms) */
                usleep(5000);
            }

            /* Advance to next point */
            p = strchr(p, ']');
            if (p) p++;
            /* Skip comma and whitespace */
            while (*p && (*p == ',' || *p == ' ' || *p == '\n' || *p == '\r')) p++;
        }

        /* Pen up */
        emit_event(fd, EV_ABS, ABS_PRESSURE, 0);
        emit_event(fd, EV_ABS, ABS_DISTANCE, 86);
        emit_syn(fd);
        emit_event(fd, EV_KEY, BTN_TOUCH, 0);
        emit_event(fd, EV_KEY, BTN_TOOL_PEN, 0);
        emit_syn(fd);

        stroke_count++;
        fprintf(stderr, "[uinject] Stroke %d: %d points\n", stroke_count, point_count);

        /* Small delay between strokes */
        usleep(50000);
    }

    fprintf(stderr, "[uinject] Done: %d strokes injected\n", stroke_count);

    free(json);
    close(fd);

    return 0;
}
