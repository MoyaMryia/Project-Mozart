"""离线语义契约：按固定样本的显式事实断言检查译文保留情况。

数据来自 tools/tests/fixtures/translation-semantics.json 的显式参考标注：
来源中文、参考译文、主体/关系/对象、数量、否定，以及 require/forbid 正则。
来源事实以来源中文为准。结构性结尾和残句谓词由各样本 facts 断言。
本模块不使用句末标点判定完整性。样本范围见 fixture 的 notes 字段。
生产翻译流程不导入本模块，只在离线回归和测试中使用。
"""
import hashlib
import json
import re
from pathlib import Path

FIXTURE_PATH = Path(__file__).resolve().parent/'tests/fixtures/translation-semantics.json'
FACT_KINDS = ('require', 'forbid')
COMPLETENESS = ('complete', 'incomplete')
# load_contract 对 fixture 校验一次；内部函数直接使用编译结果。
REQUIRED_CASE_KEYS = frozenset({'id', 'source_zh', 'reference_en', 'annotations',
                                'completeness', 'facts', 'accept', 'reject'})
ANNOTATION_KEYS = ('subject', 'relation', 'object', 'quantity', 'negation', 'completeness')
REJECT_KEYS = frozenset({'en', 'fails'})


class ContractError(ValueError):
    """fixture 结构、字段取值或正则无效。"""


class Case:
    """一个固定样本。facts 已编译，顺序与 fixture 一致。"""

    __slots__ = ('id', 'seq', 'source_zh', 'reference_en', 'annotations',
                 'completeness', 'facts', 'accept', 'reject')

    def __init__(self, case_id, seq, source_zh, reference_en, annotations,
                 completeness, facts, accept, reject):
        self.id = case_id
        self.seq = seq
        self.source_zh = source_zh
        self.reference_en = reference_en
        self.annotations = annotations
        self.completeness = completeness
        self.facts = facts
        self.accept = accept
        self.reject = reject


def failures(case, english):
    """返回未满足的断言名。顺序与 facts 书写顺序一致。"""
    failed = []
    for name, kind, patterns in case.facts:
        matched = any(pattern.search(english) for pattern in patterns)
        if matched != (kind == 'require'):
            failed.append(name)
    return failed


def passes(case, english):
    return not failures(case, english)


class Contract:
    """已校验并编译的 fixture。by_seq 只含带序号的记录样本。"""

    def __init__(self, cases, source_sha256):
        self.source_sha256 = source_sha256
        self.cases = tuple(cases)
        self.by_seq = {case.seq: case for case in self.cases if case.seq is not None}

    def checks(self, english_by_seq):
        """按序号生成检查结果。缺少该序号的译文按失败处理。"""
        return {'semantic_' + case.id: case.seq in english_by_seq and passes(case, english_by_seq[case.seq])
                for case in self.cases if case.seq is not None}


def load_contract(path=FIXTURE_PATH):
    """读取并校验 fixture。配置错误时抛 ContractError。"""
    raw = Path(path).read_bytes()
    try:
        data = json.loads(raw.decode('utf-8'))
    except json.JSONDecodeError as error:
        raise ContractError(f'fixture 不是合法 JSON：{error}') from error
    return Contract(_parse_cases(data), hashlib.sha256(raw).hexdigest())


def _text(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f'{where} 必须是非空字符串')
    return value


def _parse_cases(data):
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ContractError('fixture 需要 version 为 1')
    _text(data.get('notes'), 'notes')
    entries = data.get('cases')
    if not isinstance(entries, list) or not entries:
        raise ContractError('fixture 需要非空 cases 列表')
    shared = _parse_shared_facts(data.get('facts'))
    cases, ids, seqs = [], set(), set()
    for entry in entries:
        case = _parse_case(entry, shared, ids, seqs)
        cases.append(case)
    return cases


def _parse_shared_facts(value):
    if not isinstance(value, dict) or not value:
        raise ContractError('fixture 需要非空 facts 表')
    shared = {}
    for name, fact in value.items():
        _text(name, 'facts 键')
        shared[name] = _parse_fact(name, fact)
    return shared


