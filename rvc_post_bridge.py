#!/usr/bin/env python3
"""
Project-Mozart RVC Post-Processing Bridge v2
=============================================
将本地 Python RVC 服务接入 Project-Mozart 后处理链路。

架构:
  麦克风 → [preprocessor/ 48kHz] ──降采样──► [本 Bridge 16kHz] ──► [Python RVC] ──► 输出

Mozart 契约 (预处理→后处理):
  输入: 16kHz / mono / float32 / 320 samples (20ms) + 16B meta
  输出: RVC 模型原生采样率

用法:
  python rvc_post_bridge.py -i preprocessor/clean_speech.wav   # 官方测试音频
  python rvc_post_bridge.py -g                                 # 生成正弦波测试
  python rvc_post_bridge.py -i input.wav -m 模型名.pth         # 自定义
"""

import sys, os, numpy as np, soundfile as sf, requests
from pathlib import Path

RVC_SERVER_URL = "http://localhost:7897"
RVC_DIR = r"C:\Users\25608\Downloads\Retrieval-based-Voice-Conversion-WebUI"
PROJECT_DIR = str(Path(__file__).parent.resolve())
DEFAULT_INPUT = os.path.join(PROJECT_DIR, "preprocessor", "clean_speech.wav")

CONTRACT_SR = 16000
CONTRACT_SAMPLES = 320


class MozartToRVCBridge:
    """Project-Mozart 预处理输出 → RVC 后处理 适配器"""

    def __init__(self, model_name: str, index_path: str = "",
                 f0_method: str = "rmvpe", f0_up_key: int = 0,
                 index_rate: float = 0.75, filter_radius: int = 3,
                 rms_mix_rate: float = 1.0, protect: float = 0.33):
        self.model_name = model_name
        self.index_path = index_path
        self.f0_method = f0_method
        self.f0_up_key = f0_up_key
        self.index_rate = index_rate
        self.filter_radius = filter_radius
        self.rms_mix_rate = rms_mix_rate
        self.protect = protect

    # ---------- 方式 A: HTTP API 调用已运行的 RVC 服务 ----------
    def process_via_http(self, input_wav: str):
        """通过 Gradio API 调用 RVC 服务"""
        # 上传音频
        with open(input_wav, "rb") as f:
            resp = requests.post(f"{RVC_SERVER_URL}/upload",
                                 files={"files": (Path(input_wav).name, f, "audio/wav")})
        if resp.status_code != 200:
            print(f"Upload failed: {resp.status_code}")
            return None
        audio_path = resp.json()[0]

        # 调用推理
        data = {
            "data": [
                self.model_name,
                audio_path,
                self.f0_up_key,
                "",
                self.f0_method,
                self.index_path,
                "",
                self.index_rate,
                self.filter_radius,
                0,  # resample_sr (0=original)
                self.rms_mix_rate,
                self.protect,
            ]
        }
        resp = requests.post(f"{RVC_SERVER_URL}/run/infer_convert",
                             json=data, timeout=120)
        if resp.status_code != 200:
            print(f"Inference failed: {resp.status_code} {resp.text[:200]}")
            return None

        result = resp.json()
        if "data" in result and len(result["data"]) >= 2:
            output_path = result["data"][1].get("name", "")
            return output_path
        return None

    # ---------- 方式 B: 子进程调用 Python RVC 库 ----------
    def process_via_lib(self, input_wav: str, output_wav: str) -> bool:
        """子进程隔离调用 RVC，避免模块命名空间冲突"""
        import subprocess
        script = rf'''
import sys, os, numpy as np, soundfile as sf
os.chdir(r"{RVC_DIR}")
from dotenv import load_dotenv; load_dotenv()
sys.path.insert(0, ".")
from infer.modules.vc.modules import VC
from configs.config import Config

vc = VC(Config())
vc.get_vc("{self.model_name}")

info, out = vc.vc_single(0, r"{input_wav}", {self.f0_up_key}, None,
    "{self.f0_method}", r"{self.index_path}", "",
    {self.index_rate}, {self.filter_radius}, 0, {self.rms_mix_rate}, {self.protect})
print(info)
if out[1] is not None:
    sr_o, d = out
    af = d.astype(np.float32)/32768.0
    sf.write(r"{output_wav}", af, sr_o)
    print(f"OK: {{len(af)/sr_o:.1f}}s@{{sr_o}}Hz")
'''
        result = subprocess.run(
            [rf"{RVC_DIR}\venv\Scripts\python.exe", "-c", script],
            capture_output=True, text=True, timeout=300,
            cwd=RVC_DIR
        )
        print(result.stdout)
        if result.returncode != 0:
            print(result.stderr)
            return False
        return os.path.exists(output_wav)

    def process(self, input_wav: str, output_wav: str) -> bool:
        """自动选择可用方式处理"""
        # 优先用 HTTP API（不需要重复加载模型）
        try:
            resp = requests.get(f"{RVC_SERVER_URL}/", timeout=3)
            print("Using HTTP API mode...")
            result_path = self.process_via_http(input_wav)
            if result_path and os.path.exists(result_path):
                import shutil
                shutil.copy(result_path, output_wav)
                return True
        except Exception:
            pass

        # 退回到直接库调用
        print("Using direct Python lib mode...")
        return self.process_via_lib(input_wav, output_wav)


