# RVC Voice Changer Backend — Protocol Specification

## Overview

- **Audio stream**: UDP, bidirectional, raw int16 PCM
- **Control / model management**: HTTP REST
- **Default ports**: UDP `18000`, HTTP `18080`
- **Sample rate**: 48 kHz, mono, int16

---

## 1. UDP Audio Protocol

### Packet Format

Binary layout (little-endian, C struct compatible):

```c
struct AudioPacket {
    uint32_t magic;      // 0x52415643 ('RAVC')
    uint32_t seq;        // monotonically increasing sequence number
    uint64_t timestamp;  // send timestamp in microseconds
    uint16_t format;     // 0 = int16 mono 48kHz
    uint16_t samples;    // number of int16 samples in payload
    int16_t  payload[];  // audio samples
};
```

Header size: **20 bytes**.

### Timing

- **Packet duration**: 10 ms → 480 samples → payload 960 bytes
- **Inference chunk**: server accumulates 2 packets → 20 ms chunk
- **Jitter buffer**: client should maintain 2–3 chunks (40–60 ms) before playback

### Behavior

1. Client sends input audio packets to `server_ip:18000`.
2. Server receives packets, orders by `seq`, accumulates into 20 ms chunks.
3. Server runs: RNNoise denoise → RVC conversion.
4. Server sends converted audio packets back to the client's source address.
5. Client receives packets, buffers by `seq`, and plays.

### C++ Client Example (pseudo-code)

```cpp
#pragma pack(push, 1)
struct AudioPacketHeader {
    uint32_t magic = 0x52415643;
    uint32_t seq;
    uint64_t timestamp_us;
    uint16_t format = 0;
    uint16_t samples;
};
#pragma pack(pop)

void send_audio(int sock, sockaddr_in& dst, uint32_t seq,
                const std::vector<int16_t>& pcm) {
    AudioPacketHeader hdr;
    hdr.seq = seq;
    hdr.timestamp_us = now_us();
    hdr.samples = pcm.size();

    std::vector<uint8_t> buf(sizeof(hdr) + pcm.size() * 2);
    memcpy(buf.data(), &hdr, sizeof(hdr));
    memcpy(buf.data() + sizeof(hdr), pcm.data(), pcm.size() * 2);
    sendto(sock, buf.data(), buf.size(), 0,
           reinterpret_cast<sockaddr*>(&dst), sizeof(dst));
}
```

---

## 2. HTTP REST API

### `GET /health`

Health check.

**Response:**
```json
{ "status": "ok" }
```

### `GET /status`

Current service status and latency statistics.

**Response:**
```json
{
  "mode": "mock",
  "model": {
    "current_model_id": null,
    "loaded": false
  },
  "latency_stats_ms": {
    "count": 123,
    "avg_ms": 5.2,
    "max_ms": 12.1
  },
  "udp_config": {
    "host": "0.0.0.0",
    "port": 18000,
    "samples_per_packet": 480,
    "samples_per_chunk": 960
  }
}
```

### `GET /models`

List uploaded models.

**Response:**
```json
{
  "models": [
    { "id": "speaker_a", "exists": true, "loaded": false, "current": false }
  ]
}
```

### `POST /models/upload`

Upload a new RVC model.

**Form fields:**
- `model_id`: string
- `pth_file`: `.pth` generator checkpoint
- `config_file`: `config.json`
- `index_file`: `.index` (optional)

**Response:**
```json
{ "status": "uploaded", "model_id": "speaker_a" }
```

### `POST /models/{model_id}/activate`

Load and activate a model for inference.

**Response:**
```json
{ "status": "activated", "model_id": "speaker_a" }
```

---

## 3. Model File Layout

Uploaded models are stored as:

```
models/
└── {model_id}/
    ├── {model_id}.pth
    ├── {model_id}.index
    └── config.json
```

Compatible with standard RVC-Project trained models.
