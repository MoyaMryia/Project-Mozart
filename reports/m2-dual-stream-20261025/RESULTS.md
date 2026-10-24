# M2：USB 桥协议双流（STREAM_FAST）

日期：2026-10-25。对应总计划 v1 的 M2 里程碑。

## 交付物

| 模块 | 内容 |
| --- | --- |
| `usb-audio/proto.py` | `STREAM_FAST=2`、`FAST_SAMPLES=128`（512 B/2.67 ms）、`FAST_SILENCE_FRAMES`；帧头零改动 |
| `usb-audio/jetson-daemon.py` | 快流泵：arecord/aplay 128 样本周期（`--period-time 2667`），上行按 STREAM_FAST 封帧；下行解复用接受双流、序号按流独立；`MOZART_FAST=0` 回退 20 ms 兼容模式 |
| `usb-audio/host-daemon.py` | 直连模式 `MOZART_DIRECT=1`：物理设备（`MOZART_DIRECT_MIC/SPEAKER`）小周期直连 + 快流收发；默认 aloop 模式行为不变，且解复用同样接受双流 |
| `tools/tests/test_usb_audio_proto.py` | 快流常量、双流交错独立序号、未知流忽略语义共 4 项新增 |

兼容性：老对端按协议设计忽略未知流（单测钉死该语义），20 ms 流全链保留。

## 验收状态

- 协议单测 10/10 通过（含新增 4 项）。
- 双端守护进程语法与导入验证通过。
- **双端联调实测未执行**。阻塞：两侧 mozart-usb-audio 服务以 root 运行，更新代码后重启需要 sudo 密码，当前会话无权限。解除条件：在主机执行
  `sudo systemctl restart mozart-usb-audio`（运行目录 `/home/moyamryia/aec-study-20261020/`），
  在 Jetson 执行 `sudo systemctl restart mozart-usb-audio`（运行目录 `~/Mozart/usb-audio/`），
  然后按"板端延迟预算实测流程"测量。

## 板端延迟预算实测流程（重启后执行）

1. Jetson 启动预处理：`~/build-pre-m1/mozart-pre -d plughw:CARD=Loopback,DEV=1,SUBDEV=0 -o plughw:CARD=Loopback,DEV=0,SUBDEV=1 -a 127.0.0.1 -p 18000 --mode direct --ctl-port 19040`。
2. 主机以小周期向 `plughw:CARD=Loopback,DEV=0,SUBDEV=1` 播放脉冲测试音，从 `plughw:CARD=Loopback,DEV=1,SUBDEV=0` 录音。
3. 互相关求往返延迟；预算口径：主机 aplay/arecord 周期 + USB 0.63 ms×2 + Jetson 桥周期×2 + 预处理本段 6.7 ms。
4. 直连模式（MOZART_DIRECT=1 + 物理设备）复测，减去两侧 aloop 跳。

## 适用范围

- 配置：协议层与守护进程代码变更；单测在本机 x86_64 执行。
- 样本：协议单测 10 项；守护进程未做长时运行。
- 双端联调、AI 流回归、20 分钟受压未执行（同上阻塞）。
