# Project Mozart · Realtime AI Voice Changer（总项目）

面向 **NVIDIA Jetson Orin Nano 8GB** 的实时 AI 变声器。系统遵循 **Project Mozart**
两级解耦架构：

```
真实麦克风 ─► PipeWire ─► [Mozart 预处理] ──契约流──► [RVC 后处理] ─► 虚拟麦克风/耳机
                          (Rust,另做)        16k/f32/20ms+12B meta   (C++,本仓库两版)
```

- **预处理**（另做，Rust）：把脏采集流洗净、归一化、分段标注 → 干净的标准流
- **后处理**（本仓库两版）：在干净流上做 RVC 变声，不再清洗

本总目录下并存**两套等价的后处理实现**，技术路线不同，可对照评估择优合并。

---

## 子项目一览

| 子目录 | 路线代号 | 一句话定位 |
|---|---|---|
| [`ai_voice_changer`](./ai_voice_changer/README.md) | **Mozart-Post（本地声卡）** | dlopen 上游 ring + PipeWire 虚拟源 + ONNXRuntime |
| [`ai_voice_changer_kimi`](./ai_voice_changer_kimi/README.md) | **UDP-Contract（网络契约）** | UDP 定长契约包 + libtorch + 原生 socket HTTP |

---

## 共同契约（Project Mozart 接口）

两套实现都遵守同一份预处理→后处理契约，可互换上游/对接同一 Mozart 实例：

- **音频**：16 kHz / mono / float32 / 20 ms → 320 samples
- **元数据**：12 B，`#pragma pack(1)`
  `uint64_t pts_ns | uint32_t frame_idx | uint8_t vad_flag | uint8_t energy_db | uint8_t conf | uint8_t segment_id`
- **输出**：48 kHz / mono / float32 / 20 ms → 960 samples（RVC v2 生成器原生）
- **行为**：`vad_flag==0` 跳推理返静音；`segment_id` 变化重置流式状态；因果可流式

差异点仅在元数据之外的**传输/输出/引擎**层，详见各子项目文档：
- Mozart-Post 版：[`ai_voice_changer/docs/contract_spec.md`](./ai_voice_changer/docs/contract_spec.md)
- UDP-Contract 版：[`ai_voice_changer_kimi/docs/interface_contract.md`](./ai_voice_changer_kimi/docs/interface_contract.md)

---

## 关键差异对比

| 维度 | Mozart-Post (`ai_voice_changer`) | UDP-Contract (`ai_voice_changer_kimi`) |
|---|---|---|
| 上游数据传输 | shared ring + `dlopen mozart_post_*` | UDP `18000` 端口收包 |
| 取帧方式 | 后端主动 `poll` | 上游 push，后端 `recvfrom` |
| 输出终点 | PipeWire **virtual source**（其他 app 当麦克风）| UDP 回传给发包方 |
| 适用拓扑 | 严格本地 Jetson | 同机 / 跨机皆可 |
| 推理引擎 | ONNX Runtime CUDA EP（吃 `.onnx`）| libtorch（吃 `.pth`，可 `-DUSE_LIBTORCH=ON`）|
| 模型上传格式 | `.onnx`（客户端先用 PyTorch 导出）| `.pth`（标准 RVC，libtorch 直读）|
| 配置文件 | `config.toml`（toml++）| `config.yaml`（yaml-cpp）|
| 日志 | 自写 `MOZART_*` 宏 | spdlog 彩色日志 |
| HTTP 库 | cpp-httplib（header-only）| 原生 socket 自实现 |
| layer 划分 | `contract / output / rvc / inference / server` | `network / rvc / api / utils` |
| 旧协议兼容 | 无（彻底新生）| 保留 `RAVC` int16 旧包 + `MZRT` 新契约双支持 |
| 依赖外部数 | toml++/cpp-httplib(头) + onnxruntime + libpipewire | yaml-cpp + nlohmann/json + spdlog + (libtorch可选) |
| RVC 参数 | 简化（index_rate=0 本期不做）| 完整（pitch/index/filter/rms/protect 全留）|
| 帧积累 | 单帧推理 | 可配 `frames_per_inference` 批量 |
| 测试数 | 3（frame_meta/mock_pipeline/onnx_smoke）| 2（packet/udp_loopback）|

---

## 各自架构图

### Mozart-Post 版（本地声卡）

```
USB 麦 ─► PipeWire ─► [Mozart 预处理(Rust)] ──ring──► [本后端 C++]
                                                       │ dlopen mozart_post_*
                                                       │ ONNX: HuBERT+RMVPE+Generator
                                                       ▼
                                                PipeWire virtual source ◄── 其它 app
```

### UDP-Contract 版（网络契约）

```
[Mozart 预处理] ──UDP MZRT──► [本后端 C++ port 18000]
         ▲                          │ libtorch RVC
         │                          ▼
         ◄──UDP 48kHz MZRT──── 变声帧回传给发包方
```

---

## 选型建议

- **想要"声卡"产品形态**（其它程序能把变声结果当麦克风选）→ 选 **Mozart-Post** 版（PipeWire virtual source 是它的本质特征）。
- **想要跨设备/边缘拆分部署**（预处理在 PC，后端在 Jetson）或保留 UDP 抽象 → 选 **UDP-Contract** 版。
- **想最小化依赖、最快进入 ONNX 路线** → 选 **Mozart-Post**（toml++/cpp-httplib 全是 header-only，onnxruntime 是 Jetson JetPack 原生包）。
- **想直接吃标准 `.pth`、避开客户端导出 ONNX** → 选 **UDP-Contract**（但需在 C++ 复刻 RVC Generator，工程量较大）。
- **后续合并方向**：两套可取长补短——把 UDP-Contract 的**批量推理/参数完整度**移到 Mozart-Post 的 **Engine/Contract 框架**上，形成"本地 ring + 可选 UDP + 一套 ONNX 推理"的混合版。

---

## 快速跑通各自 Mock 闭环

### Mozart-Post 版
```bash
cd ai_voice_changer
cmake -B build -G Ninja && cmake --build build
./build/mozart_post config.toml      # source=mock, sink=dummy, mock_mode=true
ctest --test-dir build --output-on-failure
```

### UDP-Contract 版
```bash
cd ai_voice_changer_kimi
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(nproc)
./rvc_backend                          # mock_mode=true，等 UDP 输入到 18000
# 另一终端运行测试客户端：
./test_udp_loopback
```

---

## 目录结构总览

```
电子秤/                       # 总项目（本 README 所在）
├─ README.md                  # 本文档（跨子项目对比）
├─ ai_voice_changer/          # Mozart-Post 路线（C++/onnxruntime+pipewire）
│  ├─ config.toml
│  ├─ CMakeLists.txt
│  ├─ include/ src/ tests/ docs/
│  └─ README.md
└─ ai_voice_changer_kimi/     # UDP-Contract 路线（C++/libtorch+原生socket）
   ├─ config.yaml
   ├─ CMakeLists.txt
   ├─ include/ src/ tests/ docs/
   └─ README.md
```

---

## 当前阶段

两套均处于 **Phase 1（骨架 + Mock 闭环验证）**：
- 推理核心（HuBERT/RMVPE/Generator）已搭好接口，等真实模型上 Jetson 联调；
- Mozart 预处理（Rust）由另条线开发，两套均提供 mock source 上线即可单独跑通；
- 后续 Phase 2：真实模型接入、延迟优化（ONNX/TensorRT 或 libtorch JIT）、`.index` 检索回归。

详见各子项目 README 的"Phase 状态"章节。