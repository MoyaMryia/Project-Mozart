// usb_link.h — 快流专用 FunctionFS 端点客户端（M3）
// ============================================================================
// mozart-pre 经 --usb-endpoint 直接持有 gadget 的 ep3（OUT，主机→Jetson）
// 与 ep4（IN，Jetson→主机），快流不过守护进程、不过 snd-aloop。
// 帧格式与 usb-audio/proto.py 一致：24 B 头 + PCM 负载；仅接受
// TYPE_PCM + STREAM_FAST。索引对齐：读入帧序与写出内容一一对应。
#ifndef MOZART_USB_LINK_H
#define MOZART_USB_LINK_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mozart_usblink mozart_usblink_t;

/** 打开 <ffs_dir>/ep3 与 <ffs_dir>/ep4。失败返回 NULL（原因打印到 stderr）。 */
mozart_usblink_t *mozart_usblink_open(const char *ffs_dir);

/**
 * 阻塞读取一帧快流音频到 stereo（交错 S16）。
 * @return 帧数（每通道样本数，固定 128），负数为错误。
 */
int mozart_usblink_read_audio(mozart_usblink_t *link, int16_t *stereo, int capacity_frames);

/** 写出一帧快流音频。返回帧数或负数错误。 */
int mozart_usblink_write_audio(mozart_usblink_t *link, const int16_t *stereo, int frames);

uint64_t mozart_usblink_rx_frames(const mozart_usblink_t *link);
uint64_t mozart_usblink_tx_frames(const mozart_usblink_t *link);

void mozart_usblink_close(mozart_usblink_t *link);

#ifdef __cplusplus
}
#endif

#endif // MOZART_USB_LINK_H
