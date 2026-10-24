// test_usb_link.c — 快流端点客户端的帧编解码测试
// ============================================================================
// 包含 usb_link.c 直接测试内部解析；golden 帧由 usb-audio/proto.py 生成
// （跨语言对拍：C 解析器必须正确解 Python 参考编码的帧）。
#include "../src/usb_link.c"

#include <stdio.h>
#include <string.h>

#define CHECK(cond) do { \
    if (!(cond)) { \
        fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        failures++; \
    } \
} while (0)

int main(void)
{
    int failures = 0;

    // ---- golden 帧（proto.py 生成，seq=5，ts=0x1122334455667788）----
    static const unsigned char golden[] = {
        0x5a, 0x4d, 0x01, 0x00, 0x02, 0x00, 0x00, 0x00,
        0x05, 0x00, 0x00, 0x00, 0x88, 0x77, 0x66, 0x55,
        0x44, 0x33, 0x22, 0x11, 0x00, 0x02, 0x00, 0x00,
    };
    mozart_usblink_t link = {0};
    int16_t stereo[256];
    // payload = (i*7+3) % 256 作为 S16 小端字节
    unsigned char payload[512];
    for (int i = 0; i < 512; i++) payload[i] = (unsigned char)((i * 7 + 3) % 256);

    memcpy(link.pending, golden, sizeof(golden));
    memcpy(link.pending + sizeof(golden), payload, sizeof(payload));
    link.pending_len = sizeof(golden) + sizeof(payload);
    const int got = ul_pop_frame(&link, stereo, 128);
    CHECK(got == 128);
    CHECK(link.rx_frames == 1);
    CHECK(link.pending_len == 0);
    CHECK(memcmp(stereo, payload, sizeof(payload)) == 0);

    // ---- 未知流跳过：STREAM_AUDIO 帧不消费输出，仅前进缓冲 ----
    unsigned char legacy[24 + 3840];
    memset(legacy, 0, sizeof(legacy));
    legacy[0] = 0x5a; legacy[1] = 0x4d; legacy[2] = 0x01;
    legacy[3] = 0x00; legacy[4] = 0x00;                  // PCM + STREAM_AUDIO
    uint32_t legacy_len = 3840;
    memcpy(legacy + 20, &legacy_len, 4);
    memcpy(link.pending, legacy, sizeof(legacy));
    link.pending_len = sizeof(legacy);
    CHECK(ul_pop_frame(&link, stereo, 256) == 0);
    CHECK(link.pending_len == 0);       // 帧被跳过
    CHECK(link.rx_frames == 1);         // 计数不增

    // ---- 编码往返：C 打包 → C 解析 ----
    unsigned char frame[24 + 512];
    ul_pack_header(frame, 42, 512);
    CHECK(frame[4] == UL_STREAM_FAST);
    uint32_t decoded_len;
    memcpy(&decoded_len, frame + 20, 4);
    CHECK(decoded_len == 512);
    for (int i = 0; i < 512; i++) frame[24 + i] = (unsigned char)(i % 251);
    memcpy(link.pending, frame, sizeof(frame));
    link.pending_len = sizeof(frame);
    CHECK(ul_pop_frame(&link, stereo, 128) == 128);
    CHECK(memcmp(stereo, frame + 24, 512) == 0);

    // ---- 不完整帧返回 0（等待更多数据）----
    link.pending_len = 30;
    memcpy(link.pending, frame, 30);
    CHECK(ul_pop_frame(&link, stereo, 128) == 0);

    if (failures) {
        fprintf(stderr, "test_usb_link: %d 项失败\n", failures);
        return 1;
    }
    printf("[OK] test_usb_link\n");
    return 0;
}
