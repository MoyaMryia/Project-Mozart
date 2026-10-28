#!/usr/bin/env python3
"""Windows 配套程序：直接连接 Mozart USB 音频桥，录放与麦克风回传。

用途：演示机换成 Windows 时，不依赖 snd-aloop 与 WDM 虚拟声卡。
本程序自己做设备读写，用普通声卡播放与采集。

Windows 准备：
  1. pip install pyusb sounddevice
  2. 用 Zadig 给 `1d6b:0105` 的接口 0 安装 WinUSB 驱动（一次即可）。

Linux 用法相同，可直接用于测试。
接收方向接受 20 ms 与 128 样本帧；发送方向固定 20 ms。
音频走接口 0。

示例：
  python windows-bridge.py --record rx.wav --seconds 30
  python windows-bridge.py --play --seconds 30
  python windows-bridge.py --capture            # 把本机麦克风送到 Jetson
"""
import argparse
import math
import queue
import sys
import threading
import time
import wave
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import proto

try:
    import usb.core
    import usb.util
except ImportError:
    raise SystemExit('需要 pyusb：pip install pyusb')

VID, PID = 0x1d6b, 0x0105
CAPTURE_QUEUE_BLOCKS = 32


def open_device():
    device = usb.core.find(idVendor=VID, idProduct=PID)
    if device is None:
        raise SystemExit('未找到 USB 设备 1d6b:0105；检查线缆。'
                         'Windows 主机先用 Zadig 给接口 0 安装 WinUSB 驱动，'
                         '步骤见 usb-audio/README.md')
    try:
        configuration = device.get_active_configuration()
    except usb.core.USBError:
        device.set_configuration()
        configuration = device.get_active_configuration()
    interface = configuration[(0, 0)]
    out = usb.util.find_descriptor(
        interface, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
        == usb.util.ENDPOINT_OUT)
    inp = usb.util.find_descriptor(
        interface, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
        == usb.util.ENDPOINT_IN)
    if out is None or inp is None:
        raise SystemExit('设备缺少 bulk 端点')
    return device, out, inp


class AudioError(RuntimeError):
    """声卡或 WAV 输出失败。"""


class Session:
    """共享会话状态：停止通知、首个错误原因与退出码。"""

    def __init__(self):
        self.stopping = threading.Event()
        self.error = None
        self.exit_code = 0
        self._lock = threading.Lock()

    def fail(self, message):
        """记录首个错误并通知所有线程停止。"""
        with self._lock:
            if self.error is None:
                self.error = message
                self.exit_code = 1
        self.stopping.set()

    def run(self, target, *args):
        """线程结束时同步会话状态；内部异常保留原始 traceback。"""
        try:
            target(*args, self.stopping, on_error=self.fail)
        finally:
            error = sys.exc_info()[1]
            if error is not None:
                _report_failure(self.stopping, self.fail,
                                f'{target.__name__} 线程异常结束：{error}')


def _report_failure(stopping, on_error, message):
    """打印错误，回传会话状态，并请求停止。"""
    print(message, file=sys.stderr)
    if on_error is not None:
        on_error(message)
    stopping.set()


class Sink:
    """接收端：写 WAV 或用 sounddevice 播放。"""

    def __init__(self, path=None, play=False):
        self.writer = None
        self.stream = None
        self.frames = 0
        self._portaudio_error = None
        if path:
            try:
                self.writer = wave.open(str(path), 'wb')
            except OSError as error:
                raise AudioError(f'打开 WAV 失败：{error}') from error
            self.writer.setnchannels(proto.CHANNELS)
            self.writer.setsampwidth(proto.SAMPLE_BYTES)
            self.writer.setframerate(proto.RATE)
        if play:
            try:
                import sounddevice as sd
            except ImportError as error:
                self._close_writer()
                raise AudioError(f'需要 sounddevice：{error}') from error
            self._portaudio_error = sd.PortAudioError
            try:
                self.stream = sd.RawOutputStream(samplerate=proto.RATE,
                                                 channels=proto.CHANNELS,
                                                 dtype='int16', blocksize=0)
                self.stream.start()
            except sd.PortAudioError as error:
                self.close()
                raise AudioError(f'打开播放设备失败：{error}') from error

    def _close_writer(self):
        if self.writer:
            writer, self.writer = self.writer, None
            writer.close()

    def write(self, payload):
        self.frames += 1
        if self.writer:
            try:
                self.writer.writeframes(payload)
            except OSError as error:
                raise AudioError(f'写 WAV 失败：{error}') from error
        if self.stream:
            try:
                self.stream.write(payload)
            except self._portaudio_error as error:
                raise AudioError(f'写播放设备失败：{error}') from error

    def close(self):
        try:
            self._close_writer()
        finally:
            if self.stream:
                stream, self.stream = self.stream, None
                try:
                    try:
                        stream.stop()
                    finally:
                        stream.close()
                except self._portaudio_error as error:
                    raise AudioError(f'关闭播放设备失败：{error}') from error


