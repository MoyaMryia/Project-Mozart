import type { SubtitleEvent } from './api';

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
