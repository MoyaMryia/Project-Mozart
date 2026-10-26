#!/usr/bin/env python3
"""从 usb-audio/protocol.json 生成 C 与 Python 协议定义。"""
import argparse
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / 'usb-audio' / 'protocol.json'
OUTPUTS = {
    ROOT / 'IO' / 'include' / 'mozart' / 'usb_protocol_generated.h': 'c',
    ROOT / 'usb-audio' / 'proto_generated.py': 'python',
}


def render(schema, language):
    constants = schema['constants']
    formats = schema['formats']
    offsets = schema['header_offsets']
    if language == 'python':
        lines = ['# 由 tools/gen_usb_protocol.py 生成；请修改 protocol.json。']
        lines.extend(f'{name} = {value!r}' for name, value in constants.items())
        lines.extend(f'{name} = {value!r}' for name, value in formats.items())
        lines.extend(f'{name} = {value!r}' for name, value in offsets.items())
        return '\n'.join(lines) + '\n'

    lines = [
        '/* 由 tools/gen_usb_protocol.py 生成；请修改 usb-audio/protocol.json。 */',
        '#ifndef MOZART_USB_PROTOCOL_GENERATED_H',
        '#define MOZART_USB_PROTOCOL_GENERATED_H',
        '',
    ]
    for name, value in constants.items():
        if isinstance(value, float):
            text = format(value, '.9g')
        else:
            text = str(value)
        lines.append(f'#define MOZART_USB_{name} {text}')
    sizes = {'HEADER_BYTES': struct.calcsize(formats['HEADER_FORMAT'])}
    lines.append('')
    for name, size in sizes.items():
        lines.append(f'#define MOZART_USB_{name} {size}')
    lines.append('')
    for name, value in offsets.items():
        lines.append(f'#define MOZART_USB_{name} {value}')
    lines.extend(['', '#endif', ''])
    return '\n'.join(lines)


def outputs(schema):
    expected_sizes = {
        'HELLO_BYTES': struct.calcsize(schema['formats']['HELLO_FORMAT']),
        'START_BYTES': struct.calcsize(schema['formats']['START_FORMAT']),
        'STOP_ACK_BYTES': struct.calcsize(schema['formats']['STOP_ACK_FORMAT']),
    }
    for name, size in expected_sizes.items():
        if schema['constants'][name] != size:
            raise ValueError(f'{name} must match its format size ({size})')
    return {path: render(schema, language) for path, language in OUTPUTS.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='检查生成文件是否与 schema 一致')
    args = parser.parse_args()
    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    failed = False
    for path, content in outputs(schema).items():
        if args.check:
            if not path.exists() or path.read_text(encoding='utf-8') != content:
                print(f'生成文件不一致：{path.relative_to(ROOT)}', file=sys.stderr)
                failed = True
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
