"""术语表：默认词表加可选的 JSON 配置文件。

配置文件格式为 {@code {"中文词": "English meaning", ...}}。
未提供文件时使用默认词表。文件中的词条覆盖默认词条。

路径来自参数或环境变量 MOZART_GLOSSARY。文件属于外部输入，在加载时校验一次。
"""
import json
import os
import re
from pathlib import Path

DEFAULT_GLOSSARY = {
    '网赌': 'online gambling',
    '网贷': 'online lending',
    '戒赌': 'quit gambling',
    '借条': 'IOU note',
    '额度': 'credit limit',
    '套路贷': 'predatory lending',
    '高利贷': 'loan sharking',
    '催收': 'debt collection',
    '逾期': 'overdue',
    '征信': 'credit record',
    '庄家': 'the house',
    '赌徒': 'gambler',
}

CHINESE = re.compile(r'^[\u3400-\u9fff]+$')
ENGLISH = re.compile(r'^[A-Za-z][A-Za-z\-\' ]*$')


def validate(terms):
    """检查词条，返回规范化后的字典。格式错误时抛出 ValueError。"""
    if not isinstance(terms, dict):
        raise ValueError('术语表格式错误：应为 {"中文词": "英文释义"} 对象')
    cleaned = {}
    for term, meaning in terms.items():
        if not isinstance(term, str) or not CHINESE.match(term):
            raise ValueError(f'术语键必须是中文：{term!r}')
        if not isinstance(meaning, str) or not ENGLISH.match(meaning):
            raise ValueError(f'术语释义必须是英文：{term}={meaning!r}')
        cleaned[term] = ' '.join(meaning.split()).lower()
    return cleaned


def path():
    """返回配置文件路径；未配置时返回 None。"""
    configured = os.environ.get('MOZART_GLOSSARY')
    return Path(configured) if configured else None


def load(source=None):
    """返回默认词表与配置文件合并后的词表。"""
    terms = dict(DEFAULT_GLOSSARY)
    source = Path(source) if source else path()
    if source is None or not source.exists():
        return terms
    with source.open(encoding='utf-8') as handle:
        try:
            terms.update(validate(json.load(handle)))
        except json.JSONDecodeError as error:
            raise ValueError(f'术语表不是合法 JSON：{source}: {error}') from error
    return terms


def save(terms, target=None):
    """校验并写入配置文件，返回写入路径。"""
    target = Path(target) if target else path()
    if target is None:
        raise ValueError('未配置术语表路径，无法保存')
    cleaned = validate(terms)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                         encoding='utf-8')
    temporary.replace(target)
    return target
