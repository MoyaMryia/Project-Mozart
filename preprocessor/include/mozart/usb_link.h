// FunctionFS USB 音频链路。
#ifndef MOZART_USB_LINK_H
#define MOZART_USB_LINK_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mozart_usblink mozart_usblink_t;

mozart_usblink_t *mozart_usblink_open(const char *ffs_dir);
int mozart_usblink_read_audio(mozart_usblink_t *link, int16_t *stereo, int capacity_frames);
int mozart_usblink_write_audio(mozart_usblink_t *link, const int16_t *stereo, int frames);
uint64_t mozart_usblink_rx_frames(const mozart_usblink_t *link);
uint64_t mozart_usblink_tx_frames(const mozart_usblink_t *link);
int mozart_usblink_block_frames(const mozart_usblink_t *link);
uint64_t mozart_usblink_generation(const mozart_usblink_t *link);
bool mozart_usblink_stop_requested(const mozart_usblink_t *link);
int mozart_usblink_finish(mozart_usblink_t *link);
size_t mozart_usblink_pending_frames(const mozart_usblink_t *link);
bool mozart_usblink_negotiated(const mozart_usblink_t *link);
const char *mozart_usblink_last_error(const mozart_usblink_t *link);
void mozart_usblink_close(mozart_usblink_t *link);

#ifdef __cplusplus
}
#endif
#endif
