import type { SubtitleEvent } from './api';

export type SubtitleState = 'connecting' | 'online' | 'offline';

export function subscribeSubtitles(
  accept: (caption: SubtitleEvent) => void,
  state: (value: SubtitleState) => void,
  createSource = () => new EventSource('/api/subtitles'),
) {
  let source: EventSource | null = null;
  let retry: ReturnType<typeof setTimeout> | undefined;

  const disconnect = () => {
    if (source) {
      source.onopen = source.onmessage = source.onerror = null;
      source.close();
      source = null;
    }
  };
  const reconnect = () => {
    disconnect();
    state('offline');
    // HTTP 错误可能让浏览器永久关闭 SSE。主动重连，并由后端补发历史。
    retry = setTimeout(connect, 1000);
  };
  const connect = () => {
    state('connecting');
    source = createSource();
    source.onopen = () => state('online');
    source.onmessage = event => {
      let caption: SubtitleEvent;
      try { caption = JSON.parse(event.data) as SubtitleEvent; }
      catch { return; }
      accept(caption);
    };
    source.onerror = reconnect;
  };
  connect();
  return () => {
    clearTimeout(retry);
    disconnect();
  };
}