def _parse_fact(name, fact):
    if not isinstance(fact, dict) or set(fact) != {'kind', 'patterns'}:
        raise ContractError(f'facts.{name} 需要 kind 和 patterns')
    kind, patterns = fact['kind'], fact['patterns']
    if kind not in FACT_KINDS:
        raise ContractError(f'facts.{name}.kind 只能是 {FACT_KINDS}')
    if not isinstance(patterns, list) or not patterns:
        raise ContractError(f'facts.{name}.patterns 必须是非空列表')
    compiled = []
    for pattern in patterns:
        _text(pattern, f'facts.{name}.patterns')
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as error:
            raise ContractError(f'facts.{name} 的正则无效：{error}') from error
    return kind, tuple(compiled)


def _parse_case(entry, shared, ids, seqs):
    if not isinstance(entry, dict):
        raise ContractError('每条 case 必须是对象')
    missing = REQUIRED_CASE_KEYS - set(entry)
    if missing:
        raise ContractError(f'case 缺少字段：{sorted(missing)}')
    case_id = _text(entry['id'], 'case.id')
    if case_id in ids:
        raise ContractError(f'case.id 重复：{case_id}')
    ids.add(case_id)
    seq = entry.get('seq')
    if seq is not None:
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise ContractError(f'{case_id}: seq 必须是整数')
        if seq in seqs:
            raise ContractError(f'{case_id}: seq 重复')
        seqs.add(seq)
    completeness = entry['completeness']
    if completeness not in COMPLETENESS:
        raise ContractError(f'{case_id}: completeness 只能是 {COMPLETENESS}')
    annotations = entry['annotations']
    if not isinstance(annotations, dict) or set(annotations) != set(ANNOTATION_KEYS):
        raise ContractError(f'{case_id}: annotations 需要 {list(ANNOTATION_KEYS)}')
    for key in ANNOTATION_KEYS:
        _text(annotations[key], f'{case_id}.annotations.{key}')
    facts = _parse_case_facts(case_id, entry['facts'], shared)
    accept = _parse_accept(case_id, entry['accept'])
    reject = _parse_reject(case_id, entry['reject'], {name for name, _, _ in facts})
    return Case(case_id, seq, _text(entry['source_zh'], case_id + '.source_zh'),
                _text(entry['reference_en'], case_id + '.reference_en'),
                annotations, completeness, facts, accept, reject)


def _parse_case_facts(case_id, value, shared):
    if not isinstance(value, list) or not value:
        raise ContractError(f'{case_id}: facts 必须是非空名称列表')
    compiled = []
    for name in value:
        _text(name, f'{case_id}.facts')
        if name not in shared:
            raise ContractError(f'{case_id}: facts 引用未定义名称 {name}')
        kind, patterns = shared[name]
        compiled.append((name, kind, patterns))
    return tuple(compiled)


def _parse_accept(case_id, value):
    if not isinstance(value, list) or not value:
        raise ContractError(f'{case_id}: accept 必须是非空译文列表')
    return tuple(_text(english, f'{case_id}.accept') for english in value)


def _parse_reject(case_id, value, fact_names):
    if not isinstance(value, list) or not value:
        raise ContractError(f'{case_id}: reject 必须是非空列表')
    allowed = fact_names | {'completeness'}
    parsed = []
    for item in value:
        if not isinstance(item, dict) or set(item) != REJECT_KEYS:
            raise ContractError(f'{case_id}: reject 项需要 en 和 fails')
        english = _text(item['en'], f'{case_id}.reject.en')
        fails = item['fails']
        if not isinstance(fails, list) or not fails:
            raise ContractError(f'{case_id}: reject.fails 必须是非空列表')
        for name in fails:
            if name not in allowed:
                raise ContractError(f'{case_id}: reject.fails 引用未知断言 {name}')
        parsed.append({'en': english, 'fails': tuple(fails)})
    return tuple(parsed)
