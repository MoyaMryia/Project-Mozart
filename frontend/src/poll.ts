// 状态读取只保留一个在途请求。替换请求和卸载会撤销旧响应。
export function createPoll<T>(load: (signal: AbortSignal) => Promise<T>,
  accept: (value: T) => void, fail: (error: unknown) => void, timeoutMs = 5000) {
  let current: AbortController | null = null;
  let pending: Promise<void> | null = null;
  let disposed = false;
  return {
    run(replace = false): Promise<void> {
      if (disposed) return Promise.resolve();
      if (pending && !replace) return pending;
      current?.abort();
      const controller = new AbortController();
      current = controller;
      const timer = setTimeout(() => controller.abort(new DOMException('状态请求超时', 'TimeoutError')), timeoutMs);
      pending = (async () => {
        try {
          let value: T;
          try { value = await load(controller.signal); }
          catch (error) {
            if (!disposed && current === controller &&
                (!controller.signal.aborted || controller.signal.reason?.name === 'TimeoutError')) fail(error);
            return;
          }
          if (!disposed && current === controller && !controller.signal.aborted) accept(value);
        } finally {
          clearTimeout(timer);
          if (current === controller) { current = null; pending = null; }
        }
      })();
      return pending;
    },
    stop() { disposed = true; current?.abort(); current = null; pending = null; },
  };
}
