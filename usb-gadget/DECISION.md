# USB 支持最终决策（锁死）

> 2026-09-06 定案。本文档取代 STATUS.md 中所有"gadget 还能救"的开放性问题。
> 结论经过：两次 panic、七八次重启、ramoops 栈回溯、DMA/completion 候选修复、
> 以及 NVIDIA 文档盖章，不再重开。

## 决策

Mozart 对电脑的音频输出采用：

```
Jetson USB-A host 口
  └─ USB→3.5mm 声卡小尾巴（标准 UAC 类设备，snd-usb-audio 免驱）
       └─ 3.5mm 音频线 → 电脑 3.5mm 麦克风输入口
```

音频链路：

```
USB 麦(card 0) → mozart-pre(降噪/VAD) ──UDP──► rvc-backend(TRT 变声)
                                                  │ UDP 回包
     小尾巴 ◄── plughw:<小尾巴卡号> ◄── mozart-pre -o
       └─ 3.5mm ──► 电脑麦克风（模拟）
```

## 为什么是它

- **Tegra234 XUDC device mode 不支持 ISO 端点**（NVIDIA 文档明示，且实测
  ISO 流 3.45ms 内 ring underrun 0xe，所有软件修复候选同样失败）。
  标准 UAC1/UAC2 gadget 在本机无解，已盖棺。
- 小尾巴与已验证的 USB 麦（card 0 "USB PnP Sound Device"）同类，
  Jetson 侧零新代码、零内核改动、零签名问题。
- 电脑端是模拟 3.5mm，不存在驱动概念。
- 比赛评分不要求 USB 传输介质，"对方听到变声"成立。

## 硬件采购/验收标准

1. 商品为 **USB→3.5mm 声卡**（免驱、UAC 描述）；不要 A-to-A 线、
   不要只有耳机口的线、不要需要厂商驱动的型号
2. 到货先裸测：Jetson `lsusb -t` 见 Audio Class；
   `aplay -l` 有播放节点（本方案不需要它的录音节点）
3. Windows 端验收：录音设备里出现对应输入，录到的波形核对
   时长/音量/连续性，不只看电平条

## 联调时板端操作（仅三步）

```sh
aplay -l                              # 找到小尾巴卡号 X
# mozart-pre 起链路时加: -o plughw:X,0
# 电脑端录一段核对波形
```

调音：小尾巴音量 20–30% 起步；PC 端麦克风加强默认关。

## 禁止事项

- 禁止再在 3550000.usb 上做任何 UAC gadget 实验（含 `usb-gadget/`
  下的 candidate 模块、initrd 替换）
- 禁止带主机线热卸载/重绑 UDC 或 gadget（历史事故见
  `crash-20260905-serial-test/`）
- 自定义 USB 驱动路线（bulk 协议 + WinUSB）仅作为纸面备选，除非
  对录线方案被证伪，否则不启动
