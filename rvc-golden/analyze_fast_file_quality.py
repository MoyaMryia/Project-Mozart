#!/usr/bin/env python3
"""比较整段文件输出与已保存的 Golden 参考。"""
import argparse
import json
from pathlib import Path

import soundfile as sf

from compare_streaming_quality import compare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--onnx', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # 整段路径没有分块拼接边界。使用超过测试音频长度的边界间隔。
    hop = int(sf.info(args.reference).duration*16000)+16000
    result = {name: compare(args.reference, path, hop)
              for name, path in [('onnx',args.onnx), ('backend',args.backend)]}
    result['notes'] = [
        '固定 espeak 输入及同一 Golden 张量；文件设置在测试配置中保存。',
        '后端与 PyTorch 使用不同随机数生成器；波形不能按同一噪声张量比较。',
        'F0 使用 librosa.pyin；物理播放与人工听感未由本脚本验证。']
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