# ============================================================
# 预处理测试音频生成（模拟 Mozart MockSource: 440Hz sine）
# ============================================================

def generate_mozart_test_audio(output_path: str, tone_hz: float = 440.0,
                               duration_sec: float = 3.0, sample_rate: int = CONTRACT_SR):
    """
    生成与 Project-Mozart MockSource 一致的测试音频
    440Hz 正弦波，16kHz/mono/f32，模拟有声音段
    """
    total_samples = int(duration_sec * sample_rate)
    t = np.arange(total_samples, dtype=np.float32) / sample_rate
    audio = 0.3 * np.sin(2.0 * np.pi * tone_hz * t).astype(np.float32)
    sf.write(output_path, audio, sample_rate)
    print(f"Generated Mozart test audio: {output_path}")
    print(f"  {duration_sec}s, {sample_rate}Hz, {tone_hz}Hz sine, peak={abs(audio).max():.3f}")
    return output_path


# ============================================================
# 主流程
# ============================================================

def prepare_input(input_wav: str) -> str:
    """确保输入为 16kHz mono，如需要则降采样（模拟 preprocessor 的 3:1 降采样）"""
    info = sf.info(input_wav)
    if info.samplerate == CONTRACT_SR and info.channels == 1:
        return input_wav  # 已是契约格式

    data, sr = sf.read(input_wav)
    if data.ndim > 1:
        data = data.mean(axis=1)  # stereo→mono

    if sr != CONTRACT_SR:
        import librosa
        data = librosa.resample(data.astype(np.float64), orig_sr=sr, target_sr=CONTRACT_SR).astype(np.float32)
        print(f"  Resampled: {sr}Hz → {CONTRACT_SR}Hz")

    resampled = input_wav.replace(".wav", "_16k.wav")
    sf.write(resampled, data, CONTRACT_SR)
    print(f"  Prepared: {resampled} ({len(data)/CONTRACT_SR:.1f}s @ {CONTRACT_SR}Hz)")
    return resampled


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", default=DEFAULT_INPUT,
                        help=f"输入 WAV (默认: preprocessor/clean_speech.wav)")
    parser.add_argument("-o", "--output", default="mozart_rvc_output.wav")
    parser.add_argument("-m", "--model", default="John-Frusciante-319-32-12_540e.pth")
    parser.add_argument("--index", default="logs/John-Frusciante-319-32-12_540e.index")
    parser.add_argument("-p", "--pitch", type=int, default=0)
    parser.add_argument("-g", "--generate", action="store_true")
    args = parser.parse_args()

    input_wav = args.input
    if args.generate:
        input_wav = "mozart_test_input.wav"
        generate_mozart_test_audio(input_wav)

    if not os.path.exists(input_wav):
        print(f"Error: {input_wav} not found")
        sys.exit(1)

    info = sf.info(input_wav)
    print(f"Input: {input_wav}  {info.duration:.1f}s {info.samplerate}Hz")

    # 降采样到 16kHz（模拟 Mozart preprocessor 的 3:1 降采样）
    input_16k = prepare_input(input_wav)

    bridge = MozartToRVCBridge(model_name=args.model, index_path=args.index,
                                f0_up_key=args.pitch)
    print(f"RVC: model={args.model} ...")
    success = bridge.process(input_16k, args.output)

    if success:
        out_info = sf.info(args.output)
        print(f"OK: {args.output}  {out_info.duration:.1f}s {out_info.samplerate}Hz")
    else:
        print("FAILED!")
        sys.exit(1)
