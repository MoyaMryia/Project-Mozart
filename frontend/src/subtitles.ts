import type { SubtitleEvent } from './api';

export function upsertSubtitle(events: SubtitleEvent[], caption: SubtitleEvent): void {
  const index = caption.utterance_id
    ? events.findIndex(item => item.utterance_id === caption.utterance_id) : -1;
  if (index >= 0) {
    if ((caption.revision ?? 0) <= (events[index].revision ?? 0)) return;
    events[index] = caption;
    return;
  }
  events.push(caption);
  if (events.length > 50) events.shift();
}
