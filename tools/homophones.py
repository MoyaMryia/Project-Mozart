"""同音纠错：把源文里与术语同音、写法不同的片段换成术语写法。

识别会把「网赌」写成「王独」、把「戒戒」写成「姐姐」。
拼音不区分这些写法，因此用术语表生成无声调拼音索引，命中就替换。

只处理中文片段，长度 2 到 4 字。术语本身不替换。
词频不低于 MIN_FREQ 的片段按真词处理，避免把「姐姐」改成同音的「戒戒」。

依赖：pypinyin 生成拼音，jieba 提供词频表。术语表为空时不加载这两个包。
"""
import json
import logging
import re
from pathlib import Path

MAX_SPAN = 4
MIN_SPAN = 2
# 词频不低于该值的片段按真词处理，不做同音替换。
MIN_FREQ = 10
_frequencies = None

CHINESE = re.compile(r'^[\u4e00-\u9fff]+$')


def _pinyin(chars):
    """返回与字符一一对应的无声调拼音。缺少 pypinyin 时抛出明确错误。"""
    try:
        from pypinyin import lazy_pinyin
    except ImportError as error:
        raise RuntimeError('术语表非空，同音纠错需要 pypinyin：pip install pypinyin') from error
    return lazy_pinyin(chars)


def load_corrections(source):
    """读取显式纠错表，格式 {"姐姐": "戒戒"}。文件不存在时返回空表。"""
    path = Path(source)
    if not path.exists():
        return {}
    with path.open(encoding='utf-8') as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as error:
            raise ValueError(f'纠错表不是合法 JSON：{path}: {error}') from error
    if not isinstance(data, dict):
        raise ValueError(f'纠错表应为 {{"原文": "改为"}} 对象：{path}')
    for key, value in data.items():
        if not CHINESE.match(key or '') or not CHINESE.match(value or ''):
            raise ValueError(f'纠错条目必须都是中文：{key!r} -> {value!r}')
    return dict(data)


def apply_corrections(text, corrections):
    """按显式纠错表替换，长词优先。"""
    if not corrections or not text:
        return text, []
    replacements = []
    for source in sorted(corrections, key=len, reverse=True):
        if source and source in text:
            count = text.count(source)
            text = text.replace(source, corrections[source])
            replacements.append({'from': source, 'to': corrections[source], 'count': count})
    return text, replacements


def frequencies():
    """返回中文词频表。首次调用时初始化，缺少 jieba 时抛出明确错误。"""
    global _frequencies
    if _frequencies is None:
        try:
            import jieba
        except ImportError as error:
            raise RuntimeError('同音纠错需要 jieba 词频表：pip install jieba') from error
        jieba.setLogLevel(logging.ERROR)
        jieba.initialize()
        _frequencies = jieba.dt.FREQ
    return _frequencies


def is_common_word(span):
    """片段是常用词时返回 True，避免把真词改成同音术语。"""
    return frequencies().get(span, 0) >= MIN_FREQ


def build_index(terms):
    """术语表 -> 拼音键 -> 术语集合。"""
    if not terms:
        return {}
    index = {}
    for term in terms:
        if not CHINESE.match(term) or not MIN_SPAN <= len(term) <= MAX_SPAN:
            continue
        index.setdefault(''.join(_pinyin(list(term))), set()).add(term)
    return index


def correct(text, terms, corrections=None):
    """返回 (纠正后的文本, 替换记录)。

    先按显式纠错表替换，再用同音索引匹配。同音部分从最长候选开始，
    命中后跳过该片段，避免重叠替换。
    """
    text, explicit = apply_corrections(text, corrections or {})
    index = build_index(terms)
    if not index or not text:
        return text, explicit
    chars = list(text)
    pinyin = _pinyin(chars)
    if len(pinyin) != len(chars):
        return text, []
    replacements = []
    out = []
    position = 0
    while position < len(chars):
        matched = False
        for size in range(MAX_SPAN, MIN_SPAN - 1, -1):
            span = ''.join(chars[position:position + size])
            if len(span) != size or not CHINESE.match(span):
                continue
            if span in terms or is_common_word(span):
                continue
            key = ''.join(pinyin[position:position + size])
            candidates = index.get(key)
            if not candidates:
                continue
            term = sorted(candidates)[0]
            if term == span:
                continue
            out.append(term)
            replacements.append({'from': span, 'to': term})
            position += size
            matched = True
            break
        if not matched:
            out.append(chars[position])
            position += 1
    return ''.join(out), explicit + replacements
