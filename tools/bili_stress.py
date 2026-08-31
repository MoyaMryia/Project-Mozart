#!/usr/bin/env python
# bili_stress.py — 实时变声链路长时间压测
# 以实时节拍把长音频灌进 UDP 流式 RVC，记录连续性/错误/内存曲线
import socket, struct, time, sys, subprocess
import numpy as np
import soundfile as sf

WAV = sys.argv[1] if len(sys.argv) > 1 else "/home/moyamryia/mozart-archive/test-audio/bili_16k.wav"
PACE = float(sys.argv[2]) if len(sys.argv) > 2 else 0.02   # 0.02=实时, 0.01=2x超载
LOG = "/tmp/opencode/keep/stress.log"

pcm, sr = sf.read(WAV, dtype="float32")
assert sr == 16000, f"need 16k mono, got {sr}Hz"
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(0.01)
sock.connect(("127.0.0.1", 18000))
H = struct.Struct("<IQIBBBB")

real = zeros = other = 0
log = open(LOG, "w", buffering=1)
def note(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    log.write(line + "\n")
    print(line, flush=True)

note(f"start: {WAV} {len(pcm)/16000:.0f}s pace={PACE*1000:.0f}ms/frame")
t0 = time.monotonic()
be_pid = subprocess.run(["pgrep", "-f", "rvc_backend"], capture_output=True, text=True).stdout.split()[0]

def drain():
    global real, zeros, other
    try:
        while True:
            d, _ = sock.recvfrom(65536)
            if len(d) == H.size + 960 * 4:
                x = np.frombuffer(d[H.size:], dtype=np.float32)
                if np.abs(x).max() > 1e-4: real += 1
                else: zeros += 1
            else:
                other += 1
    except socket.timeout:
        pass

n_frames = len(pcm) // 320
last_rss = 0
for k in range(n_frames):
    hdr = H.pack(0x4D5A5254, int((time.monotonic()-t0)*1e9)+1, k+1, 1, 200, 255, 1)
    sock.send(hdr + pcm[k*320:(k+1)*320].astype(np.float32).tobytes())
    drain()
    target = t0 + (k+1) * PACE
    if target > time.monotonic():
        time.sleep(target - time.monotonic())
    if (k+1) % 1500 == 0:  # 每 30s
        rss = int(open(f"/proc/{be_pid}/status").read().split("VmRSS:")[1].split()[0]) // 1024
        swp = int(open(f"/proc/{be_pid}/status").read().split("VmSwap:")[1].split()[0]) // 1024
        note(f"{(k+1)*0.02:.0f}s/{n_frames*0.02:.0f}s 喂入 | 收包 real={real} zero={zeros} bad={other} | be RSS={rss}MB swap={swp}MB")

note("feed done, draining 8s tail...")
end = time.monotonic() + 8
while time.monotonic() < end:
    if not drain(): time.sleep(0.05)
dur = time.monotonic() - t0
note(f"DONE: 喂入 {n_frames*0.02:.0f}s 用时 {dur:.0f}s | real={real*0.02:.0f}s zero={zeros*0.02:.0f}s bad={other}")
