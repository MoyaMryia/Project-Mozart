# RVC 后端 · 端口对接表（供前端/预处理端使用）

> 版本：v0.1  
> 日期：2025-07-11  
> 适用范围：Project Mozart 预处理端 / 任何需要接入 RVC 变声后端的前端客户端

---

## 1. 网络端口总览

| 协议 | 方向 | 端口 | 地址 | 用途 | 说明 |
|------|------|------|------|------|------|
| **UDP** | 双向 | **18000** | `0.0.0.0` | 音频契约流 | 接收预处理后音频 → 返回变声后音频 |
| **HTTP** | 入站 | **18080** | `0.0.0.0` | 控制与管理 | 模型列表、状态、健康检查 |

> 后端默认同时监听两个端口。UDP 是**无连接**的，首次收到合法契约包后，后端记录源地址，后续变声音频自动回发到该地址。

---

## 2. UDP 契约流 — 端口 18000

### 2.1 数据流向

```
[前端/预处理端] ──UDP:18000──► [RVC 后端]
                                     │
                                     │ RVC 推理 (16kHz → 48kHz)
                                     │
[前端/播放器]  ◄──UDP:18000── [RVC 后端]
```

- **发送**：前端发送预处理后的 16kHz 契约帧到 `后端IP:18000`
- **接收**：后端自动回发 48kHz 变声契约帧到前端源地址

### 2.2 包格式（定长，小端序）

```c
struct ContractAudioPacket {
    // ── Header: 20 bytes ──
    uint32_t magic;        // 0x4D5A5254 = 'MZRT'，大端视角
    uint64_t pts_ns;       // 呈现时间戳，纳秒（uint64_t）
    uint32_t frame_idx;    // 单调递增帧序号，从 0 开始
    uint8_t  vad_flag;     // 语音检测：1 = 含语音，0 = 静音
    uint8_t  energy_db;    // 能量分贝，映射到 0~255（公式见 §2.4）
    uint8_t  conf;         // 预处理器置信度，0~255
    uint8_t  segment_id;   // 语音段编号，0 = 静音/未分段

    // ── Payload: float32 PCM ──
    float    samples[];    // 单声道 float32，范围 [-1.0, 1.0]
};
```

### 2.3 输入帧（前端 → 后端）

| 参数 | 值 | 计算 |
|------|-----|------|
| 采样率 | **16,000 Hz** | — |
| 帧时长 | **20 ms** | — |
| 样本数 | **320** | 16000 × 0.02 |
| 位深 | **float32** | 4 bytes/sample |
| Payload 大小 | **1,280 bytes** | 320 × 4 |
| 包头大小 | **20 bytes** | — |
| **包总大小** | **1,300 bytes** | 20 + 1,280 |

> 发送要求：每 20ms 发送一帧，严禁拆包或合并。后端按帧序号处理，乱序帧会被丢弃。

### 2.4 输出帧（后端 → 前端）

| 参数 | 值 | 计算 |
|------|-----|------|
| 采样率 | **48,000 Hz** | RVC 生成器输出 |
| 帧时长 | **20 ms** | 与输入帧一一对应 |
| 样本数 | **960** | 48000 × 0.02 |
| 位深 | **float32** | 4 bytes/sample |
| Payload 大小 | **3,840 bytes** | 960 × 4 |
| 包头大小 | **20 bytes** | 与输入帧相同格式 |
| **包总大小** | **3,860 bytes** | 20 + 3,840 |

> 元数据**全部透传**：后端不修改 `pts_ns`、`frame_idx`、`segment_id` 等字段，只把 `vad_flag` 原样返回。

### 2.5 energy_db 映射公式（供前端编码）

```cpp
uint8_t encode_energy_db(float raw_db) {
    // raw_db 典型范围：-96 dB (静音) ~ 0 dB (满幅)
    float clamped = std::max(-96.0f, std::min(0.0f, raw_db));
    return static_cast<uint8_t>(clamped + 96.0f);  // 映射到 0~255
}
```

### 2.6 魔数字节序

`0x4D5A5254` 在小端序传输中的字节顺序：

```
字节偏移 0: 0x54 ('T')
字节偏移 1: 0x52 ('R')
字节偏移 2: 0x5A ('Z')
字节偏移 3: 0x4D ('M')
```

前端解析时直接按小端读 `uint32_t` 即可得到 `0x4D5A5254`。

---

## 3. HTTP API — 端口 18080

### 3.1 端点列表

| 端点 | 方法 | 用途 | 请求 | 响应 |
|------|------|------|------|------|
| `/health` | `GET` | 心跳 | 无 | `{"status":"ok"}` |
| `/status` | `GET` | 运行状态 | 无 | JSON（见 §3.2） |
| `/models` | `GET` | 列出模型 | 无 | JSON 模型列表 |
| `/models/{id}/activate` | `POST` | 切换模型 | URL 参数 `id` | `{"status":"activated","model_id":"xxx"}` |

### 3.2 /status 响应示例

