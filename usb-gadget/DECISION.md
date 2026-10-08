# USB 音频输出决策

决策日期：2026-09-06。本文取代 [STATUS.md](STATUS.md) 中仍开放的 gadget 调查问题。
项目停止在当前 Tegra234 XUDC 平台上进行 UAC gadget 实验。
调查包含 panic、ramoops、DMA/completion 修复候选和 ISO 流测试；原始证据保留在本目录。

## 当前方案

```text
Jetson USB-A host
  -> 外接 USB 声卡（UAC、snd-usb-audio）
  -> 3.5 mm 模拟音频线
  -> 电脑音频输入
```

板端音频路径：

```text
USB 麦克风 -> mozart-pre -> UDP -> mozart_stated RVC
                 ^                      |
                 +------ UDP 输出 ------+
                 -> ALSA 外接声卡 -> 模拟音频线 -> 电脑
```

历史测试和供应商资料支持关闭当前平台的 XUDC ISO/UAC 路线。
该决策限定到本项目使用的平台，不据此推断其他 NVIDIA 平台。
标准 USB 声卡连接 Jetson host 口，使用现有 ALSA 播放路径，不需要修改内核。
电脑收到模拟输入；是否有兼容输入插孔需要在采购前确认。

## 硬件验收

1. 确认外接声卡提供 UAC 播放功能。
2. 确认电脑输入接口、线缆插头和电平匹配。
3. 在 Jetson 用 `lsusb -t` 确认 Audio Class。
4. 用 `aplay -l` 和 `aplay -L` 确认播放设备。
5. 使用稳定 ALSA 名称配置输出。
6. 在电脑录音，核对时长、音量、连续性和失真。

设备枚举成功或电平条变化不能代替录音验收。
电脑接口可能需要 TRRS 转接、线路输入或衰减；具体连接以实际设备为准。

## 板端联调

先列出设备：

```bash
aplay -l
aplay -L
```

在已有 `mozart-pre` 命令中增加播放设备，例如：

```text
-o plughw:CARD=Device,DEV=0
```

从较低输出音量开始，结合电脑录音调整。默认关闭电脑麦克风加强后再测量。
卡号只用于临时诊断，长期配置使用稳定设备名称。

## 路线约束

- 保持当前 3550000.usb 的 UAC gadget 实验关闭，包括 candidate 模块和 initrd 替换。
- 不带主机线热卸载或重绑 UDC/gadget；事故记录见 [串口测试](crash-20260905-serial-test/README.md)。
- bulk/WinUSB 自定义协议保留为备选设计，当前没有实施任务。
- 历史系统恢复步骤见 [RESTORE-20260906.md](RESTORE-20260906.md)，不作为当前部署步骤执行。
