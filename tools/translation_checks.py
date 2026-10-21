"""Conservative checks for explicit digits, spoken years and large quantities.

This is not a semantic translation evaluator. It catches dropped/mutated numbers
that the small translator has actually produced in the project's replay tests.
"""
import re
from collections import Counter

DIGITS = {c: i for i, c in enumerate('零一二三四五六七八九')}
DIGITS.update({'〇': 0, '两': 2})
UNITS = {'十': 10, '百': 100, '千': 1000}
MONTHS = ('January', 'February', 'March', 'April', 'May', 'June',
          'July', 'August', 'September', 'October', 'November', 'December')
MONTH_PATTERN = r'(?<!\d)(1[0-2]|[1-9])月'
SMALL_WORDS = ('zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven',
               'eight', 'nine', 'ten', 'eleven', 'twelve')
MIN_TERM_RUN = 3
LATIN_TERMS = re.compile(r'[A-Za-z](?:\.[A-Za-z])+\.?|[A-Za-z][A-Za-z0-9]*')
TRANSLATION_TOKENS = re.compile(LATIN_TERMS.pattern+r'|\d+')
ENGLISH_SMALL = {word: value for value, word in enumerate(
    SMALL_WORDS+('thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen'))}
ENGLISH_TENS = {word: value for word, value in zip(
    ('twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety'), range(20, 100, 10))}
ENGLISH_SCALES = {'hundred': 100, 'thousand': 1000, 'million': 1000000, 'billion': 1000000000}
ENGLISH_NUMBER_WORD = (r'(?:'+ '|'.join((*ENGLISH_SMALL, *ENGLISH_TENS, *ENGLISH_SCALES))+
    r'|(?:a|an)(?=\s+(?:'+'|'.join(ENGLISH_SCALES)+r')\b))')
ENGLISH_NUMBER_PHRASE = ENGLISH_NUMBER_WORD+r'(?:[\s-]+(?:(?:and|or|to)\s+)?'+ENGLISH_NUMBER_WORD+r')*'
ENGLISH_NUMBER = re.compile(r'\b(?:between\s+)?'+ENGLISH_NUMBER_PHRASE+r'\b', re.I)

def _english_integer(words):
    """解析按递减数量级组成的英文整数，拒绝不完整的数字词组。"""
    for scale_word, scale in sorted(ENGLISH_SCALES.items(), key=lambda item: item[1], reverse=True):
        if scale_word not in words:
            continue
        if words.count(scale_word) != 1:
            return None
        index = words.index(scale_word)
        head, tail = words[:index], words[index+1:]
        high = 1 if head in (['a'], ['an']) else _english_integer(head)
        if tail[:1] == ['and']:
            tail = tail[1:]
        low = _english_integer(tail) if tail else 0
        if high is None or low is None or not 0 < high < (10 if scale == 100 else 1000) or not 0 <= low < scale:
            return None
        return high*scale+low
    if len(words) == 1:
        return ENGLISH_SMALL.get(words[0], ENGLISH_TENS.get(words[0]))
    if len(words) == 2 and words[0] in ENGLISH_TENS and 0 < ENGLISH_SMALL.get(words[1], 0) < 10:
        return ENGLISH_TENS[words[0]]+ENGLISH_SMALL[words[1]]
    return None


def _range_values(left_words, right_words):
    left, right = _english_integer(left_words), _english_integer(right_words)
    if left is None or right is None:
        return None
    right_scales = [w for w in right_words if w in ENGLISH_SCALES]
    if not right_scales:
        return None
    suffix = []
    for word in reversed(right_words):
        if word not in ENGLISH_SCALES:
            break
        suffix.insert(0, word)
    left_scales = [w for w in left_words if w in ENGLISH_SCALES]
    explicit_left = left
    if not left_scales:
        factor = 1
        for word in suffix:
            factor *= ENGLISH_SCALES[word]
        left *= max(factor, max(ENGLISH_SCALES[w] for w in right_scales))
    elif suffix[:len(left_scales)] == left_scales:
        for word in suffix[len(left_scales):]:
            left *= ENGLISH_SCALES[word]
    if left != explicit_left and left >= right:
        left = explicit_left
    return left, right


