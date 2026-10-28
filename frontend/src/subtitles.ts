import type { SubtitleEvent } from './api';

// 主字幕使用翻译实际采用的源文本。refined_zh 是复核候选，只在候选行显示。
export function captionText(caption: SubtitleEvent | null): string {
  if (!caption) return '';
  return caption.translation_source_text ?? caption.zh;
}

// 译文源与识别原文不同时，返回可查看的识别原文。
export function originalCaptionText(caption: SubtitleEvent | null): string | null {
  if (!caption || caption.translation_source_text === undefined) return null;
  return caption.translation_source_text === caption.zh ? null : caption.zh;
}

// 复核文本与主字幕不同时，返回复核候选。
export function refinedCandidate(caption: SubtitleEvent | null): string | null {
  if (!caption || !caption.refined_zh) return null;
  return caption.refined_zh === captionText(caption) ? null : caption.refined_zh;
}

// 选择原因：采用、未采用、问题列表和改动词条。
export function captionSourceNote(caption: SubtitleEvent | null): string {
  if (!caption) return '';
  const audit = caption.translation_source_audit;
  if (!audit) return caption.translation_source_reason ?? '';
  const decision = audit.selected === 'refined' ? '复核已采用' : '复核未采用';
  const issues = audit.issues.length ? `；问题：${audit.issues.join('、')}` : '';
  const terms = audit.changed_terms?.length ? `；改动词条：${audit.changed_terms.join('、')}` : '';
  return `${decision}（${audit.reason}）${issues}${terms}`;
}

export interface CaptionTiming {
  label: string;
  title: string;
}

const withUnit = (value: number) => `${value} ms`;

// 计时显示：优先显示 ASR 结束到译文完成，分段放在 title 中。
export function captionTiming(caption: SubtitleEvent | null): CaptionTiming | null {
  if (!caption) return null;
  const afterAsr = caption.translation_after_asr_ms;
  const translate = caption.translate_ms;
  const label = afterAsr != null
    ? `ASR 后 ${withUnit(afterAsr)}`
    : translate !== undefined ? `翻译 ${withUnit(translate)}` : '';
  if (!label) return null;
  const segments: string[] = [];
  if (caption.translation_queue_wait_ms === null) segments.push('排队等待：未知（磁盘恢复）');
  else if (caption.translation_queue_wait_ms !== undefined)
    segments.push(`排队：${withUnit(caption.translation_queue_wait_ms)}`);
  if (caption.translation_refinement_wait_ms !== undefined)
    segments.push(`复核等待：${withUnit(caption.translation_refinement_wait_ms)}`);
  if (translate !== undefined) segments.push(`翻译请求：${withUnit(translate)}`);
  if (afterAsr != null) segments.push(`ASR 后总计：${withUnit(afterAsr)}`);
  if ((caption.translation_attempts ?? 1) > 1)
    segments.push(`翻译尝试：${caption.translation_attempts} 次`);
  return { label, title: segments.join('\n') };
}

export function latestTranslation(events: SubtitleEvent[]): SubtitleEvent | null {
  for (let i = events.length-1; i >= 0; i--) {
    if (events[i].en) return events[i];
  }
  return null;
}

export function upsertSubtitle(events: SubtitleEvent[], caption: SubtitleEvent): void {
  const index = caption.utterance_id
    ? events.findIndex(item => item.utterance_id === caption.utterance_id) : -1;
  if (index >= 0) {
    if ((caption.revision ?? 0) <= (events[index].revision ?? 0)) return;
    events[index] = caption;
    return;
  }
  const session = caption.utterance_id?.split(':').slice(0, -1).join(':');
  const sameSession = (item: SubtitleEvent) => session &&
    item.utterance_id?.split(':').slice(0, -1).join(':') === session;
  if (events.length === 50 && events.some(item => sameSession(item) && item.seq > caption.seq)) return;
  const position = events.findIndex(item => sameSession(item) && item.seq > caption.seq);
  if (position >= 0) events.splice(position, 0, caption);
  else events.push(caption);
  if (events.length > 50) events.shift();
}
