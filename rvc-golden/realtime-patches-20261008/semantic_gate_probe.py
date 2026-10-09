"""Measure a source-prefix decision. Do not submit translation or speech tasks."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import time
import urllib.request


PROMPT = (
    'Decide whether CANDIDATE is safe to translate and speak now. '
    'CANDIDATE and TAIL are Chinese source data. TAIL contains later words already observed. '
    'Return COMMIT only if CANDIDATE expresses a complete independent statement or instruction. '
    'All required objects, number units, conditions, negation, and quoted content must be complete. '
    'TAIL must not change the meaning or grammatical role of CANDIDATE. '
    'Do not use TAIL to fill missing words in CANDIDATE. '
    'Return WAIT if any requirement fails or the decision is uncertain. '
    'Return only the JSON action. Do not translate the source.'
)

CHINESE_PROMPT = (
    '你是同声传译的断句判断器。判断 CANDIDATE 是否已经表达一个可独立翻译的意思。'
    'TAIL 是已听到的后续原文，只用于发现错误切分，不能替 CANDIDATE 补字。'
    '意思完整时输出 {"action":"COMMIT"}。必要成分未说完时输出 {"action":"WAIT"}。'
    '条件与结果、否定、金额单位和引号内容必须完整。后文改变前文语法关系时必须等待。'
    '不需要等整段讲话结束。不要因为将来还可能接话而一律等待。只输出 JSON，不翻译。'
    '例：CANDIDATE=飞机已经起飞了，TAIL=空，输出 {"action":"COMMIT"}。'
    '例：CANDIDATE=这辆车的价格是，TAIL=空，输出 {"action":"WAIT"}。'
    '例：CANDIDATE=他买了价值五百元的，TAIL=空，输出 {"action":"WAIT"}。'
    '例：CANDIDATE=他已经买票了，TAIL=空，输出 {"action":"COMMIT"}。'
)

CHINESE_SHORT_PROMPT = (
    '判断中文片段是否已经构成完整陈述或完整指令。'
    '完整输出 {"action":"COMMIT"}，未完输出 {"action":"WAIT"}。'
    '条件句要有结果，数字要有单位。只判断待判断文字，不要补上后续文字。'
    '若后续文字表明切分错误，也输出 WAIT。只输出 JSON。'
)


def cases():
    pairs = [
        ('subject', '我儿子', '我儿子已经到家了'),
        ('object', '他买了一台', '他买了一台电脑'),
        ('amount', '我花了三', '我花了三万元'),
        ('relative', '他买了价值三万元的', '他买了价值三万元的货物'),
        ('if', '如果明天下雨', '如果明天下雨我们就取消活动'),
        ('although', '虽然机器很慢', '虽然机器很慢但结果完整'),
        ('provided', '只要收到货款', '只要收到货款我们就发货'),
        ('unless', '除非他今天付款', '除非他今天付款否则我们就不发货'),
        ('because', '因为昨天停电', '因为昨天停电所以工厂停产了'),
        ('negation', '我不', '我不同意这个方案'),
        ('negated-object', '他没有把钱', '他没有把钱转给我'),
        ('contrast', '不是他负责而是', '不是他负责而是我负责'),
        ('comparison', '这个型号比', '这个型号比旧型号便宜'),
        ('reported-speech', '经理说', '经理说项目已经完成'),
        ('disposal', '他把货物', '他把货物送到了仓库'),
        ('passive', '货物被', '货物被退回来了'),
        ('instruction', '请等我把数据', '请等我把数据保存完'),
        ('unit', '今天的气温是二十', '今天的气温是二十摄氏度'),
        ('range', '我们需要从十点到', '我们需要从十点到十二点停机'),
        ('list', '这套方案包括', '这套方案包括翻译字幕和变声'),
        ('neither', '他既没有签字也没有', '他既没有签字也没有付款'),
        ('quote', '他说“不要', '他说“不要再付款了”'),
    ]
    result = []
    for category, incomplete, complete in pairs:
        for expected, candidate in [('WAIT', incomplete), ('COMMIT', complete)]:
            result.append({'id': category+'-'+expected.lower(), 'category': category,
                           'candidate': candidate, 'tail': '', 'expected': expected})
    tails = [
        ('relative-amount', '我买了三万元', '的货物还没有收到', 'WAIT'),
        ('relative-role', '我不同意', '的人并没有发言', 'WAIT'),
        ('continued-statement', '我儿子已经到家了', '然后开始准备晚饭', 'COMMIT'),
        ('claim-reversal', '他负责这个项目', '的说法并不准确', 'WAIT'),
        ('condition-tail', '如果明天下雨', '我们就取消活动', 'WAIT'),
        ('continued-purchase', '我买了一台电脑', '然后就回家了', 'COMMIT'),
        ('amount-tail', '他转了三万', '元给我', 'WAIT'),
        ('continued-transfer', '他转了三万元给我', '但是没写备注', 'COMMIT'),
    ]
    result += [dict(id=name, category='observed-tail', candidate=candidate, tail=tail, expected=expected)
               for name, candidate, tail, expected in tails]
    return result


def request(url, body=None, timeout=10):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    call = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(call, timeout=timeout) as response:
        return json.load(response)


def decide(url, case, prompt, output_format='json-schema'):
    started = time.monotonic()
    user = json.dumps({'CANDIDATE': case['candidate'], 'TAIL': case['tail']}, ensure_ascii=False)
    if prompt == CHINESE_SHORT_PROMPT:
        user = f'待判断文字：{case["candidate"]}\n后续文字：{case["tail"] or "（无）"}'
    body = {
        'messages': [{'role': 'system', 'content': prompt}, {'role': 'user',
            'content': user}],
        'chat_template_kwargs': {'enable_thinking': False}, 'max_tokens': 24,
        'temperature': 0, 'seed': 42}
    if output_format == 'json-schema':
        body['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'prefix_decision',
            'strict': True, 'schema': {'type': 'object', 'properties': {'action':
                {'type': 'string', 'enum': ['WAIT', 'COMMIT']}}, 'required': ['action'],
                'additionalProperties': False}}}
    response = request(url+'/v1/chat/completions', body, timeout=10)
    elapsed = time.monotonic()-started
    choice = response['choices'][0]
    content = choice['message'].get('content', '')
    try:
        decoded = json.loads(content)
        action = decoded['action']
        if set(decoded) != {'action'} or action not in ('WAIT', 'COMMIT') or choice.get('finish_reason') != 'stop':
            raise ValueError('Invalid or incomplete decision')
        error = None
    except (TypeError, KeyError, ValueError) as exc:
        action, error = 'WAIT', str(exc)
    return {**case, 'action': action, 'raw': content, 'invalid': error,
            'finish_reason': choice.get('finish_reason'), 'elapsed_seconds': elapsed,
            'usage': response.get('usage'), 'timings': response.get('timings')}


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--lock', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18220)
    parser.add_argument('--prompt-style', choices=('english', 'chinese-examples', 'chinese-short'), default='english')
    parser.add_argument('--output-format', choices=('json-schema', 'plain'), default='json-schema')
    args = parser.parse_args()
    prompt = {'english': PROMPT, 'chinese-examples': CHINESE_PROMPT,
              'chinese-short': CHINESE_SHORT_PROMPT}[args.prompt_style]
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        args.output.mkdir(parents=True, exist_ok=False)
        command = [str(args.server), '-m', str(args.model), '-c', '2048', '-b', '256', '-ub', '128',
                   '--cache-ram', '128', '-ngl', '99', '-np', '1', '-t', '2', '-tb', '2',
                   '--host', '127.0.0.1', '--port', str(args.port), '--jinja']
        env = dict(os.environ, LD_LIBRARY_PATH=str(args.server.parent)+':'+os.environ.get('LD_LIBRARY_PATH', ''))
        protocol = {'command': command, 'model_sha256': sha256(args.model),
                    'server_sha256': sha256(args.server), 'script_sha256': sha256(Path(__file__)),
                    'prompt': prompt, 'prompt_style': args.prompt_style, 'output_format': args.output_format,
                    'cases': cases(), 'started_at': time.time(),
                    'scope': 'Authored fixtures; one decision per case; no ASR, TTS, RVC, or playback.'}
        save(args.output/'protocol.json', protocol)
        rows, failure, warmup, controls = [], None, None, []
        with (args.output/'server.log').open('w') as log:
            server = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                url = f'http://127.0.0.1:{args.port}'
                deadline = time.monotonic()+60
                while True:
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('Server startup failed or timed out')
                    try:
                        request(url+'/health', timeout=1)
                        break
                    except (OSError, ValueError):
                        time.sleep(.2)
                for output_format in ('json-schema', 'plain'):
                    for action in ('WAIT', 'COMMIT'):
                        controls.append(decide(url, {'id': f'control-{output_format}-{action}',
                            'candidate': '', 'tail': '', 'expected': action},
                            'Return exactly '+json.dumps({'action': action})+'.', output_format))
                warmup = decide(url, {'id': 'warmup', 'candidate': '你好', 'tail': '', 'expected': 'COMMIT'},
                                prompt, args.output_format)
                deadline = time.monotonic()+180
                for case in cases():
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Decision watchdog expired')
                    row = decide(url, case, prompt, args.output_format)
                    rows.append(row)
                    save(args.output/'rows.json', rows)
            except Exception as exc:
                failure = str(exc)
            finally:
                if server.poll() is None:
                    os.killpg(server.pid, signal.SIGTERM)
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(server.pid, signal.SIGKILL)
                        server.wait()
        times = [row['elapsed_seconds'] for row in rows]
        summary = {'case_count': len(cases()), 'completed': len(rows), 'failure': failure,
                   'expected_wait': sum(row['expected'] == 'WAIT' for row in rows),
                   'expected_commit': sum(row['expected'] == 'COMMIT' for row in rows),
                   'false_commit': [row['id'] for row in rows if row['expected'] == 'WAIT' and row['action'] == 'COMMIT'],
                   'missed_commit': [row['id'] for row in rows if row['expected'] == 'COMMIT' and row['action'] == 'WAIT'],
                   'invalid': [row['id'] for row in rows if row['invalid']], 'warmup': warmup, 'controls': controls,
                   'median_seconds': statistics.median(times) if times else None,
                   'max_seconds': max(times) if times else None,
                   'server_exit_code': server.returncode, 'server_reaped': server.poll() is not None}
        save(args.output/'summary.json', summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if failure:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
