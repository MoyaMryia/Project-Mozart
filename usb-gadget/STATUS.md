> **⚠ 2026-09-06 终局**：本文件 §3 描述的 Windows Code 10 排查已被
> `DECISION.md` 取代。根因是硬件（Tegra234 XUDC 不支持 ISO），USB gadget
> 路线锁死关闭，最终方案见 `DECISION.md`。下文保留作调查记录。

# USB 声卡（UAC2 Gadget）工作状态与交接

> UPDATE 2026-09-05: Windows progressed to audio interface operations after
> the runtime nonempty-serial test, then the Jetson panicked in feedback
> buffer cleanup. See `crash-20260905-serial-test/README.md` for saved evidence.
> Offline fixes and tests are in `candidate/`; they are NOT installed or
> cleared for an unguarded hardware test. See `candidate/README.md` for the
> completed lifetime fixes, recovery-boot prerequisite and ISO limitations.
> The historical status below must
> not be interpreted as validation of ISO streaming or boot-safe deployment.

> 2026-09-05 会话落盘。目标：让 Jetson Orin Nano 通过 Type-C 口枚举为
> USB 声卡，主机免驱看到 "Mozart AI Sound Card"，作为 TARGET.md
> "AI 声卡" 产品形态的最后一米。

---

## 0. 一句话现状

板端全部打通：补丁版 UDC 驱动开机原生加载、gadget 开机自启、UDC 绑定成功、
板端出现 ALSA `UAC2Gadget` 卡、Windows 能枚举出 1d6b:0104 复合设备。
**卡在：Windows usbccgp 报 Code 10 (0xC000000E)，复合层启动失败**，
尚未定位到具体描述符字段。Linux 主机侧验证（self-loopback）还没做。

## 1. 已完成的事（按因果顺序）

1. **内核能力缺口**：JetPack r39.2 (6.8.12-1021-tegra) 有 gadget 框架
   （`USB_CONFIGFS=m`、UDC `3550000.usb` 在位），但
   `CONFIG_USB_CONFIGFS_F_UAC2 is not set`，无 `usb_f_uac2` 模块。
2. **out-of-tree 编出 `f_uac2.ko` + `u_audio.ko`**：源码取自
   NVIDIA r39.2 官方 `public_sources.tbz2`（295MB，URL 见 §4）
   里的 `kernel_src.tbz2`，与系统原版 .ko 外部符号完全一致。
   注意依赖四个文件：`f_uac2.c`、`u_audio.{c,h}`、`u_uac2.h`、
   `uac_common.h`（v6.8 的 u_audio.h 需要）。
3. **第一个 bind 失败根因（-19 ENODEV）**：tegra-xudc 驱动
   `tegra_xudc_alloc_ep()` 只声明 `type_bulk/type_int`，
   **漏了 `type_iso`**——ISO 传输实现完整（TRB_TYPE_ISOCH 等）但从不
   广播，`usb_ep_autoconfig` 按能力过滤找不到同步端点。上游 v6.12
   也没修。补丁 = 加一行 `ep->usb_ep.caps.type_iso = true;`。
4. **热替换失败**（重要教训）：运行中 `rmmod/insmod tegra-xudc`
   触发 `tegra_xudc_remove` → PHY 角色切换原子通知链 →
   `cancel_work_sync` in atomic 的 WARN；期间再遇解绑/枚举竞态
   **两次把 USB+SSH 整体拖死，被迫断电重启**。
5. **改为持久化方案**（当前生效）：
   - 补丁版 `tegra-xudc.ko` 放进 `/lib/modules`——**没用**，
     因为 `/boot/initrd` 里还有一份原版（build-id 验证实锤），
     开机 5s 从 initrd 加载；
   - 外科手术替换 initrd 里的单个 .ko：解包（zcat|cpio）→ 替换 →
     原格式重包（gzip newc cpio）。体积 11.37→11.45MB，bootloader
     引用的路径不变（`/boot/initrd`，extlinux.conf 指向它）。
     `update-initramfs -u` 生成的通用版体积翻倍（25MB）被弃用。
   - `nv-l4t-usb-device-mode{,-runtime}.service` 已 disable
     （它创建 USB-C 上的 l4t 默认复合 gadget 0955:7020）。
   - `/etc/systemd/system/mozart-gadget.service` 开机执行
     `usb-gadget/mozart-gadget.sh start`（modprobe + configfs 建
     gadget + 绑 UDC）。

