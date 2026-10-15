"""用小型线性模型估计当前音色的播报时长。运行时只使用标准库。"""
import json
import math
from pathlib import Path
import re

from speech_chunks import estimated_audio_seconds, word_units

FEATURE_VERSION = 1
FEATURE_NAMES = ['intercept', 'words', 'approximate_syllables', 'clause_pauses']
DEFAULT_PROFILE = Path(__file__).with_name('duration_profiles')/'qiqi-pocket.json'


def duration_features(text):
    words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|[0-9][0-9,.:]*", text)
    syllables = 0
    for word in words:
        letters = re.sub('[^a-z]', '', word.lower())
        count = len(re.findall('[aeiouy]+', letters))
        if letters.endswith('e') and not letters.endswith(('le', 'ye')) and count > 1:
            count -= 1
        syllables += max(1, count) if letters else word_units(word)*2
    return [1., float(len(words)), float(syllables), float(len(re.findall('[,;:]', text)))]


class DurationEstimator:
    def __init__(self, path=DEFAULT_PROFILE):
        self.profile = None
        self.warning = None
        try:
            data = json.loads(Path(path).read_text())
            coefficients = data['coefficients']
            margin = data['reserve_margin_seconds']
            if (data['feature_version'] != FEATURE_VERSION or data['features'] != FEATURE_NAMES
                    or len(coefficients) != len(FEATURE_NAMES)
                    or any(isinstance(c, bool) or not math.isfinite(c) or c < 0 for c in coefficients)
                    or isinstance(margin, bool) or not math.isfinite(margin) or not 0 <= margin <= 5
                    or not isinstance(data['runtime'], dict) or not data['runtime'].get('model_sha256')
                    or not isinstance(data['id'], str) or not data['id']
                    or not 1 <= data['min_words'] <= data['max_words'] <= 100
                    or not re.fullmatch('[0-9a-f]{64}', data['voice_revision'])):
                raise ValueError('Invalid duration profile')
            self.profile = data
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.warning = str(error)

    def predict(self, texts, language, voice_revision, runtime):
        profile = self.profile
        reason = 'profile_unavailable'
        if profile:
            reason = 'scope_mismatch'
            matches = (language == 'en' and voice_revision == profile['voice_revision'] and runtime
                and all(runtime.get(key) == value for key, value in profile['runtime'].items()))
            if matches:
                # 数字、逐字母缩写和非英语字符没有足够训练样本。使用保守粗估。
                unusual = any(re.search(r'\d|\b[A-Z]{2,}\b|[^\x00-\x7f]', text) for text in texts)
                in_range = all(profile['min_words'] <= duration_features(text)[1] <= profile['max_words'] for text in texts)
                if not unusual and in_range:
                    seconds = sum(max(.4, sum(c*x for c, x in zip(profile['coefficients'], duration_features(text))))
                                  for text in texts)
                    margin = profile['reserve_margin_seconds']*len(texts)
                    return dict(seconds=seconds, reserve_seconds=seconds+margin,
                        method='tiny_linear_v1', uncertainty_seconds=margin, profile=profile['id'])
                reason = 'unsupported_text'
        seconds = sum(max(estimated_audio_seconds(text, language),
            .36*sum(word_units(token) for token in text.split()) if language == 'en' else 0) for text in texts)
        return dict(seconds=seconds, reserve_seconds=seconds, method='heuristic',
            uncertainty_seconds=None, fallback_reason=reason)


def source_duration(event):
    """返回观测音频区间。分句只接受明确划分的区间，不复用父段总长。"""
    if event.get('source_correction'):
        return None
    value = event.get('source_duration_seconds')
    if value is None and not event.get('clause_parent_id'):
        start, end = event.get('audio_start_pts_ns'), event.get('audio_end_pts_ns')
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            if not 0 < end-start <= 300_000_000_000:
                return None
            value = (end-start)/1e9
        elif event.get('source_audio_seconds') is not None:
            value = event['source_audio_seconds']
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 300 or not math.isfinite(value):
        return None
    return value
