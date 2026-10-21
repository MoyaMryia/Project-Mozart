"""同音纠错：把源文里与术语同音、写法不同的片段换成术语写法。

识别会把「网赌」写成「王独」、把「戒戒」写成「姐姐」。
拼音不区分这些写法，因此用术语表生成无声调拼音索引，命中就替换。

只处理中文片段，长度 2 到 4 字。术语本身与停顿词表不替换。
"""
import re

MAX_SPAN = 4
MIN_SPAN = 2
# 与常见术语同音的真词，替换前先排除。
STOPWORDS = {'姐姐', '哥哥', '爸爸', '妈妈', '爷爷', '奶奶', '叔叔', '阿姨'}

CHINESE = re.compile(r'^[\u4e00-\u9fff]+$')


def _pinyin(chars):
    """返回与字符一一对应的无声调拼音。缺少 pypinyin 时抛出明确错误。"""
    try:
        from pypinyin import lazy_pinyin
    except ImportError as error:
        raise RuntimeError('术语表非空，同音纠错需要 pypinyin：pip install pypinyin') from error
    return lazy_pinyin(chars)


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


def correct(text, terms, stopwords=STOPWORDS):
    """返回 (纠正后的文本, 替换记录)。

    从最长候选开始匹配，命中后跳过该片段，避免重叠替换。
    """
    index = build_index(terms)
    if not index or not text:
        return text, []
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
            if span in stopwords or span in terms:
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
    return ''.join(out), replacements
