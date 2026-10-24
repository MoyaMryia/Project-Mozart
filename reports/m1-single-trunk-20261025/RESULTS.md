# M1：预处理器单干重构（WOLA 主干 + 直出路由 + 控制通道）

日期：2026-10-25。对应总计划 v1 的 M1 里程碑。

## 交付物

| 模块 | 内容 |
| --- | --- |
| `preprocessor/src/noise_suppressor.c`（+头） | WOLA 谱降噪 C 实现：滑窗分析、噪声通道（慢 EMA + 最小值保持 + 门控上爬）、决策导向维纳增益、首 8 帧均值初始化、流式任意分块 |
| `preprocessor/src/stages.c`（+头） | 整形级：噪声门 / AGC / 峰值限幅，关闭时逐位恒等 |
| `preprocessor/src/control.c`（+头） | 本地 UDP 控制通道：`mode ai\|direct`、`stage denoise\|gate\|agc\|limiter on\|off`（未知名字回错误）、`status`（JSON 快照，含 direct_us 延迟探针） |
| `preprocessor/src/dsp.c` 重构 | 单干链：FL→高通→WOLA→整形级；`enhance`（任意块长）+ `pack_frame`（960→16k 契约）分离；混合 VAD |
| `preprocessor/src/main.c` 重构 | 小周期采集（默认 128）、960 攒帧发送、直出路由（10 ms 淡入淡出）、延迟探针、状态快照 |
| `playback` | 任意帧数写入 + S16 直写 + 写互斥 + 小周期缓冲自动扩展 + underruns 原子化 |
| RNNoise 退出 | 处理链、CLI、配置链（`storage.file_denoise`）、文档全部摘除；vendored 库保留供回退（CI 以 `MOZART_USE_RNNOISE=OFF` 构建，回退路径需显式开启） |

## 验收结果

### 契约与等价

1. **MZRT 契约不变**：帧格式、采样率、字段语义零改动；`test_replay` 13 项全过（含故障注入、定速回放、双发）。
2. **C 对拍 Python 参考**（同配置 128/64/0，3 秒确定性夹具，变长分块喂入）：
   同配置最大绝对差 **3.7e-9**，测试门限 **1e-6**（留三个数量级余量）；
   bypass 通道**逐位一致**（FFT/WOLA/窗零误差）。
   记录：开发期一度以 la=0 对拍 la=1 夹具，残差 3.4e-4——配置错配被审查发现并纠正，
   门限随之下调；该教训已写入夹具注释。
3. **生产路径降噪实证**：dsp 层（`enhance`）对稳态白噪实测 **17.1 dB** 抑制
   （`test_production_path_suppresses_noise` 回归钉死）。
   开发期曾引入"零样本预热"导致噪声估计钉死在 0、增益恒 1——审查实测发现后删除；
   正确语义 = ns 以首帧真实信号初始化，代价为首块输出不满（首帧补零约 12 ms）与
   流起始单 hop 的 1/w 边缘放大（与 Python 参考逐位一致，直出转换时被限幅）。
4. **真实 TTS 语音叠噪电池**（Mimo 三音色 × 五噪声 × 五 SNR，参考实现级）：
   白噪 snr0 改善 +10.9 dB；纯语音透明 44.8/60.0/55.0 dB。
   数据见 [tts-battery.json](tts-battery.json)；语音素材为本会话 API 生成，未入库。

### VAD 分段对比（旧能量/RNNoise VAD vs 新混合 VAD）

由 [tools/vad_segment_compare.py](../../tools/vad_segment_compare.py) 生成，
原始数据 [vad-comparison.json](vad-comparison.json)：

| 素材 | 旧边界（帧） | 新边界（帧） |
| --- | --- | --- |
| 冰糖·女 | 7, 132, 302, 650, 809 | 7, 300, 809 |
| 苏打·男 | 14, 168, 507, 635, 758 | 14, 758 |
| 茉莉·女 | 7, 85, 306, 405, 506, 717 | 8, 85, 717 |

新 VAD 产出的边界全部命中旧边界（差 ≤1 帧），并把 <500 ms 停顿桥接为更长语段。
判据 = WOLA 置信度（功率加权 post-SNR≥2 bin 占比）× 主干电平 > −60 dB；
纯置信度判据在停顿噪声地板处悬浮于退出门限附近（实测不分段），电平门限兜底。

### 延迟

- WOLA 稳态发射延迟 192 样本 = **4.0 ms**（M0 实测，(128,64,0)）。
- 直出路径软件延迟经控制通道 `status.direct_us` 实时暴露（板端实测待部署后执行）。

### 回归

预处理器 4/4 + 新增生产路径抑制测试、原生 28/28、状态 14/14、Python 323/323。
审查修复清单：P0 预热缺陷（上述）、cleanup 未初始化指针（valgrind 证实，声明前置）、
对拍配置错配、printf 参数类型、不可达守卫删除、控制通道未知级名回错、
underruns 原子化、fast-math 对 WOLA 文件豁免、`_Static_assert` 钉 bins 关系、
故障注入测试改双读模式（首块不满量属预期）。

## 已知边界

- 直出与后端回包共用播放设备，切换经 10 ms 淡变衔接，无混音。
- `stage denoise` 运行时切换重建 WOLA 状态（约 0.5 s 收敛期）。
- 流起始单 hop 边缘放大（见验收第 3 条）在直出 S16 转换时限幅为一次可闻点击级瞬态。

## 板端冒烟（提交 ba72b6f 后实测）

Jetson Orin、aloop 直连、10 秒 direct 模式：MZRT 499 帧、直出 2861 周期、
overrun 0；`direct_us` avg 5 µs / max 13 µs；mode 切换生效；SIGTERM 退出码 0。
软件路径合计 ≈ 2.67 ms（period 128）+ 4.0 ms（WOLA）+ 5 µs ≈ **6.7 ms**。
开发期另修复两处：控制表悬空指针（`vt` 按值保存）、后端缺席时回包
ECONNREFUSED 容忍（采纳存档分支 fix/udp-restart-recovery 的洞察）。

## 适用范围

- 配置：本机 x86_64 构建；48 kHz；period 默认 128。aarch64 构建含 fast-math，
  WOLA 文件已豁免，但板端数值对拍未执行。
- 样本：对拍夹具 1 份（3 s，144000 样本）；生产路径探针 1 次（100 块白噪）；
  VAD 对比 3 段 TTS 语音各 900 帧；TTS 电池 3 语音 × 5 噪声 × 5 SNR；
  回归见对应章节分母。
- 板端直出的声学回环实测未做（现有 direct_us 为软件段）。阻塞：缺少回录接线。解除条件：连接回录后量全程。
- 运行时 stage 切换的听感连续性未验证。阻塞：缺少人工试听。解除条件：板端切换并试听记录。
- 长时运行（>30 分钟）稳定性未测。解除条件：随 soak 复测一并执行。
- rvc-golden 历史工件中的 `file_rnnoise` 字样按历史证据保留，未随改名。