## 2. 当前配置（生效中）

- Device: VID 1d6b / PID 0104, bcdUSB 0x0200, bcdDevice 0x0100
- Function: uac2.0，双向 48kHz/2ch/S16，`p_hs_bint=c_hs_bint=4`
- FU（静音+音量）默认开（含中断端点）
- 板端音频口：`card 2: UAC2Gadget`——playback PCM 即主机看到的"麦克风"
- 音频数据面（设计，未联调）：
  `USB麦 → mozart-pre(降噪) → rvc-backend(TRT 变声) → UDP回包 →
   mozart-pre -o plughw:UAC2Gadget → 主机录到变声后的人声`

## 3. 未解决的问题（接手从这里开始）

**Windows usbccgp Code 10 / 0xC000000E**（复合父设备启动失败，
还没轮到音频子驱动）。已排除：单向/双向配置都同样失败；bInterval=4 合法。

排查线索（按性价比排序）：
1. **Self-loopback 验证**：C-to-A 线把本板 Type-C 插到本板 USB-A 口，
   Linux xHCI 枚举后 `lsusb -v -d 1d6b:0104` 全量 dump 描述符 +
   `arecord` 验证音频功能真的能通。这一步能区分"功能本身坏了"vs
   "仅 Windows 挑剔"。
2. **对照实验：f_uac1（UAC1）**。Windows 对 UAC1 gadget 兼容性好得多
   （大量成功案例），16bit/48kHz 正好够 demo。f_uac1 同样需要 ISO
   端点（补丁已就位）。源码在同目录 `kernel_src.tbz2` 里。
3. **抓 Windows 侧证据**：设备管理器 → 详细信息 → 属性"问题代码"；
   或 USBView/USB Device Viewer (WDK) 看 Windows 读到的描述符树。
4. **查上游 commit**：git.kernel.org 现在有 Anubis 反爬（webfetch 被
   挡），改用 GitHub mirror（`gh-proxy.org/` 前缀可用，raw.githubusercontent
   直连被墙）查 f_uac2.c 在 v6.8..v6.12 间是否修过 Windows 相关问题。
5. 疑点清单（凭记忆，未验证）：IAD bFirstInterface 与 composite 分配的
   interface 序号一致性；IN 端点 async 无 feedback 是否触发 Windows
   严格校验；bcdUSB 0x0200 vs 0x0210/BOS；串号/字符串描述符。

## 4. 资产索引

| 资产 | 位置 |
|---|---|
| 源码+脚本+补丁（工作区） | `Mozart/usb-gadget/` |
| 备份+恢复指南 | `~/mozart-archive/usb-gadget-backup-20260905/`（**先读 RESTORE.md**） |
| NVIDIA r39.2 源码 | `usb-gadget/public_sources.tbz2`（295MB，可删可重下：`https://developer.nvidia.com/downloads/embedded/L4T/r39_Release_v2.0/sources/public_sources.tbz2`） |
| 服务 | `/etc/systemd/system/mozart-gadget.service`（enabled） |

## 5. 铁律（血泪，勿再踩）

1. **驱动替换/gadget 重建必须在主机线拔掉时做**；带主机解绑 gadget 或
   热换 xudc 曾两次拖死 SSH+USB 被迫断电。
2. 改 `/lib/modules` 里的 tegra-xudc.ko **不够**——内核从
   `/boot/initrd` 加载它。验证手段：对比
   `/sys/module/tegra_xudc/notes/.note.gnu.build-id` 与
   `readelf -n <ko>` 的 Build ID。
3. `update-initramfs -u` 生成的通用 initrd 与 NVIDIA 原版差异大
   （11→25MB），不要直接替换 `/boot/initrd`，用外科手术法。
4. 供电：主机 USB 口只出枚举电流，别指望它给板子供电；DC 电源必须插。
5. 线材：Jetson Type-C（device）↔ 主机，C-to-C 或 C-to-A 都行；
   **A-to-A 不可用**；Jetson 的 USB-A 口是纯 host。

## 6. 恢复原状（一键）

见 `~/mozart-archive/usb-gadget-backup-20260905/RESTORE.md`。
