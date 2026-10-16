"""使用持续 FFmpeg 过滤器播放完整任务，保留音高和过滤器状态。"""
import os
import math
from pathlib import Path
import subprocess
import threading
import time
import wave
from tempo_control import MAX_TEMPO

_library = None
_library_lock = threading.Lock()


def tempo_library():
    global _library
    path = Path(__file__).resolve().parents[1]/'build-gpu/preprocessor/libmozart-tempo.so'
    if not path.is_file():
        return None
    with _library_lock:
        if _library is None:
            import ctypes
            library = ctypes.CDLL(str(path))
            library.mozart_tempo_filter.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_double]
            library.mozart_tempo_filter.restype = ctypes.c_int
            _library = library
    return _library


class NativeFilter:
    def __init__(self, library, rate, tempo):
        incoming, writer = os.pipe()
        reader, outgoing = os.pipe()
        self.stdin = os.fdopen(writer, 'wb', buffering=0)
        self.stdout = os.fdopen(reader, 'rb', buffering=0)
        self.code = None
        def run():
            # CDLL 调用释放 Python 锁。每个任务使用独立的过滤器状态。
            self.code = library.mozart_tempo_filter(incoming, outgoing, rate, tempo)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired('native tempo filter', timeout)
        return self.code

    def terminate(self):
        if not self.stdin.closed:
            self.stdin.close()

    kill = terminate


class TempoPlayer:
    def __init__(self, rate, device, tempo, log, capture=None):
        if not math.isfinite(tempo) or not 1 <= tempo <= MAX_TEMPO:
            raise ValueError('Invalid playback tempo')
        self.rate, self.tempo = rate, tempo
        self.stopped = threading.Event()
        self.error = None
        self.output_samples = 0
        self.first_audio_at = None
        self.output = subprocess.Popen(['aplay', '-q', '-D', device, '-t', 'raw',
            '-f', 'S16_LE', '-r', str(rate), '-c', '1'], stdin=subprocess.PIPE, stderr=log, bufsize=0)
        try:
            command = ['ffmpeg', '-v', 'error', '-nostdin',
                '-threads', '1', '-filter_threads', '1', '-probesize', '32', '-analyzeduration', '0',
                '-f', 's16le', '-ar', str(rate), '-ac', '1', '-blocksize', '1024', '-i', 'pipe:0',
                '-af', f'atempo={tempo:.3f}', '-f', 's16le', '-flush_packets', '1', 'pipe:1']
            self.backend = 'ffmpeg'
            library = tempo_library()
            if library:
                self.filter = NativeFilter(library, rate, tempo)
                self.backend = 'libavfilter_inprocess'
            else:
                self.filter = subprocess.Popen(command,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, bufsize=0)
        except Exception:
            self.output.stdin.close()
            self.output.terminate()
            try:
                self.output.wait(timeout=.1)
            except subprocess.TimeoutExpired:
                self.output.kill()
                self.output.wait(timeout=3)
            raise
        self.stdin = self.filter.stdin
        self.reader = threading.Thread(target=self.forward,
            args=(device, capture), daemon=True)
        self.reader.start()

    def forward(self, device, capture):
        recording = None
        try:
            if capture:
                recording = wave.open(str(capture), 'wb')
                recording.setparams((1, 2, self.rate, 0, 'NONE', 'not compressed'))
            began = None
            while not self.stopped.is_set():
                pcm = os.read(self.filter.stdout.fileno(), 4096)
                if not pcm:
                    break
                remaining = memoryview(pcm)
                while remaining and not self.stopped.is_set():
                    count = os.write(self.output.stdin.fileno(), remaining)
                    if count <= 0:
                        raise RuntimeError('Playback pipe accepted no audio')
                    remaining = remaining[count:]
                if self.stopped.is_set():
                    break
                if began is None:
                    began = time.monotonic()
                    self.first_audio_at = time.time()
                self.output_samples += len(pcm)//2
                if recording:
                    recording.writeframesraw(pcm)
                if device == 'null':
                    # 按过滤后的采样数等待。不能只按估算变速时长伪造完成时间。
                    self.stopped.wait(max(0, began+self.output_samples/self.rate-time.monotonic()))
        except Exception as error:
            if not self.stopped.is_set():
                self.error = error
                self.terminate()
        finally:
            if recording:
                recording.close()
            self.filter.stdout.close()
            if not self.output.stdin.closed:
                self.output.stdin.close()

    def poll(self):
        if self.error:
            return 1
        code = self.filter.poll()
        if code is not None and code != 0:
            return code
        output = self.output.poll()
        if output is not None and output != 0:
            return output
        if code is None or self.reader.is_alive():
            return None
        return output

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic()+timeout
        def remaining():
            return None if deadline is None else max(0, deadline-time.monotonic())
        code = self.filter.wait(timeout=remaining())
        self.reader.join(timeout=remaining())
        if self.reader.is_alive():
            raise subprocess.TimeoutExpired('tempo playback', timeout)
        output = self.output.wait(timeout=remaining())
        return code or output or int(self.error is not None)

    def terminate(self):
        self.stopped.set()
        for child in (self.filter, self.output):
            if child.poll() is None:
                child.terminate()

    def kill(self):
        self.stopped.set()
        for child in (self.filter, self.output):
            if child.poll() is None:
                child.kill()
