#!/usr/bin/env python3
"""检查文档和代码中的限制句。规则来源：AGENTS.md 的“未解决项与限制”。

正文出现限制句时失败。限制句只能写在文末“适用范围”一节，同一行写明阻塞原因和解除条件。
已有命中记录在基线文件中。基线只能缩小：新增命中失败，基线中已消失的条目也失败。
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import ast
import io
import re
import subprocess
import tokenize
import sys

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT/'tools/writing_lint_baseline.json'
# 这些文件定义或测试规则，必须引用被禁止的写法。
# requirements.md 逐字引用赛题评价标准，等级描述含限制词，属于历史证据原文。
# libusb.h 保留第三方原始接口注释；许可证随头文件交付。
EXEMPT = {'AGENTS.md', 'docs/WRITING.md', 'tools/writing_lint.py',
          'tools/writing_lint_baseline.json', 'tools/tests/test_writing_lint.py',
          'requirements.md', 'usb-audio/native-host/third_party/libusb.h'}
CODE_SUFFIXES = {'.py', '.c', '.cc', '.cpp', '.h', '.hpp', '.js', '.mjs', '.ts', '.tsx', '.vue', '.sh'}
HEDGES_ZH = (
    '不代表', '不意味着', '不等于', '不能说明', '不说明', '并非', '不是',
    '不能证明', '无法证明', '不足以证明', '不能保证', '无法保证', '不保证',
    '不能据此', '不能推断', '不应理解为', '不宣称', '不声称', '不能宣称',
    '仍需', '尚需', '待验证', '未验证', '没有验证', '未经验证',
    '尚未', '仍未', '没有完成', '未覆盖', '没有覆盖', '本轮没有', '不作为',
)
# 这些词也用作普通描述，例如“有待处理窗”“未完成任务”。只按限制用法匹配。
HEDGES_ZH_RE = re.compile(r'有待(?!处理)|未完成(?!的?任务)')
HEDGES_EN = re.compile('|'.join((
    r'\b(?:does|do|did) not (?:prove|mean|imply|guarantee|show|establish)\b',
    r'\bcannot (?:prove|guarantee|confirm|show|establish)\b',
    r'\bnot (?:guaranteed|necessarily|verified|proven|yet)\b',
    r'\b(?:unverified|no guarantee)\b',
    r'\b(?:still|further) (?:needs?|requires?|verification|validation|review)\b',
    r'\b(?:has|have) not been\b',
)), re.IGNORECASE)
SCOPE_HEADING = re.compile(r'^##\s+(?:适用范围|Scope)\s*$', re.IGNORECASE)
REPORT = re.compile(r'^reports/[^/]+/RESULTS\.md$')
MESSAGES = {
    'hedge-in-body': '正文出现限制句。能解决就去做；不能解决时移到文末“适用范围”。',
    'scope-missing-blocker': '“适用范围”中的未验证项必须在同一行写明“阻塞：”和“解除条件：”。',
    'scope-not-last': '“适用范围”必须是文末最后一个二级标题。',
    'scope-duplicate': '每份文档只能有一个“适用范围”一节。',
    'report-missing-scope': '实测报告必须在文末设“适用范围”，并列出配置和样本数。',
    'hedge-in-comment': '代码注释出现限制句。修复代码，或把未验证项移到报告的“适用范围”。',
    'broad-except': '宽泛 except 吞掉了异常。只捕获当前能处理的异常，或重新抛出。',
}


def hedges(line):
    """返回该行命中的限制词。"""
    found = [word for word in HEDGES_ZH if word in line]
    found += [m.group(0) for m in HEDGES_ZH_RE.finditer(line)]
    return found + [m.group(0) for m in HEDGES_EN.finditer(line)]


INLINE_CODE = re.compile(r'`[^`]*`')
FENCE = re.compile(r'^\s*(```|~~~)')
BLOCKER = re.compile(r'阻塞[:：]|Blocker:', re.IGNORECASE)
RELEASE = re.compile(r'解除条件[:：]|Release condition:', re.IGNORECASE)


def violation(path, number, rule, text):
    return dict(path=path, line=number, rule=rule, text=text.strip())


def prose_lines(text):
    """返回 (行号, 去掉行内代码后的文本)。跳过围栏代码块。"""
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if match:
            fence = None if fence == match.group(1) else fence or match.group(1)
            continue
        if fence is None:
            yield number, INLINE_CODE.sub('', line)


def check_markdown(path, text):
    lines = list(prose_lines(text))
    headings = [i for i, (_, line) in enumerate(lines) if line.startswith('## ')]
    scopes = [i for i in headings if SCOPE_HEADING.match(lines[i][1])]
    found = []
    for i in scopes[:-1]:
        found.append(violation(path, lines[i][0], 'scope-duplicate', lines[i][1]))
    start = scopes[-1] if scopes else len(lines)
    if scopes and headings[-1] != start:
        found.append(violation(path, lines[start][0], 'scope-not-last', lines[start][1]))
    for i, (number, line) in enumerate(lines):
        if not hedges(line):
            continue
        if i < start:
            found.append(violation(path, number, 'hedge-in-body', line))
        elif not (BLOCKER.search(line) and RELEASE.search(line)):
            found.append(violation(path, number, 'scope-missing-blocker', line))
    if REPORT.match(path):
        scope = ' '.join(line for _, line in lines[start:])
        if not scopes or not re.search('配置|Configuration', scope, re.I) \
                or not re.search('样本|Sample', scope, re.I):
            found.append(violation(path, 0, 'report-missing-scope', ''))
    return found


def python_comments(text):
    """返回 Python 注释和文档字符串的 (行号, 文本)。"""
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            yield token.start[0], token.string
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant) \
                and isinstance(node.body[0].value.value, str):
            first = node.body[0].lineno
            for offset, line in enumerate(node.body[0].value.value.splitlines()):
                yield first + offset, line


def c_comments(text):
    """返回 C 系语言注释的 (行号, 文本)。跳过字符串和字符字面量。"""
    number, i, quote = 1, 0, None
    while i < len(text):
        char = text[i]
        if quote:
            if char == '\\':
                i += 1
            elif char == quote:
                quote = None
        elif char in '"\'`':
            quote = char
        elif text.startswith('//', i):
            end = text.find('\n', i)
            end = len(text) if end < 0 else end
            yield number, text[i:end]
            i = end
            continue
        elif text.startswith('/*', i):
            end = text.find('*/', i)
            end = len(text) if end < 0 else end + 2
            for offset, line in enumerate(text[i:end].splitlines()):
                yield number + offset, line
            number += text.count('\n', i, end)
            i = end
            continue
        if text[i] == '\n':
            number += 1
        i += 1


def broad_excepts(path, text):
    lines = text.splitlines()
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.ExceptHandler):
            continue
        broad = node.type is None or (isinstance(node.type, ast.Name)
                                      and node.type.id in ('Exception', 'BaseException'))
        exits = (ast.Return, ast.Break, ast.Continue, ast.FunctionDef,
                 ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        reraises = isinstance(node.body[-1], ast.Raise) and not any(isinstance(n, exits) for n in ast.walk(node))
        if broad and not reraises:
            yield violation(path, node.lineno, 'broad-except', lines[node.lineno-1])


def check_code(path, text):
    suffix = Path(path).suffix
    if suffix == '.py':
        comments = python_comments(text)
    elif suffix == '.sh':
        comments = ((n, l) for n, l in enumerate(text.splitlines(), 1) if l.lstrip().startswith('#'))
    else:
        comments = c_comments(text)
    found = [violation(path, n, 'hedge-in-comment', l) for n, l in comments if hedges(l)]
    if suffix == '.py':
        found += broad_excepts(path, text)
    return found


def git(*args):
    return subprocess.run(['git', '-C', str(ROOT), *args], stdout=subprocess.PIPE, check=True).stdout


INDEX = ':index'  # sources() 的特殊 ref：读取暂存区。


def wanted(name):
    suffix = Path(name).suffix
    return name not in EXEMPT and (suffix == '.md' or suffix in CODE_SUFFIXES)


def blobs(entries):
    """用一次 git cat-file --batch 读取 (路径, 对象哈希) 列表的内容。"""
    if not entries:
        return
    request = ''.join(f'{sha}\n' for _, sha in entries).encode()
    output = subprocess.run(['git', '-C', str(ROOT), 'cat-file', '--batch'], input=request,
                            stdout=subprocess.PIPE, check=True).stdout
    offset = 0
    for name, _ in entries:
        header_end = output.index(b'\n', offset)
        size = int(output[offset:header_end].split()[2])
        yield name, output[header_end+1:header_end+1+size]
        offset = header_end + 1 + size + 1


def sources(ref=None):
    """返回 (路径, 文本)。ref 为空时读取工作区；为 INDEX 时读取暂存区；否则读取该提交。"""
    if ref is None:
        names = git('ls-files', '--cached', '--others', '--exclude-standard', '-z').decode().split('\0')
        for name in sorted(n for n in set(names) if n and wanted(n) and (ROOT/n).is_file()):
            yield name, (ROOT/name).read_bytes().decode('utf-8')
        return
    entries = []
    if ref == INDEX:
        # 每项为 "<模式> <哈希> <阶段>\t<路径>"。阶段非 0 表示未解决的合并冲突。
        for line in filter(None, git('ls-files', '-s', '-z').decode().split('\0')):
            meta, name = line.split('\t', 1)
            mode, sha, stage = meta.split()
            if stage != '0':
                raise SystemExit(f'{name}: 暂存区有未解决的合并冲突')
            if mode != '160000' and wanted(name):
                entries.append((name, sha))
    else:
        for line in filter(None, git('ls-tree', '-r', '-z', ref).decode().split('\0')):
            meta, name = line.split('\t', 1)
            _, kind, sha = meta.split()
            if kind == 'blob' and wanted(name):
                entries.append((name, sha))
    for name, data in blobs(sorted(entries)):
        yield name, data.decode('utf-8')


def scan(ref=None):
    """返回 (命中列表, 已扫描路径集合)。"""
    found, paths = [], set()
    for path, text in sources(ref):
        paths.add(path)
        try:
            found += check_markdown(path, text) if path.endswith('.md') else check_code(path, text)
        except (SyntaxError, tokenize.TokenError) as error:
            raise SystemExit(f'{path}: 无法解析：{error}')
    return found, paths


def key(item):
    return item['path'], item['rule'], item['text']


def load_baseline(text):
    return Counter({(e['path'], e['rule'], e['text']): e['count'] for e in json.loads(text)['entries']})


def dump_baseline(counts):
    entries = [dict(path=p, rule=r, text=t, count=c) for (p, r, t), c in sorted(counts.items()) if c > 0]
    return json.dumps(dict(version=1, entries=entries), ensure_ascii=False, indent=1) + '\n'


def compare(found, baseline, paths):
    """返回 (新增命中, 基线中已消失的条目)。

    只有已扫描路径的条目会成为过期条目。未提交的文件不在 CI 检出中，它们的条目保留。
    """
    remaining = Counter({k: c for k, c in baseline.items() if k[0] in paths})
    new = []
    for item in found:
        if remaining[key(item)] > 0:
            remaining[key(item)] -= 1
        else:
            new.append(item)
    return new, +remaining


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--init-from', metavar='REF', help='从指定提交生成初始基线；基线已存在时拒绝')
    mode.add_argument('--prune', action='store_true', help='删除基线中已消失的条目和已删除文件的条目')
    mode.add_argument('--staged', action='store_true', help='检查暂存区内容，即将要提交的内容')
    parser.add_argument('--base-ref', metavar='REF', help='基线条目多于该提交时失败')
    args = parser.parse_args(argv)
    if args.init_from:
        if BASELINE.exists():
            parser.error('基线已存在。基线只能用 --prune 缩小')
        BASELINE.write_text(dump_baseline(Counter(map(key, scan(args.init_from)[0]))))
        return 0
    relative = str(BASELINE.relative_to(ROOT))
    if args.staged:
        try:
            text = git('show', f':{relative}').decode()
        except subprocess.CalledProcessError:
            raise SystemExit(f'{relative} 不在暂存区。运行 git add {relative}')
    else:
        text = BASELINE.read_text()
    baseline = load_baseline(text)
    found, paths = scan(INDEX if args.staged else None)
    new, stale = compare(found, baseline, paths)
    if args.prune:
        kept = Counter({k: c for k, c in baseline.items() if k[0] in paths}) - stale
        BASELINE.write_text(dump_baseline(kept))
        print(f'已删除 {sum(baseline.values()) - sum(kept.values())} 条基线条目')
        return 0
    failed = False
    if args.base_ref:
        try:
            base = load_baseline(git('show', f'{args.base_ref}:{relative}').decode())
        except subprocess.CalledProcessError:
            base = None  # 基准提交还没有基线：本次是引入基线的提交。
        grown = +(baseline - base) if base is not None else Counter()
        for (path, rule, text), count in sorted(grown.items()):
            failed = True
            print(f'{path}: baseline-grown: 基线新增 {count} 条 {rule}：{text}')
    for item in new:
        failed = True
        print(f"{item['path']}:{item['line']}: {item['rule']}: {MESSAGES[item['rule']]}\n    {item['text']}")
    for (path, rule, text), count in sorted(stale.items()):
        failed = True
        print(f'{path}: stale-baseline: 基线条目已消失（{rule}）。运行 --prune。\n    {text}')
    if failed:
        print('规则见 AGENTS.md “未解决项与限制”和“代码校验与降级”。', file=sys.stderr)
    return int(failed)


if __name__ == '__main__':
    sys.exit(main())