def english_quantity_digits(text):
    """仅在数字核对中展开英文整数及共享数量级的范围，保留字幕原文。"""
    def number_digits(match):
        tokens = re.split(r'[\s-]+', match[0].casefold())
        between = tokens[:1] == ['between']
        if between:
            tokens = tokens[1:]
        operators = ['and'] if between else ['or', 'to']
        splits = [i for i, t in enumerate(tokens) if t in operators]
        if splits:
            values = {_range_values(tokens[:i], tokens[i+1:]) for i in splits}
            values.discard(None)
            if len(values) == 1:
                return ' '.join(str(v) for v in values.pop())
            return match[0]
        value = _english_integer(tokens)
        if value is not None:
            if value >= len(SMALL_WORDS) or any(t in ENGLISH_SCALES for t in tokens):
                return str(value)
            return match[0]
        # 完整数额并列时保留每次出现。存在可合并的相邻词组时保留原串。
        parts = []
        start = 0
        for i, token in enumerate(tokens):
            if token == 'and':
                parts.append(tokens[start:i])
                start = i+1
        parts.append(tokens[start:])
        values = [_english_integer(part) for part in parts]
        if len(parts) > 1 and all(v is not None for v in values) and all(
                _english_integer(a+['and']+b) is None for a, b in zip(parts, parts[1:])):
            return ' and '.join(str(v) if v >= len(SMALL_WORDS) or any(t in ENGLISH_SCALES for t in part)
                else ' '.join(part) for part, v in zip(parts, values))
        return match[0]
    return ENGLISH_NUMBER.sub(number_digits, text)


def source_term_counts(source):
    """保留源文拉丁术语的拼写与出现次数。"""
    terms = LATIN_TERMS.findall(source)
    counts = Counter(term.replace('.', '').casefold() for term in terms)
    spellings = {term.replace('.', '').casefold(): term for term in terms}
    return [{'term': spellings[term], 'source_count': count} for term, count in counts.items()]


def repeated_source_terms(source, translation):
    """检查译文中超过原文次数的连续拉丁术语循环。"""
    longest = Counter()
    previous, run = None, 0
    for token in TRANSLATION_TOKENS.findall(translation):
        token = token.replace('.', '').casefold()
        run = run+1 if token == previous else 1
        longest[token] = max(longest[token], run)
        previous = token
    issues = []
    for item in source_term_counts(source):
        run = longest[item['term'].replace('.', '').casefold()]
        if run >= MIN_TERM_RUN and run > item['source_count']:
            issues.append({**item, 'longest_run': run})
    return issues


def translation_number_counts(translation):
    translation = english_quantity_digits(translation)
    actual = Counter(re.findall(r'\d+(?:\.\d+)?', translation.replace(',', '')))
    # Accept an equivalent small count only with an explicit unit. An unrelated
    # "one" or modal "may" must not mask a missing source quantity.
    for value, word in enumerate(SMALL_WORDS):
        actual[str(value)] += len(re.findall(r'\b'+word+r'\s+(?:times|fold|days?|months?|years?|yuan)\b', translation, flags=re.I))
    return actual


def integer(text):
    if text.isdecimal():
        return int(text)
    total = current = 0
    for character in text:
        if character in DIGITS:
            current = DIGITS[character]
        elif character in UNITS:
            total += (current or 1)*UNITS[character]
            current = 0
    return total+current


