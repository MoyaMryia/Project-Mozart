#!/usr/bin/env python3
"""把控制协议数据报按 ctlmsg.h 渲染成可读文本（开发调试用）。

用法：python3 tools/protodump.py <十六进制字节串 | - （stdin）>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctlmsg_generated as ctl

NAMES = {v: k for k, v in vars(ctl).items() if k.startswith('MOZART_CTL_')}


def render(datagram: bytes) -> str:
    version, msg_type, flags, req_id = int.from_bytes(datagram[0:1], 'little'), \
        int.from_bytes(datagram[1:2], 'little'), int.from_bytes(datagram[2:4], 'little'), \
        int.from_bytes(datagram[4:8], 'little')
    body = datagram[8:]
    kind = 'request' if flags & 1 else ('response' if flags & 2 else ('event' if flags & 4 else '?'))
    name = NAMES.get(msg_type, f'type{msg_type}')
    extra = ''
    if msg_type == ctl.MOZART_CTL_ERR and len(body) >= 8:
        code = int.from_bytes(body[0:4], 'little')
        text_len = int.from_bytes(body[4:8], 'little')
        extra = f' code={NAMES.get(code, code)} text={body[8:8+text_len].decode(errors="replace")!r}'
    elif msg_type in (ctl.MOZART_CTL_STATUS_DATA, ctl.MOZART_CTL_MODE_EVT) and body:
        extra = f' mode={body[0]} state={body[1] if len(body) > 1 else "-"}'
    return f'version={version} {name} {kind} req_id={req_id}{extra}'


def main() -> int:
    data = sys.stdin.read().strip() if sys.argv[1] == '-' else sys.argv[1]
    print(render(bytes.fromhex(data)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
