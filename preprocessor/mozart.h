// mozart.h — preprocessor 公共头（伞头文件）
// ============================================================================
// 下游只需 include 本文件。契约帧类型来自 IO/include/mozart/frame_meta.h
// （唯一定义源）。
#ifndef MOZART_H
#define MOZART_H

#include "mozart/frame_meta.h"
#include "mozart/rnnoise.h"
#include "mozart/dsp.h"
#include "mozart/capture.h"
#include "mozart/wav.h"

#define MOZART_PRE_VERSION "0.2.0"

#endif /* MOZART_H */
