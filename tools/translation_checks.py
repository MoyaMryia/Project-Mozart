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


def translation_number_counts(translation):
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


def required_number_counts(text):
    text = normalize_exact_composites(text)
    numbers = Counter()
    for digits, unit in re.findall(r'(\d+(?:\.\d+)?)([万亿千百]?)', text.replace(',', '')):
        if unit:
            value = float(digits)*{'万':10000, '亿':100000000, '千':1000, '百':100}[unit]
            numbers[str(int(value)) if value.is_integer() else str(value)] += 1
        else:
            numbers[digits] += 1
    # Read a year digit by digit, e.g. 一九八七年.
    for year in re.findall(r'([零〇一二三四五六七八九]{4})年', text):
        numbers[''.join(str(DIGITS[c]) for c in year)] += 1
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


def normalize_translation_quantities(text):
    """Spell explicit 万/亿 quantities as equivalent Arabic numerals for the LLM.

    Keep the original source for captions and validation. Do not guess vague
    quantities (几万), broken mixed numerals (一0百), dates or idioms.
    """
    text = normalize_exact_composites(text)
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


def values_changed_to_item_counts(source, translation):
    """Catch the measured 'goods worth 38,000' -> '38,000 items' error.

    Only explicit source value constructions qualify; this is a narrow check,
    not a semantic evaluator or an inference about unspecified currency.
    """
    normalized = normalize_translation_quantities(source)
    values = re.findall(r'(?:等值的商品|价值(?:超过)?)(\d+(?:\.\d+)?)', normalized)
    english = translation.replace(',', '')
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
