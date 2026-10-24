#!/usr/bin/env python3
"""从 IO/include/mozart/ctlmsg.h 生成 Python 侧控制协议常量。

用法：python3 tools/gen_ctlmsg.py [输出路径]
默认输出到 stdout。CI 比对生成物与仓内版本，防止三端契约漂移。
"""
import re
import sys
from pathlib import Path

HEADER = Path(__file__).resolve().parents[1] / 'IO/include/mozart/ctlmsg.h'


def main() -> int:
    text = HEADER.read_text(encoding='utf-8')
    names = {}
    for name, value in re.findall(r'#define\s+(MOZART_CTL_\w+)\s+(\d+)', text):
        names[name] = int(value)
    for name, value in re.findall(r'\b(MOZART_CTL_\w+)\s*=\s*(\d+)', text):
        names.setdefault(name, int(value))
    out = sys.stdout if len(sys.argv) < 2 else open(sys.argv[1], 'w')
    out.write('"""由 tools/gen_ctlmsg.py 从 ctlmsg.h 生成；勿手改。"""\n\n')
    for name in sorted(names):
        out.write(f'{name} = {names[name]}\n')
    if out is not sys.stdout:
        out.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
