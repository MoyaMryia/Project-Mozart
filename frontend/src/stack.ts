import type { StackStatus, StackRequirements } from './api';

const phases: Record<string, string> = {
  initializing: '正在启动', preparing_services: '正在准备翻译和语音',
  waiting_for_voice: '等待选择音色', preparing_captions: '正在准备识别',
  capturing: '正在采集', captions: '正在收尾识别和翻译', speech: '正在排空播报',
  preview: '输入已结束，可以试听', failed: '运行失败', stopped: '已停止', completed: '已完成',
};
export const stackPhaseLabel = (status: StackStatus) => phases[status.phase ?? ''] ?? '正在处理';
export const requiredOutputLabel = (requirements: StackRequirements) => requirements.playback ? '设备播放' : 'WAV 下载';
export function outputMatches(requirements: StackRequirements, language: string, playback: boolean): boolean {
  return language === requirements.language && playback === requirements.playback;
}
