#define _GNU_SOURCE
#include <dlfcn.h>
#include <scsi/sg.h>
#include <sys/ioctl.h>
#include <stdarg.h>
#include <string.h>

/*
 * LD_PRELOAD shim: intercepts SG_IO ioctls for TEST UNIT READY (opcode 0x00)
 * and patches the response to CHECK CONDITION / NOT READY / MEDIUM NOT PRESENT.
 * This tricks makemkvcon into thinking no disc is present so it proceeds with flashing.
 */

static unsigned char sense_no_medium[] = {
    0x70,       /* response code: current, fixed format */
    0x00,
    0x02,       /* sense key: NOT READY */
    0x00, 0x00, 0x00, 0x00,
    0x0a,       /* additional sense length */
    0x00, 0x00, 0x00, 0x00,
    0x3a,       /* ASC: MEDIUM NOT PRESENT */
    0x00,       /* ASCQ */
    0x00, 0x00, 0x00, 0x00
};

typedef int (*real_ioctl_t)(int fd, unsigned long request, ...);

int ioctl(int fd, unsigned long request, ...) {
    va_list args;
    va_start(args, request);
    void *arg = va_arg(args, void *);
    va_end(args);

    real_ioctl_t real_ioctl = (real_ioctl_t)dlsym(RTLD_NEXT, "ioctl");
    int ret = real_ioctl(fd, request, arg);

    if (request == SG_IO && arg) {
        sg_io_hdr_t *hdr = (sg_io_hdr_t *)arg;
        /* TEST UNIT READY: opcode 0x00, cmd_len 6, direction NONE */
        if (hdr->cmd_len == 6 && hdr->cmdp && hdr->cmdp[0] == 0x00 && hdr->status == 0) {
            hdr->status = 0x02;  /* CHECK CONDITION */
            hdr->masked_status = 0x01;
            if (hdr->sbp && hdr->mx_sb_len >= sizeof(sense_no_medium)) {
                memcpy(hdr->sbp, sense_no_medium, sizeof(sense_no_medium));
                hdr->sb_len_wr = sizeof(sense_no_medium);
            }
        }
    }

    return ret;
}