```json
{
  "mode": "mock",
  "model": {
    "current_model_id": null,
    "loaded": false
  },
  "latency_stats_ms": {
    "count": 1234,
    "avg_ms": 2.5,
    "max_ms": 8.1
  },
  "bypass_stats": {
    "inference_count": 1200,
    "bypass_count": 34
  },
  "contract_config": {
    "host": "0.0.0.0",
    "port": 18000,
    "input_sample_rate_hz": 16000,
    "output_sample_rate_hz": 48000,
    "frame_duration_ms": 20,
    "input_samples_per_frame": 320,
    "output_samples_per_frame": 960
  }
}
```

### 3.3 /models 响应示例

```json
{
  "models": [
    {
      "id": "speaker_a",
      "exists": true,
      "loaded": false,
      "current": false
    }
  ]
}
```

---

## 4. 前端发送流程（伪代码）

```cpp
// 配置
constexpr uint16_t BACKEND_PORT = 18000;
constexpr uint32_t INPUT_SR = 16000;
constexpr uint32_t FRAME_MS = 20;
constexpr size_t SAMPLES_PER_FRAME = INPUT_SR * FRAME_MS / 1000; // 320

// 打开 UDP socket
int sock = socket(AF_INET, SOCK_DGRAM, 0);
sockaddr_in backend_addr = {};
backend_addr.sin_family = AF_INET;
backend_addr.sin_port = htons(BACKEND_PORT);
inet_pton(AF_INET, "192.168.1.100", &backend_addr.sin_addr); // 后端 IP

// 每 20ms 发送一帧
uint32_t frame_idx = 0;
while (running) {
    // 1. 从预处理流水线取 320 个 float32 样本
    std::vector<float> samples = preprocessor.get_frame(SAMPLES_PER_FRAME);

    // 2. 填充包头
    uint8_t packet[1300];
    write_u32_le(packet, 0x4D5A5254);               // magic
    write_u64_le(packet + 4, get_timestamp_ns());    // pts_ns
    write_u32_le(packet + 12, frame_idx++);          // frame_idx
    packet[16] = vad_flag;                            // 1 or 0
    packet[17] = encode_energy_db(energy_db);         // energy
    packet[18] = 255;                               // conf (max)
    packet[19] = segment_id;                          // segment

    // 3. 拷贝样本（小端 float32）
    memcpy(packet + 20, samples.data(), SAMPLES_PER_FRAME * sizeof(float));

    // 4. 发送
    sendto(sock, packet, sizeof(packet), 0,
           (sockaddr*)&backend_addr, sizeof(backend_addr));

    // 5. 等待 20ms
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
}
```

---

## 5. 前端接收流程（伪代码）

```cpp
// 接收循环（同 socket，后端会回发到前端源地址）
uint8_t recv_buf[4096];
while (running) {
    sockaddr_in from_addr;
    socklen_t from_len = sizeof(from_addr);
    ssize_t n = recvfrom(sock, recv_buf, sizeof(recv_buf), 0,
                         (sockaddr*)&from_addr, &from_len);
    if (n < 3860) continue; // 包太小，跳过

    // 校验魔数
    uint32_t magic = read_u32_le(recv_buf);
    if (magic != 0x4D5A5254) continue;

    // 解析元数据
    uint64_t pts_ns = read_u64_le(recv_buf + 4);
    uint32_t frame_idx = read_u32_le(recv_buf + 12);
    uint8_t  vad_flag = recv_buf[16];

    // 解析 960 个 float32 样本
    float* samples = reinterpret_cast<float*>(recv_buf + 20);
    size_t num_samples = (n - 20) / sizeof(float);
    // 送入播放器 / 声卡
    player.write(samples, num_samples, 48000);
}
```

---

## 6. 注意事项与约束

| 项 | 约束 | 违反后果 |
|----|------|----------|
| **帧间隔** | 严格 20ms ± 2ms | 后端丢帧或缓存溢出 |
| **采样率** | 输入必须 16kHz，输出 48kHz | 推理失败或音质损坏 |
| **声道** | 仅限单声道 | 多余声道被忽略或导致解析错误 |
| **float 范围** | `[-1.0, 1.0]` | 超出部分后端会截断 |
| **frame_idx** | 必须单调递增，不可回绕 | 乱序帧被丢弃 |
| **VAD 标记** | 静音帧 `vad_flag = 0` | 后端 GPU 节省约 90% 算力 |
| **MTU** | 输出包 3860 bytes > 典型 1500 | 建议局域网或本机回环部署；广域网需启用路径 MTU 发现或拆帧 |

---

## 7. 快速检查清单

前端对接前请确认：

- [ ] 能发送 **1300 bytes** 定长 UDP 包到 `后端IP:18000`
- [ ] 包首 4 字节为魔数 `0x4D5A5254`（小端）
- [ ] 样本为 **float32**、单声道、16kHz、每帧 **320 样本**
- [ ] 能接收 **3860 bytes** UDP 回包，解析出 960 个 float32 样本 @ 48kHz
- [ ] 能访问 `http://后端IP:18080/health` 并收到 `{"status":"ok"}`
- [ ] 帧发送间隔严格控制在 **20ms**

---

*如有问题，请检查后端 `/status` 返回的 `contract_config` 是否与前端配置一致。*
