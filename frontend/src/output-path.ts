// 输出路径切换：变声 / 译文播报 / 直出。
// 播放设备一次只允许一个进程打开，所以切换顺序是固定的：
// 先释放设备，再把预处理指向新的输出，最后改路由或启用播报会话。

export type OutputPath = 'voice' | 'speech' | 'direct';

export interface OutputActions {
  /** 设置预处理播放设备；'null' 表示不播出声。 */
  setPlayback(playback: string): Promise<void>;
  /** 设置播报设备；播报进程的播放设备只在启动时固定，靠这个接口跟随面板。 */
  setSpeechPlayback(device: string): Promise<void>;
  setRouting(patch: { mic_muted?: boolean; bypass?: boolean }): Promise<void>;
  /** 停止播报、取消在播片段并释放播放设备。 */
  stopSpeech(): Promise<void>;
  /** 播报停止后重新准备运行时。 */
  prepareSpeech(): Promise<void>;
  enableSpeech(voiceId: string): Promise<void>;
}

export interface SpeechSnapshot {
  sessionEnabled: boolean;
  runtimeReady: boolean;
  voiceId: string;
}

export class OutputPathError extends Error {
  override name = 'OutputPathError';
}

export const selectOutput = async (
  path: OutputPath,
  speech: SpeechSnapshot | null,
  actions: OutputActions,
  playbackDevice: string,
): Promise<void> => {
  if (path === 'speech') {
    if (!speech) throw new OutputPathError('播报服务未启动：用 OUTPUT=both 启动链路后再切换');
    if (!speech.voiceId) throw new OutputPathError('先在播报面板选择参考音色');
    await actions.setPlayback('null');
    // 播报进程启动时固定了播放设备；不重定向就会与预处理争用或播到旧设备。
    await actions.setSpeechPlayback(playbackDevice);
    if (!speech.runtimeReady) await actions.prepareSpeech();
    await actions.enableSpeech(speech.voiceId);
    return;
  }
  if (speech?.sessionEnabled) await actions.stopSpeech();
  await actions.setPlayback(playbackDevice);
  await actions.setRouting(path === 'direct'
    ? { bypass: true, mic_muted: false }
    : { bypass: false, mic_muted: false });
};