def receive(inp, sink, stopping, show_stats=True, on_error=None):
    """读 USB 并写入 Sink；USB 或播放故障时记录错误并返回 1。"""
    buffer = b''
    expected = {}
    gaps = 0
    rejected = 0
    while not stopping.is_set():
        try:
            buffer += bytes(inp.read(65536, timeout=300))
        except usb.core.USBTimeoutError:
            continue
        except usb.core.USBError as error:
            _report_failure(stopping, on_error, f'读 USB 失败：{error}')
            return 1
        while True:
            try:
                frame = proto.decode(buffer)
            except proto.FrameError as error:
                print(f'帧格式错误，丢弃缓冲：{error}', file=sys.stderr)
                buffer = b''
                break
            if frame is None:
                break
            version, kind, stream, _flags, sequence, _ts, payload = frame
            buffer = buffer[proto.HEADER_BYTES + len(payload):]
            if version != proto.VERSION:
                print(f'未知协议版本 {version}', file=sys.stderr)
                continue
            if not proto.is_audio(kind, stream):
                continue
            if not proto.is_audio_frame(kind, stream, len(payload)):
                rejected += 1
                if rejected == 1:
                    print(f'帧长 {len(payload)} 不在 512/3840 之内，丢弃', file=sys.stderr)
                continue
            if stream in expected and sequence != expected[stream]:
                gaps += 1
            expected[stream] = sequence + 1
            try:
                sink.write(payload)
            except AudioError as error:
                _report_failure(stopping, on_error, str(error))
                return 1
            if show_stats and sink.frames % 50 == 0:
                print(f'已接收 {sink.frames} 帧，缺口 {gaps}，丢弃 {rejected}', flush=True)
    return 0


def capture(out, stopping, on_error=None):
    """把本机麦克风送给 Jetson，20 毫秒一帧；USB 写在本线程执行。"""
    try:
        import sounddevice as sd
    except ImportError as error:
        _report_failure(stopping, on_error, f'需要 sounddevice：{error}')
        return 1
    blocks = int(proto.RATE * proto.FRAME_MS / 1000)
    pending = queue.Queue(maxsize=CAPTURE_QUEUE_BLOCKS)
    sequence = 0
    dropped = 0
    code = 0
    buffered = bytearray()

    def callback(indata, _frames, _time, status):
        nonlocal dropped
        if status:
            print(f'采集状态：{status}', file=sys.stderr)
        buffered.extend(indata)
        while len(buffered) >= proto.FRAME_BYTES:
            payload = bytes(buffered[:proto.FRAME_BYTES])
            del buffered[:proto.FRAME_BYTES]
            try:
                pending.put_nowait(payload)
            except queue.Full:
                dropped += 1
                if dropped == 1 or dropped % 100 == 0:
                    print(f'发送队列已满，丢弃 {dropped} 帧', file=sys.stderr)

    def finished():
        nonlocal code
        if not stopping.is_set():
            code = 1
            _report_failure(stopping, on_error, '音频采集流意外结束')

    try:
        with sd.RawInputStream(samplerate=proto.RATE, channels=proto.CHANNELS,
                               dtype='int16', blocksize=blocks, callback=callback,
                               finished_callback=finished):
            while not stopping.is_set():
                try:
                    payload = pending.get(timeout=0.2)
                except queue.Empty:
                    continue
                if stopping.is_set():
                    break
                frame = proto.encode(proto.TYPE_PCM, sequence,
                                     int(time.monotonic() * 1e6), payload)
                try:
                    out.write(frame, timeout=2000)
                except usb.core.USBError as error:
                    _report_failure(stopping, on_error, f'写 USB 失败：{error}')
                    code = 1
                    break
                sequence += 1
    except sd.PortAudioError as error:
        _report_failure(stopping, on_error, f'音频采集失败：{error}')
        code = 1
    print(f'已发送 {sequence} 帧', flush=True)
    if dropped:
        print(f'发送队列已满，共丢弃 {dropped} 帧', file=sys.stderr, flush=True)
    return code


def main():
    parser = argparse.ArgumentParser(description='Mozart USB 音频桥 Windows 客户端')
    parser.add_argument('--record', help='把收到的音频写入 WAV')
    parser.add_argument('--play', action='store_true', help='用系统扬声器播放收到的音频')
    parser.add_argument('--capture', action='store_true', help='把系统麦克风发送给 Jetson')
    parser.add_argument('--seconds', type=float, default=None, help='运行时长，默认不限')
    arguments = parser.parse_args()
    if not (arguments.record or arguments.play or arguments.capture):
        parser.error('至少选择一个动作：--record / --play / --capture')
    if arguments.seconds is not None and (not math.isfinite(arguments.seconds) or arguments.seconds < 0):
        parser.error('--seconds 必须是有限的非负秒数')

    session = Session()
    sink = None
    threads = []
    try:
        _device, out, inp = open_device()
        if arguments.record or arguments.play:
            sink = Sink(arguments.record, arguments.play)
            thread = threading.Thread(target=session.run, args=(receive, inp, sink),
                                      daemon=True)
            thread.start()
            threads.append(thread)
        if arguments.capture:
            thread = threading.Thread(target=session.run, args=(capture, out), daemon=True)
            thread.start()
            threads.append(thread)
        if arguments.seconds is not None:
            session.stopping.wait(arguments.seconds)
        else:
            while not session.stopping.is_set() and any(t.is_alive() for t in threads):
                session.stopping.wait(0.2)
    except KeyboardInterrupt:
        pass
    except AudioError as error:
        _report_failure(session.stopping, session.fail, str(error))
    finally:
        session.stopping.set()
        for thread in threads:
            thread.join()
        if sink:
            try:
                sink.close()
            except (AudioError, OSError) as error:
                _report_failure(session.stopping, session.fail, f'关闭输出失败：{error}')
    if session.error is not None:
        print(f'会话失败：{session.error}', file=sys.stderr, flush=True)
    else:
        print('结束', flush=True)
    return session.exit_code


if __name__ == '__main__':
    sys.exit(main())
