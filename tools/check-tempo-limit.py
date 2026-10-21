#!/usr/bin/env python3
"""检查变速上限在 Python 与 C 两侧一致，且已编译的库不比源码旧。"""
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.').resolve()
source = root/'preprocessor/src/tempo_filter.c'
library = root/'build-gpu/preprocessor/libmozart-tempo.so'

match = re.search(r'#define MOZART_MAX_TEMPO ([0-9.]+)', source.read_text())
if match is None:
    raise SystemExit('tempo_filter.c 缺少 MOZART_MAX_TEMPO')

sys.path.insert(0, str(root/'tools'))
import tempo_control

native = float(match.group(1))
if abs(native - tempo_control.MAX_TEMPO) > 1e-9:
    raise SystemExit(f'Python 上限 {tempo_control.MAX_TEMPO} 与 C 上限 {native} 不一致')

if library.is_file() and library.stat().st_mtime < source.stat().st_mtime:
    raise SystemExit(f'变速库比源码旧：{library}，请重建')

print(f'变速上限一致：{native}；二进制未过期')
