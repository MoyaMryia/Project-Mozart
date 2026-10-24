# 演示彩排与应急手册（M6）

对应总计划 v1 的 M6。前置：M2/M3 的部署重启已执行（见
[reports/m2-dual-stream-20261025/RESULTS.md](../reports/m2-dual-stream-20261025/RESULTS.md)）。

## 彩排清单（每轮 15 分钟，跑 3 轮）

1. 冷启动：两机重启后 45 秒内 USB 桥自愈（计数恢复出帧）。
2. 预处理启动：`mozart-pre --usb-endpoint /dev/ffs-mozart --mode direct --ctl-port 19040`
   （M3 形态）或 M2 形态的 aloop 设备参数。
3. 延迟实测：`status` 的 `direct_us` + 主机回录互相关，记录中位数与 p99。
   验收：≤10 ms（阶段 ≤12 ms）、p99 − 中位 ≤1 ms。
4. 降噪演示：播放风扇/哼声混音，`stage denoise on`，A/B 试听。
5. AI 演示：后端起流，`mode ai`，确认变声与字幕。
6. 切换压力：direct↔ai 连续切换 20 次，无爆音、无断流。
7. 拔线恢复：USB-C 拔插 3 次，帧计数继续增长。
8. 断网验证：拔网线后全流程跑通（单线演示）。

## 应急路径

| 故障 | 现象 | 处置 |
| --- | --- | --- |
| 桥客户端崩溃 | 无声 | `sudo systemctl restart mozart-usb-audio`（两端）；45 秒自愈 |
| 预处理崩溃 | 无声、MZRT 停 | 重跑第 2 步命令；`mode direct` 兜底可听 |
| 控制通道失联 | 切换无效 | 重启预处理；期间直出/AI 状态保持 |
| 后端崩溃 | 无变声 | 预处理 `mode direct` 保底人声；重启 state daemon |
| 延迟劣化 | 回声感 | 查 `direct_us` 遥测；重启预处理清状态 |
| xrun 风暴 | 爆音 | `--period-frames 256` 降中断率，记录现场后复位 |

## 记录模板

每轮记录：日期、轮次、延迟中位/p99、切换次数、拔插次数、异常与处置、
试听结论。三轮全过即 M6 验收。
