# HK MIC 变声与字幕并行实测

## 结果

HK MIC 的同一份采集音频已同时送入 RVC 和字幕识别。USB PnP Sound Device 播放 RVC 回包。
前端字幕条在桌面视口内可见。字幕连接断开后可以重新连接。

## 通路

```text
HK MIC → mozart-pre → UDP 18101 → RVC → mozart-pre → USB PnP Sound Device
                  └→ UDP 18100 → Zipformer → SenseVoice → 翻译 → SSE → 前端
```

一个 `mozart-pre` 独占采集设备，不使用 `dsnoop`。
字幕启动器使用 `--mode captions --external-capture --no-playback`。
`SUBTITLE_TARGET=127.0.0.1:18100` 启用采集双发。
设备重启脚本从 `pre-devices.json` 恢复字幕目标。

字幕启动器先启动后端和识别服务。随后切换后端到 `rt_rvc`，再启动采集。
后端由字幕启动器管理；采集进程使用独立会话，避免后端进程组清理时误停采集。

## 证据

- 15 秒抓包中，18100 和 18101 各收到 750 包。750 对输入包逐字节一致。
- 同一窗口收到 750 个 RVC 输出包，共 720000 个 48 kHz 样本。
- 最终快照：RVC 接收和发送各 4877 包，完成 406 块，推理错误为 0。
- 预处理播放快照：播放 4750/4750 包，坏包为 0，ALSA 欠载 4 次。
- 字幕状态文件记录 4827 个有效输入包，拒收为 0。栈状态为 `running/capturing`。
- 有声测试记录了“我今天吃了顿饭”。复核文本为“今天吃了顿饭。”，译文为 `A meal was eaten today.`。
- 浏览器阻断 `/api/subtitles` 后显示 `OFFLINE`。解除阻断后自动恢复 `ONLINE`。
- 1600×900 视口中，字幕条纵坐标为 444.16–515.84 px。
- 2455×534 视口中，字幕条纵坐标为 261.16–332.84 px。

Jetson 证据位于 `~/mozart-archive/logs/dual-20261028/`。
有声字幕和分句录音位于 `~/mozart-archive/logs/live4/`。
最终运行日志位于 `~/mozart-archive/logs/live6/`。
本机浏览器截图位于 `~/Projects/Project-Mozart-backups/live-caption-browser/`。

## 调查修正

旧版 STT 只在 socket 超时时打印包计数。连续收包时，最后一条 `packets=0` 会保留。
这条旧日志记录的是最近一次收包超时的计数。新版日志在持续收包时也周期更新，并记录拒收数。
外部采集模式通过本次实例的有效音频状态确认采集，缺少音频时先等待，超时后失败。

主分支曾缺少启动脚本已经使用的 `--status-file` 参数。
本次补齐源码实现，并用源码构建的二进制验证状态、失败退出和双发。

## 审查处理

已修复旧状态误判、状态所属进程校验、测试清理误杀风险，以及状态写失败测试未检查提前停止的问题。
状态文件写入失败会停止对应进程，避免继续运行并暴露旧状态。
损坏的内部进程状态会触发启动器失败，不将程序错误降级为正常采集。
SSE 构造使用固定同源地址；构造异常继续向上传递，网络错误走主动重连。

## 适用范围

- 配置：Jetson Orin Nano Super 8 GB；qiqi-zh；音高偏移 11；索引比例 0.5；nprobe 16。
- 采集：`plughw:CARD=MIC,DEV=0`，48 kHz、双声道、S16_LE、period 64。
- 播放：`plughw:CARD=Device,DEV=0`，48 kHz、双声道、S16_LE。
- 样本：1 个 15 秒双路抓包窗口；1 组现场有声字幕记录；2 个视口；1 次浏览器断线恢复。
- 验证内容：并行通路、设备重启、状态文件和页面显示。识别质量记录见[字幕报告](../subtitle-20261027/RESULTS.md)。
