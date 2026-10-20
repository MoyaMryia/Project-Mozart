"""根据逐段时长和积压选择播放比例。每个任务只选择一次比例。"""
import math

MAX_TEMPO = 1.15
DEFAULT_TEMPO_MAX = MAX_TEMPO


def validate_tempo_limits(maximum, target_delay, recovery_seconds):
    if (not math.isfinite(maximum) or not 1 <= maximum <= MAX_TEMPO
            or not math.isfinite(target_delay) or target_delay < 0
            or not math.isfinite(recovery_seconds) or recovery_seconds <= 0):
        raise ValueError('Invalid tempo controller limits')


class TempoController:
    def __init__(self, maximum=DEFAULT_TEMPO_MAX, target_delay=1.5, recovery_seconds=30.):
        validate_tempo_limits(maximum, target_delay, recovery_seconds)
        self.maximum = math.floor(maximum*1000+1e-9)/1000
        self.target_delay, self.recovery_seconds = target_delay, recovery_seconds
        self.previous = {}

    def select(self, duration, source_seconds, delay, backlog, key, now):
        source_ratio = duration/source_seconds if source_seconds and source_seconds > 0 else 1.
        pressure = max(0., delay-self.target_delay, backlog-duration-self.target_delay)
        recovery_ratio = 1.+pressure/self.recovery_seconds
        requested = max(1., source_ratio, recovery_ratio)
        tempo = min(self.maximum, requested)
        previous, at = self.previous.get(key, (1., now))
        # 上升立即响应积压。相邻任务逐步恢复原速；长时间空闲后清除历史。
        if now-at < 5:
            tempo = max(tempo, previous-.01)
        tempo = round(min(self.maximum, max(1., tempo)), 3)
        self.previous[key] = (tempo, now)
        if len(self.previous) > 32:
            del self.previous[next(iter(self.previous))]
        return dict(tempo=tempo, requested_ratio=requested, source_ratio=source_ratio,
            recovery_ratio=recovery_ratio, delay_seconds=delay, backlog_seconds=backlog,
            source_duration_seconds=source_seconds, predicted_duration_seconds=duration,
            limited=requested > self.maximum, maximum=self.maximum,
            target_delay_seconds=self.target_delay, recovery_seconds=self.recovery_seconds)