def _exact_section(text):
    """Parse a standard Chinese section below 10,000; ambiguous forms fail.

    This is deliberately stricter than integer(): repeated units/digit strings
    must not turn clipped recognition into an asserted amount.
    """
    leading_zero = text.startswith(('零', '〇'))
    text = text.lstrip('零〇')
    if not text:
        return 0
    total = 0
    previous_unit = 10000
    digit = None
    zero_after_unit = False
    for c in text:
        if c in DIGITS:
            if DIGITS[c] == 0:
                if digit is not None:
                    return None
                zero_after_unit = True
                continue
            if digit is not None:
                return None
            digit = DIGITS[c]
        elif c in UNITS:
            unit = UNITS[c]
            if unit >= previous_unit or (digit is None and not (c == '十' and total == 0)):
                return None
            total += (digit if digit is not None else 1) * unit
            digit = None
            previous_unit = unit
            zero_after_unit = False
        else:
            return None
    if digit is not None and previous_unit > 10 and not (leading_zero or zero_after_unit):
        # 五千五 / 七万五 are colloquial shorthand, not uniquely 5005 /70005.
        if total:
            return None
    return total + (digit or 0)


def _exact_composite(text):
    """Return an exact composite amount, or None for a range/broken numeral."""
    if '万' not in text and '亿' not in text:
        return None
    # Simple/range forms already have their existing handling. Only replace a
    # composite with a nonempty suffix; never partially rewrite mixed 2万5千.
    if not re.search(r'[万亿].+', text):
        return None
    if text.count('亿') > 1 or text.count('万') > 1:
        return None
    total = 0
    if '亿' in text:
        head, text = text.split('亿')
        value = _exact_section(head)
        if not value:
            return None
        total += value * 100000000
    if '万' in text:
        head, text = text.split('万')
        value = _exact_section(head)
        if not value:
            return None
        total += value * 10000
    if text and all(c in DIGITS for c in text) and not text.startswith(('零', '〇')):
        return None
    value = _exact_section(text)
    return None if value is None else total + value


def normalize_exact_composites(text):
    """Atomically normalize unambiguous Chinese composite quantities only."""
    numerals = '零〇一二两三四五六七八九十百千万亿'
    def replace(match):
        value = _exact_composite(match[0])
        return str(value) if value is not None else match[0]
    return re.sub(r'(?<![\d'+numerals+r'])['+numerals+r']+(?![\d'+numerals+r'])', replace, text)


def decade(text):
    """把口语年代写成 2010年代，例如 二零一几年、201几年。"""
    def chinese(match):
        return str(int(''.join(str(DIGITS[c]) for c in match[1]))*10)+'年代'
    def arabic(match):
        return str(int(match[1])*10)+'年代'
    return DECADE_AR.sub(arabic, DECADE_CN.sub(chinese, text))


def required_number_counts(text):
    text = decade(normalize_tail_amounts(normalize_exact_composites(text)))
    numbers = Counter()
    for digits, unit in re.findall(r'(\d+(?:\.\d+)?)([万亿千百]?)', text.replace(',', '')):
        # 识别残句会把「一千」写成「一00」。前导零的纯数字串跳过，不参与金额核对。
        if not unit and len(digits) > 1 and digits.startswith('0') and '.' not in digits:
            continue
        if unit:
            value = float(digits)*{'万':10000, '亿':100000000, '千':1000, '百':100}[unit]
            numbers[str(int(value)) if value.is_integer() else str(value)] += 1
        else:
            numbers[digits] += 1
    # Read a year digit by digit, e.g. 一九八七年.
    for year in re.findall(r'([零〇一二三四五六七八九]{4})年', text):
        numbers[''.join(str(DIGITS[c]) for c in year)] += 1
    # Read a spoken decade, e.g. 二零一几年 is 2010年代.
    for spoken in DECADE_CN.findall(text):
        numbers[str(int(''.join(str(DIGITS[c]) for c in spoken))*10)] += 1
    for digits in DECADE_AR.findall(text):
        numbers[str(int(digits)*10)] += 1
    def large(match):
        amount, unit = match.groups()
        scale = {'万':10000, '亿':100000000}[unit]
        if len(amount) == 2 and all(c in DIGITS for c in amount):
            # Conversational ranges: 七八万 means 70,000–80,000.
            numbers.update(str(DIGITS[c]*scale) for c in amount)
        else:
            numbers[str(integer(amount)*scale)] += 1
        return ''
    remainder = re.sub(r'([零〇一二两三四五六七八九十百千]+)(万|亿)', large, text)
    for amount in re.findall(r'[零〇一二两三四五六七八九十百千]+', remainder):
        if not any(unit in amount for unit in ('百','千')):
            continue
        if not any(c in DIGITS for c in amount) and '十' not in amount:
            # 几千/数百 are vague quantities, not explicit 1,000/100.
            continue
        if len(amount) == 3 and amount[0] in DIGITS and amount[1] in DIGITS and amount[2] in ('百','千'):
            numbers.update(str(DIGITS[c]*UNITS[amount[2]]) for c in amount[:2])
        else:
            # Do not sum repeated units in an ambiguous recognition result.
            value = _exact_section(amount)
            if value is not None:
                numbers[str(value)] += 1
    return numbers


