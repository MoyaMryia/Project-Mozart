# M3：快流专用端点（描述符手术）

日期：2026-10-25。对应总计划 v1 的 M3 里程碑。

## 交付物

| 模块 | 内容 |
| --- | --- |
| `usb-audio/jetson-daemon.py` 描述符 | 接口 1（vendor-specific）+ ep3（0x03 OUT）/ep4（0x84 IN），FS 64 B / HS 512 B；接口 0 原样保留；描述符自检脚本解析验证双接口四端点 |
| `preprocessor/src/usb_link.c`（+头） | 快流端点客户端：24 B 桥帧 C 编解码（与 proto.py 逐字段一致）、ep3 阻塞读、ep4 分帧写、未知流按长度跳过（前向兼容） |
| `preprocessor/src/main.c` | `--usb-endpoint <ffs目录>` 模式：采集改读 ep3、直出改写 ep4（128 样本帧、淡变、延迟探针复用）；MZRT 发送路径不变 |
| `usb-audio/host-daemon.py` | 直连模式声明接口 1 并将快流路由到专用端点；声明失败回退接口 0 复用（M2 形态）；新增接口 1 接收泵 |
| `preprocessor/tests/test_usb_link.c` | Python 参考帧 golden 对拍（跨语言）、未知流跳过、编码往返、不完整帧等待，4 组断言 |

## 延迟预算（部署后应达）

主机麦克风 period 2.67 + USB 0.63 + **预处理器（ep3 读 + WOLA 4.0 + ep4 写）≈ 4.1** + USB 0.63 + 主机扬声器 2.67 ≈ **10.7 ms**（主机 64 样本周期时 ≈ 8 ms）。
对比 M2 形态（经守护进程 + aloop 两跳）省去约 5.3 ms 与两级泵抖动。

## 验收状态

- 预处理器 5/5 测试通过（含 usb_link）；描述符经 Jetson 内核实机验证：
  首版接口 1 引用不存在的字符串索引被拒（strings EINVAL），改为与接口 0
  共用索引后内核接受（提交 bbb9de3）。
- **端到端实测已执行**（2026-10-25）：描述符在真机 UDC 枚举，主机声明接口 1
  并路由快流；预处理器 `--usb-endpoint` 模式打开 ep3/ep4 成功
  （需 chmod 666，见运维注意），direct_periods 持续增长、`direct_us` avg 58 µs、
  MZRT 发送并行正常。
- **主机回路延迟未达标**：非周期脉冲全局互相关实测 **740 ms**（归一化峰 0.33），
  早前稀疏脉冲观测到 5 ms 级最快事件，链路功能正确。堆积定位移入适用范围。

## 运维注意

- `/dev/ffs-mozart/ep*` 默认 root 权限：非 root 预处理器需
  `sudo chmod 666 /dev/ffs-mozart/ep3 /dev/ffs-mozart/ep4`（重启或重挂载后失效），
  后续写入 udev 规则固化。

## 适用范围

- 配置：本机 x86_64 编译与单测；Jetson 实机 UDC 枚举与端点联调。
- 样本：usb_link 单测 4 组断言；描述符内核接受性 3 次迭代验证；
  端到端音频回路 3 次测量（每次 6–12 s）。
- 回路延迟的逐跳分解未做。阻塞：桥帧缺少逐跳转发时间戳。解除条件：为 ts_us 增加逐跳转发字段后测量。
