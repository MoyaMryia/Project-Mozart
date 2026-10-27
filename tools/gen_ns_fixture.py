#!/usr/bin/env python3
"""生成噪声抑制器的对拍夹具（C 实现与 Python 参考实现对拍）。

输入为确定性合成信号（白噪 + 动态音乐 + 幅度调制语音样音），
经 tools/wola_reference.py 参考实现处理，输出期望 float32 序列。
用法：python3 tools/gen_ns_fixture.py <输出目录>
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wola_reference import GATE_NR_DB, WolaConfig, process_array, synth_music, synth_noise

SECONDS = 3.0


def make_input() -> np.ndarray:
    rng = np.random.default_rng(20261025)
    n = int(SECONDS * 48000)
    noise = synth_noise('white', n, rng) * 0.03
    music = synth_music(n, rng) * 0.4
    t = np.arange(n) / 48000.0
    tone = 0.1 * np.sin(2 * np.pi * 300.0 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t))
    return noise + music + tone


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)

    x = make_input().astype(np.float64)
    config = WolaConfig(lookahead=0)  # 与部署候选 (128, 64, 0) 对齐
    expected = process_array(x, config)
    silence_input = np.concatenate([np.zeros(48000, dtype=np.float64), x])
    silence_expected = process_array(silence_input, config)

    x32 = x.astype('<f4')
    e32 = expected.astype('<f4')
    silence_x32 = silence_input.astype('<f4')
    silence_e32 = silence_expected.astype('<f4')
    (out_dir / 'gates.h').write_text(f'#define WOLA_NR_GATE_DB {GATE_NR_DB}\n')
    (out_dir / 'input.f32').write_bytes(x32.tobytes())
    (out_dir / 'expected.f32').write_bytes(e32.tobytes())
    (out_dir / 'silence-input.f32').write_bytes(silence_x32.tobytes())
    (out_dir / 'silence-expected.f32').write_bytes(silence_e32.tobytes())
    (out_dir / 'meta.json').write_text(json.dumps({
        'sample_rate': 48000,
        'config': vars(config),
        'input_samples': len(x32),
        'expected_samples': len(e32),
        'silence_input_samples': len(silence_x32),
        'silence_expected_samples': len(silence_e32),
    }, indent=1))
    print(f'fixture: {len(x32)} 输入样本, {len(e32)} 期望样本 -> {out_dir}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
