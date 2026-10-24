#!/usr/bin/env python3
"""VAD 分段对比：新旧预处理二进制对同一 WAV 的分段边界与 VAD 帧统计。

用法：python3 tools/vad_segment_compare.py <旧二进制> <新二进制> <wav>... [--frames N] [--old-flag F]
输出 JSON 到 stdout。M1 报告的分段对比由此生成。
"""
import argparse
import json
import socket
import struct
import subprocess
import sys

HEADER = struct.Struct('<IQIBBBB')


def capture(binary, wav, frames, extra):
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(('127.0.0.1', 0))
    process = subprocess.Popen(
        [binary, '-i', wav, '-a', '127.0.0.1',
         '-p', str(listener.getsockname()[1]), '-n', str(frames)] + list(extra),
        stderr=subprocess.DEVNULL)
    rows = []
    listener.settimeout(4)
    try:
        while len(rows) < frames:
            rows.append(HEADER.unpack_from(listener.recv(4096)))
    except socket.timeout:
        pass
    process.wait(timeout=5)
    listener.close()
    return rows


def summarize(rows):
    bounds, last = [], 0
    for _magic, _pts, idx, vad, _energy, _conf, seg in rows:
        if seg and not last:
            bounds.append(idx)
        last = seg
    return {'frames': len(rows), 'bounds': bounds,
            'vad_frames': sum(r[3] for r in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('old_binary')
    parser.add_argument('new_binary')
    parser.add_argument('wavs', nargs='+')
    parser.add_argument('--frames', type=int, default=900)
    parser.add_argument('--old-flag', default='--no-rnnoise',
                        help='旧二进制关闭降噪的旗标')
    arguments = parser.parse_args()
    results = {}
    for wav in arguments.wavs:
        results[wav] = {
            'old': summarize(capture(arguments.old_binary, wav, arguments.frames,
                                     [arguments.old_flag])),
            'new': summarize(capture(arguments.new_binary, wav, arguments.frames, [])),
        }
    json.dump(results, sys.stdout, ensure_ascii=False, indent=1)
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