def required_numbers(text):
    return sorted(required_number_counts(text), key=lambda n: float(n))


# 口语短式：两万七 = 27,000，两亿七 = 270,000,000。
# 后一个字不能是百/千/十，否则是完整复合数（两万七千），由 _exact_composite 处理。
# 口语年代：二零一几年与 201几年 都表示 2010 年代。几 表示同十年内。
DECADE_CN = re.compile(r'([零〇一二三四五六七八九]{3})几(?:年代|年)')
DECADE_AR = re.compile(r'(\d{3})几(?:年代|年)')

TAIL_AMOUNT = re.compile(r'(?<![\d零〇一二两三四五六七八九十百千万亿])'
                         r'([零〇一二两三四五六七八九十百千]+)(万|亿)([一二三四五六七八九])'
                         r'(?![百千十\d万亿])')


def normalize_tail_amounts(text):
    """把口语短式补成完整金额，供译文提示和数字核对共用。"""
    def replace(match):
        whole, unit, digit = match.groups()
        scale = {'万': 10000, '亿': 100000000}[unit]
        return str(integer(whole)*scale + DIGITS[digit]*(scale//10))
    return TAIL_AMOUNT.sub(replace, text)


def normalize_translation_quantities(text):
    """Spell explicit 万/亿 quantities as equivalent Arabic numerals for the LLM.

    Keep the original source for captions and validation. Do not guess vague
    quantities (几万), broken mixed numerals (一0百), dates or idioms.
    """
    text = normalize_tail_amounts(normalize_exact_composites(text))
    text = decade(text)
    def arabic(match):
        value = float(match[1])*{'万':10000, '亿':100000000}[match[2]]
        return str(int(value)) if value.is_integer() else str(value)
    numerals = '零〇一二两三四五六七八九十百千万亿'
    # Exact Chinese composites were handled atomically above. Do not partially
    # replace unresolved composites or mixed 2万5千 into adjacent digit strings.
    text = re.sub(r'(?<![\d'+numerals+r'])(\d+(?:\.\d+)?)([万亿])(?![\d'+numerals+r'])', arabic, text)
    def chinese(match):
        amount, unit = match.groups()
        scale = {'万':10000, '亿':100000000}[unit]
        if len(amount) == 2 and all(c in DIGITS for c in amount):
            return '–'.join(str(DIGITS[c]*scale) for c in amount)
        return str(integer(amount)*scale)
    return re.sub(r'(?<![\d'+numerals+r'])([零〇一二两三四五六七八九十百千]+)(万|亿)(?![\d'+numerals+r'])', chinese, text)


def repeated_numbers(source, translation):
    required = required_number_counts(source)
    actual = translation_number_counts(translation)
    # A spelled-out calendar name plus an added numeric amount still contains
    # two occurrences of that value: "June ... won 6" is not a faithful 6月底.
    for month in set(re.findall(MONTH_PATTERN, source)):
        actual[month] += len(re.findall(r'\b'+MONTHS[int(month)-1]+r'\b',translation))
    return [n for n in required if actual[n] > required[n]]


def added_large_numbers(source, translation):
    """Reject invented explicit amounts/years, without guessing small counts.

    A vague source such as 几千 does not license a specific 70,000. This check
    does not claim to validate roles, idioms or the meaning around an amount.
    """
    required = required_number_counts(source)
    return sorted((n for n, count in translation_number_counts(translation).items()
                   if count and float(n) >= 1000 and n not in required), key=float)


# 原文的否定标记。命中任意一个就不再检查，避免误伤「应付」「免不了」这类正常用法。
NEGATION_ZH = re.compile(r'不|没|别|未|无|非|免|拒绝|否|禁止|取消|避免|停止|缺乏')
# 译文中的否定。know、now 这类词靠词界排除。
NEGATION_EN = re.compile(r"\b(?:not|never|without|none|nothing|nobody|nowhere|cannot|no)\b|n't\b",
                         re.IGNORECASE)


def added_negation(source, translation):
    """列出原文没有否定时译文凭空的否定词。

    只覆盖显式否定。原文缺少否定词而用其他方式表达否定时，本检查会多报，
    因此调用方按重试处理，由第二次译文决定结果。
    """
    if NEGATION_ZH.search(source):
        return []
    return [match.group(0) for match in NEGATION_EN.finditer(translation)]


def values_changed_to_item_counts(source, translation):
    """Catch the measured 'goods worth 38,000' -> '38,000 items' error.

    Only explicit source value constructions qualify; this is a narrow check,
    not a semantic evaluator or an inference about unspecified currency.
    """
    normalized = normalize_translation_quantities(source)
    values = re.findall(r'(?:等值的商品|价值(?:超过)?)(\d+(?:\.\d+)?)', normalized)
    english = english_quantity_digits(translation).replace(',', '')
    return [n for n in values if re.search(r'\b'+re.escape(n)+r'\+?\s+(?:items|products|goods)\b',english,re.I)]


def missing_numbers(source, translation):
    actual = translation_number_counts(translation)
    # A calendar translation such as 2022年5月 -> May 2022 preserves the
    # month. Do not let a month name satisfy another quantity with that value.
    other_values = set(required_numbers(re.sub(MONTH_PATTERN, '', source)))
    for month in re.findall(MONTH_PATTERN, source):
        if month not in other_values and re.search(r'\b'+MONTHS[int(month)-1]+r'\b', translation):
            actual[month] += 1
    required = required_number_counts(source)
    return [n for n in required_numbers(source) if actual[n] < required[n]]


def source_role_constraints(source):
    """Hints for the two reproduced explicit-source failures, not coreference."""
    hints = []
    if re.search(r'他(?:也)?不管', source):
        hints.append('Keep the explicit 他不管 clause in third person (he/him or they/them); do not turn it into I/me.')
    if re.search(r'(?:妈妈|母亲)[^。！？!?；;]{0,20}不相信', source) and '自己' not in source:
        hints.append('The mother-doubt clause has no explicit reflexive object. Do not add herself or invent what she doubts. Preserve any unfinished clause as unfinished.')
    return hints


def changed_explicit_roles(source, translation):
    """Flag narrow measured losses; passing is not a semantic quality score."""
    issues = []
    if re.search(r'他(?:也)?不管', source) and not re.search(r'\b(?:he|him|his|they|them|their)\b', translation, re.I):
        issues.append('explicit_third_person_missing')
    if re.search(r'(?:妈妈|母亲)[^。！？!?；;]{0,20}不相信', source) and '自己' not in source and re.search(r'\bherself\b', translation, re.I):
        issues.append('unsupported_mother_reflexive')
    return issues


def changed_loan_repayment(source, translation):
    """Reject the measured explicit 还贷款 -> borrowing error; not full semantics."""
    if '还贷款' not in source:
        return False
    repayment = re.search(r'\b(?:repay(?:s|ing|ment|ments|ed)?|repaid|pay(?:ing|s)?\s+(?:back|off)|paid\s+(?:back|off)|settle(?:d|s)?\s+(?:the\s+)?loan)\b', translation, re.I)
    borrowing = re.search(r'\b(?:take|taking|took|taken|get|getting|got|borrow(?:ed|ing|s)?)\s+(?:out\s+)?(?:a|the|another)?\s*loan\b', translation, re.I)
    return not repayment or (bool(borrowing) and '借' not in source)
