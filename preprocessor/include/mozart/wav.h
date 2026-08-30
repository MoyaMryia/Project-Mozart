// wav.h — 离线 WAV 输入（PCM S16），无麦克风时验证整条链路用
#ifndef MOZART_WAV_H
#define MOZART_WAV_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mozart_wav_reader mozart_wav_reader_t;

/** 打开 WAV（仅支持 PCM 16bit）。要求 48 kHz / 双声道，否则返回 NULL。 */
mozart_wav_reader_t *mozart_wav_open(const char *path);

/** 读 frames 帧交错 S16 到 buf，返回实际帧数（0 = EOF）。 */
int mozart_wav_read(mozart_wav_reader_t *r, int16_t *buf, int frames);

/** rewind 到数据起点（循环播放用）。 */
void mozart_wav_rewind(mozart_wav_reader_t *r);

void mozart_wav_close(mozart_wav_reader_t *r);

#ifdef __cplusplus
}
#endif

#endif // MOZART_WAV_H
