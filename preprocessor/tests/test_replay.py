"""Exercise WAV replay timing and UDP metadata using the real CLI, without audio hardware."""
import concurrent.futures
import ctypes
import errno
import json
import math
import os
import selectors
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

BINARY = str(Path(sys.argv.pop(1)).resolve())
HEADER = struct.Struct('<IQIBBBB')


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mozart-replay-', dir=Path(BINARY).parent)
        self.addCleanup(self.directory.cleanup)
        self.wav = Path(self.directory.name) / 'input.wav'
        with wave.open(str(self.wav), 'wb') as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(struct.pack('<1920h', *([1000] * 1920)))

    def listener(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(('127.0.0.1', 0))
        listener.settimeout(3)
        self.addCleanup(listener.close)
        return listener

    def command(self, listener, *extra):
        return [BINARY, '-i', str(self.wav), '--no-denoise', '-a', '127.0.0.1',
                '-p', str(listener.getsockname()[1]), '--ctl-port', '0', *extra]

    def start(self, command, env=None):
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.addCleanup(cleanup)
        return process

    def test_paced_dual_send(self):
        first, second = self.listener(), self.listener()
        count = 25
        start = time.monotonic_ns()
        process = self.start(self.command(first, '--pace', '-n', str(count),
                                          '-b', f'127.0.0.1:{second.getsockname()[1]}'))
        packets = {first: [], second: []}
        with selectors.DefaultSelector() as selector:
            selector.register(first, selectors.EVENT_READ)
            selector.register(second, selectors.EVENT_READ)
            deadline = time.monotonic() + 5
            while min(map(len, packets.values())) < count and time.monotonic() < deadline:
                for key, _ in selector.select(.1):
                    packets[key.fileobj].append(key.fileobj.recv(4096))
                if process.poll() is not None and not selector.select(0):
                    break
        _, error = process.communicate(timeout=3)
        finish = time.monotonic_ns()
        self.assertEqual(process.returncode, 0, error.decode())
        self.assertEqual(len(packets[first]), count)
        self.assertEqual(packets[first], packets[second])
        self.assertGreaterEqual((finish-start)/1e9, .45)
        previous_pts = None
        for index, packet in enumerate(packets[first]):
            self.assertEqual(len(packet), 1300)
            magic, pts, frame, *_ = HEADER.unpack_from(packet)
            self.assertEqual(magic, 0x4D5A5254)
            self.assertEqual(frame, index+1)
            self.assertGreaterEqual(pts, start)
            self.assertLessEqual(pts, finish)
            if previous_pts is not None:
                self.assertEqual(pts-previous_pts, 20_000_000)
            previous_pts = pts

    def test_paced_replay_stops_on_signal(self):
        listener = self.listener()
        process = self.start(self.command(listener, '--pace'))
        self.assertEqual(len(listener.recv(4096)), 1300)
        process.send_signal(signal.SIGTERM)
        _, error = process.communicate(timeout=2)
        self.assertEqual(process.returncode, 0, error.decode())

    def test_pace_requires_file(self):
        result = subprocess.run([BINARY, '--pace', '--no-denoise'], capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'--pace requires -i', result.stderr)

    def test_status_file_rejects_input_inode_before_initialization(self):
        original = self.wav.read_bytes()
        original_stat = self.wav.stat()
        root = Path(self.directory.name)
        (root / 'alias-dir').mkdir()
        symlink = root / 'symlink.wav'
        symlink.symlink_to(self.wav)
        hardlink = root / 'hardlink.wav'
        hardlink.hardlink_to(self.wav)
        aliases = [str(self.wav), f'{root}/./alias-dir/../input.wav',
                   str(symlink), str(hardlink)]
        listener = self.listener()
        for alias in aliases:
            # 无效第二目标会在初始化阶段进入 cleanup。
            for extra in ([], ['-b', 'invalid']):
                with self.subTest(alias=alias, extra=extra):
                    result = subprocess.run(
                        self.command(listener, '-n', '1', '--ctl-port', '0',
                                     '--status-file', alias, *extra),
                        capture_output=True, timeout=2)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b'stage=config reason=status_file aliases input', result.stderr)
                    self.assertEqual(self.wav.read_bytes(), original)
                    self.assertEqual(Path(alias).read_bytes(), original)
                    for path in (self.wav, Path(alias)):
                        current = path.stat()
                        self.assertEqual((current.st_dev, current.st_ino),
                                         (original_stat.st_dev, original_stat.st_ino))
                    self.assertTrue(symlink.is_symlink())
                    self.assertEqual(sorted(p.name for p in root.iterdir()),
                                     ['alias-dir', 'hardlink.wav', 'input.wav', 'symlink.wav'])
        with selectors.DefaultSelector() as selector:
            selector.register(listener, selectors.EVENT_READ)
            self.assertFalse(selector.select(0), 'configuration failure sent audio')

    def test_input_stat_failure_exits_without_status_write(self):
        root = Path(self.directory.name)
        missing = root / 'missing.wav'
        existing_status = root / 'existing.json'
        existing_status.write_bytes(b'preserve existing status')
        original = self.wav.read_bytes()
        for input_path in (missing, self.wav / 'not-a-directory'):
            for status_path in (input_path, existing_status):
                with self.subTest(input=input_path, status=status_path):
                    result = subprocess.run(
                        [BINARY, '-i', str(input_path), '--status-file', str(status_path),
                         '--ctl-port', '0', '-n', '1'], capture_output=True, timeout=2)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b'stage=config input_stat', result.stderr)
                    code = errno.ENOENT if input_path == missing else errno.ENOTDIR
                    self.assertIn(os.strerror(code).encode(), result.stderr)
                    self.assertFalse(missing.exists())
                    self.assertEqual(existing_status.read_bytes(), b'preserve existing status')
                    self.assertEqual(self.wav.read_bytes(), original)
                    self.assertEqual(sorted(p.name for p in root.iterdir()),
                                     ['existing.json', 'input.wav'])

    def test_status_stat_error_exits_without_writes(self):
        original = self.wav.read_bytes()
        result = subprocess.run(
            [BINARY, '-i', str(self.wav), '--status-file', str(self.wav / 'status.json'),
             '--ctl-port', '0', '-n', '1'], capture_output=True, timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'stage=config status_stat', result.stderr)
        self.assertIn(os.strerror(errno.ENOTDIR).encode(), result.stderr)
        self.assertEqual(self.wav.read_bytes(), original)
        self.assertEqual(list(self.wav.parent.iterdir()), [self.wav])

    def test_absent_status_file_is_created_without_input_change(self):
        listener = self.listener()
        original = self.wav.read_bytes()
        status_path = self.wav.parent / 'new-status.json'
        result = subprocess.run(
            self.command(listener, '-n', '1', '--ctl-port', '0', '--status-file', str(status_path)),
            capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.wav.read_bytes(), original)
        status = json.loads(status_path.read_text())
        self.assertEqual(status['state'], 'stopped')
        self.assertEqual(status['frames'], 1)

    def test_fast_replay_preserves_relative_pts(self):
        listener = self.listener()
        process = self.start(self.command(listener, '-n', '2'))
        packets = [listener.recv(4096) for _ in range(2)]
        _, error = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0, error.decode())
        self.assertEqual([HEADER.unpack_from(p)[1] for p in packets], [0, 20_000_000])


class AlsaFailureTest(unittest.TestCase):
    setUp = ReplayTest.setUp
    listener = ReplayTest.listener
    command = ReplayTest.command
    start = ReplayTest.start
    @classmethod
    def setUpClass(cls):
        cls.shim_directory = tempfile.TemporaryDirectory(prefix='mozart-alsa-faults-', dir=Path(BINARY).parent)
        cls.addClassCleanup(cls.shim_directory.cleanup)
        cls.shim = Path(cls.shim_directory.name) / 'alsa_faults.so'
        subprocess.run([os.environ.get('CC', 'cc'), '-shared', '-fPIC',
                        str(Path(__file__).with_name('alsa_faults.c')), '-o', str(cls.shim), '-ldl'],
                       check=True, capture_output=True)

    def echo_server(self):
        listener = self.listener()
        listener.settimeout(.1)
        stopped = threading.Event()
        def echo():
            while not stopped.is_set():
                try:
                    packet, peer = listener.recvfrom(4096)
                except socket.timeout:
                    continue
                listener.sendto(packet[:HEADER.size] + bytes(960 * 4), peer)
        worker = threading.Thread(target=echo)
        worker.start()
        def cleanup():
            stopped.set()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.addCleanup(cleanup)
        return listener

    def fault_process(self, direction, mode, finite=True):
        listener = self.echo_server() if direction == 'write' else self.listener()
        if direction == 'write':
            command = self.command(listener, '--pace', '-o', 'null')
        else:
            command = [BINARY, '-d', 'null', '--no-denoise', '--ctl-port', '0',
                       '-p', str(listener.getsockname()[1])]
        if finite:
            command += ['-n', '1']
        env = dict(os.environ, LD_PRELOAD=str(self.shim))
        env[f'MOZART_FAULT_{direction.upper()}'] = mode
        return self.start(command, env)

    def wait_marker(self, process, marker):
        data = b''
        deadline = time.monotonic() + 2
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr, selectors.EVENT_READ)
            while marker not in data and time.monotonic() < deadline:
                if selector.select(.1):
                    block = os.read(process.stderr.fileno(), 4096)
                    if not block:
                        break
                    data += block
        self.assertIn(marker, data)
        return data

    def test_requested_output_open_failure_stops_before_input(self):
        listener = self.listener()
        process = self.start(self.command(listener, '-o', 'mozart_missing_output', '-n', '1'))
        _, error = process.communicate(timeout=2)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'status=failed stage=open', error)
        listener.settimeout(.1)
        with self.assertRaises(socket.timeout):
            listener.recv(4096)

    def test_playback_write_failure_stops_unbounded_input(self):
        process = self.fault_process('write', 'device_gone', finite=False)
        _, error = process.communicate(timeout=2)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'[fault] write', error)
        self.assertIn(b'status=failed stage=write', error)

    def test_playback_failure_interrupts_capture_wait(self):
        listener = self.echo_server()
        env = dict(os.environ, LD_PRELOAD=str(self.shim),
                   MOZART_FAULT_READ='after_second_again', MOZART_FAULT_WRITE='device_gone')
        process = self.start([BINARY, '-d', 'null', '-o', 'null', '--no-denoise',
                              '--ctl-port', '0', '--period-frames', '960',
                              '-p', str(listener.getsockname()[1])], env)
        _, error = process.communicate(timeout=2)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'status=failed stage=write', error)
        self.assertNotIn(b'status=failed stage=capture', error)
        self.assertNotIn(b'[fault] blocking_open', error)

    def test_capture_read_failure_returns_nonzero(self):
        process = self.fault_process('read', 'device_gone')
        _, error = process.communicate(timeout=2)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'status=failed stage=capture', error)

    def test_alsa_cancellation_without_stop_request_is_failure(self):
        for direction in ['read', 'write']:
            with self.subTest(direction=direction):
                process = self.fault_process(direction, 'cancelled')
                _, error = process.communicate(timeout=2)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(f'code={-errno.ECANCELED}'.encode(), error)

    def test_stalled_io_and_recovery_have_deadline(self):
        for direction in ['read', 'write']:
            for mode in ['again', 'suspend', 'xrun']:
                with self.subTest(direction=direction, mode=mode):
                    process = self.fault_process(direction, mode)
                    _, error = process.communicate(timeout=3)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertIn(f'code={-errno.ETIMEDOUT}'.encode(), error)
                    self.assertIn(f'[fault] {direction}'.encode(), error)

    def test_signal_cancels_stalled_io_and_recovery(self):
        for direction in ['read', 'write']:
            for mode in ['again', 'suspend', 'xrun']:
                with self.subTest(direction=direction, mode=mode):
                    process = self.fault_process(direction, mode, finite=False)
                    marker = b'[fault] resume' if mode == 'suspend' else f'[fault] {direction}'.encode()
                    before = self.wait_marker(process, marker)
                    start = time.monotonic()
                    process.send_signal(signal.SIGTERM)
                    _, error = process.communicate(timeout=.75)
                    self.assertEqual(process.returncode, 0, (before+error).decode())
                    self.assertLess(time.monotonic()-start, .75)
                    self.assertNotIn(b'status=failed', before+error)
                    self.assertNotIn(b'[fault] blocking_open', before+error)

    def test_recoverable_errors_and_partial_io_complete(self):
        for direction in ['read', 'write']:
            for mode in ['recover_suspend', 'recover_xrun', 'partial']:
                with self.subTest(direction=direction, mode=mode):
                    process = self.fault_process(direction, mode)
                    _, error = process.communicate(timeout=2)
                    self.assertEqual(process.returncode, 0, error.decode())
                    self.assertIn(f'[fault] {direction}'.encode(), error)
                    if mode == 'recover_suspend':
                        self.assertIn(b'[fault] resume', error)
                    self.assertNotIn(b'status=failed', error)
                    self.assertNotIn(b'[fault] blocking_open', error)

    def test_missing_backend_output_fails_finite_drain(self):
        listener = self.listener()
        process = self.start(self.command(listener, '--pace', '-o', 'null', '-n', '1'))
        listener.recv(4096)
        _, error = process.communicate(timeout=3)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'status=failed stage=drain', error)


class UsbLoopTest(unittest.TestCase):
    setUp = ReplayTest.setUp
    listener = ReplayTest.listener
    start = ReplayTest.start
    wire = struct.Struct('<HBBBBHIQI')

    @classmethod
    def setUpClass(cls):
        cls.shim_directory = tempfile.TemporaryDirectory(prefix='mozart-status-shim-', dir=Path(BINARY).parent)
        cls.addClassCleanup(cls.shim_directory.cleanup)
        source = Path(cls.shim_directory.name) / 'status_faults.c'
        cls.status_shim = source.with_suffix('.so')
        source.write_text(r'''
#define _GNU_SOURCE
#include <alsa/asoundlib.h>
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

static atomic_int fired;
static int fault(const char *operation)
{
    const char *mode = getenv("MOZART_STATUS_FAULT");
    return mode && !strcmp(mode, operation) && !atomic_exchange(&fired, 1);
}
static int status_path(const char *path)
{
    const char *target = getenv("MOZART_STATUS_PATH");
    return target && !strncmp(path, target, strlen(target)) && path[strlen(target)] == '.';
}
static int status_fd(int fd)
{
    char proc[64], path[4096];
    snprintf(proc, sizeof(proc), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(proc, path, sizeof(path)-1);
    if (n < 0) return 0;
    path[n] = 0;
    return status_path(path);
}
int mkstemp(char *path)
{
    int (*real)(char *) = dlsym(RTLD_NEXT, "mkstemp");
    if (status_path(path) && fault("mkstemp")) { errno = EACCES; return -1; }
    return real(path);
}
int fchmod(int fd, mode_t mode)
{
    int (*real)(int, mode_t) = dlsym(RTLD_NEXT, "fchmod");
    if (status_fd(fd) && fault("fchmod")) { errno = EPERM; return -1; }
    return real(fd, mode);
}
ssize_t write(int fd, const void *data, size_t size)
{
    ssize_t (*real)(int, const void *, size_t) = dlsym(RTLD_NEXT, "write");
    if (!status_fd(fd)) return real(fd, data, size);
    char marker[128];
    int n = snprintf(marker, sizeof(marker), "[status-shim] begin tid=%ld policy=%d\n",
                     syscall(SYS_gettid), sched_getscheduler(0));
    real(STDERR_FILENO, marker, n);
    const char *delay = getenv("MOZART_STATUS_DELAY_US");
    long us = delay ? strtol(delay, NULL, 10) : 0;
    struct timespec wait = { .tv_sec = us / 1000000, .tv_nsec = us % 1000000 * 1000 };
    while (nanosleep(&wait, &wait) < 0 && errno == EINTR) {}
    if (fault("write") || (syscall(SYS_gettid) == getpid() && fault("final_write"))) {
        errno = ENOSPC;
        return -1;
    }
    if (fault("zero")) return 0;
    if (fault("eintr")) { errno = EINTR; return -1; }
    if (fault("partial")) size /= 2;
    ssize_t result = real(fd, data, size);
    real(STDERR_FILENO, "[status-shim] end\n", 18);
    return result;
}
int close(int fd)
{
    int (*real)(int) = dlsym(RTLD_NEXT, "close");
    int is_status = status_fd(fd);
    int fail = is_status && fault("close");
    int result = real(fd);
    if (fail) { errno = EIO; return -1; }
    const char *cleanup = getenv("MOZART_STATUS_CLEANUP_FAULT");
    if (is_status && cleanup && atomic_load(&fired)) { errno = EBADF; return -1; }
    return result;
}
int rename(const char *from, const char *to)
{
    int (*real)(const char *, const char *) = dlsym(RTLD_NEXT, "rename");
    if (status_path(from) && fault("rename")) { errno = EROFS; return -1; }
    return real(from, to);
}
int unlink(const char *path)
{
    int (*real)(const char *) = dlsym(RTLD_NEXT, "unlink");
    int result = real(path);
    if (status_path(path) && getenv("MOZART_STATUS_CLEANUP_FAULT")) { errno = EACCES; return -1; }
    return result;
}
int pthread_mutexattr_init(pthread_mutexattr_t *attr)
{
    int (*real)(pthread_mutexattr_t *) = dlsym(RTLD_NEXT, "pthread_mutexattr_init");
    return fault("mutex_attr_init") ? ENOMEM : real(attr);
}
int pthread_mutexattr_setprotocol(pthread_mutexattr_t *attr, int protocol)
{
    int (*real)(pthread_mutexattr_t *, int) = dlsym(RTLD_NEXT, "pthread_mutexattr_setprotocol");
    return fault("mutex_protocol") ? ENOTSUP : real(attr, protocol);
}
int pthread_mutexattr_destroy(pthread_mutexattr_t *attr)
{
    int (*real)(pthread_mutexattr_t *) = dlsym(RTLD_NEXT, "pthread_mutexattr_destroy");
    int result = real(attr);
    return fault("mutex_attr_destroy") ? EINVAL : result;
}
static pthread_mutex_t *status_mutex;
static atomic_int status_lock_threads;
int pthread_mutex_init(pthread_mutex_t *mutex, const pthread_mutexattr_t *attr)
{
    int (*real)(pthread_mutex_t *, const pthread_mutexattr_t *) = dlsym(RTLD_NEXT, "pthread_mutex_init");
    if (syscall(SYS_gettid) == getpid() && !status_mutex) {
        status_mutex = mutex;
        int protocol = -1;
        int error = attr ? pthread_mutexattr_getprotocol(attr, &protocol) : EINVAL;
        fprintf(stderr, "[mutex-shim] status protocol=%s\n",
                !error && protocol == PTHREAD_PRIO_INHERIT ? "inherit" : "other");
        if (fault("mutex_init")) return EAGAIN;
    }
    return real(mutex, attr);
}
int pthread_mutex_lock(pthread_mutex_t *mutex)
{
    int (*real)(pthread_mutex_t *) = dlsym(RTLD_NEXT, "pthread_mutex_lock");
    if (mutex == status_mutex) {
        int thread = syscall(SYS_gettid) == getpid() ? 1 : 2;
        if (!(atomic_fetch_or(&status_lock_threads, thread) & thread))
            fprintf(stderr, "[mutex-shim] status lock thread=%s\n", thread == 1 ? "main" : "writer");
    }
    return real(mutex);
}
int snd_pcm_open(snd_pcm_t **pcm, const char *name, snd_pcm_stream_t stream, int mode)
{
    int (*real)(snd_pcm_t **, const char *, snd_pcm_stream_t, int) = dlsym(RTLD_NEXT, "snd_pcm_open");
    fprintf(stderr, "[mutex-shim] audio open\n");
    return real(pcm, name, stream, mode);
}
int pthread_attr_init(pthread_attr_t *attr)
{
    int (*real)(pthread_attr_t *) = dlsym(RTLD_NEXT, "pthread_attr_init");
    return fault("attr_init") ? ENOMEM : real(attr);
}
int pthread_create(pthread_t *tid, const pthread_attr_t *attr, void *(*run)(void *), void *arg)
{
    int (*real)(pthread_t *, const pthread_attr_t *, void *(*)(void *), void *) = dlsym(RTLD_NEXT, "pthread_create");
    if (attr && fault("create")) return EAGAIN;
    return real(tid, attr, run, arg);
}
int clock_nanosleep(clockid_t clock, int flags, const struct timespec *wake, struct timespec *remain)
{
    int (*real)(clockid_t, int, const struct timespec *, struct timespec *) = dlsym(RTLD_NEXT, "clock_nanosleep");
    if (syscall(SYS_gettid) != getpid() && fault("clock")) return EINVAL;
    return real(clock, flags, wake, remain);
}
int pthread_join(pthread_t tid, void **value)
{
    int (*real)(pthread_t, void **) = dlsym(RTLD_NEXT, "pthread_join");
    static unsigned calls;
    int result = real(tid, value);
    ++calls;
    if ((calls == 1 && fault("join")) || (calls == 2 && fault("reply_join"))) return EINVAL;
    return result;
}
''')
        subprocess.run([os.environ.get('CC', 'cc'), '-shared', '-fPIC', '-Wall', '-Wextra',
                        str(source), '-o', str(cls.status_shim), '-ldl', '-pthread'],
                       check=True, capture_output=True)

    def status_environment(self, mode='', delay=0, **extra):
        return dict(os.environ, LD_PRELOAD=str(self.status_shim),
                    MOZART_STATUS_PATH=str(Path(self.directory.name) / 'status.json'),
                    MOZART_STATUS_FAULT=mode, MOZART_STATUS_DELAY_US=str(delay), **extra)

    def launch(self, *extra, env=None, wait_file=True):
        root = Path(self.directory.name)
        for name in ('ep3', 'ep4'):
            os.mkfifo(root / name)
        self.input_fd = os.open(root / 'ep3', os.O_RDWR | os.O_NONBLOCK)
        self.output_fd = os.open(root / 'ep4', os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, self.input_fd)
        self.addCleanup(os.close, self.output_fd)
        reservation = self.listener()
        self.ctl_port = reservation.getsockname()[1]
        reservation.close()
        self.control = self.listener()
        self.control.settimeout(.15)
        self.backend = AlsaFailureTest.echo_server(self)
        self.status_path = root / 'status.json'
        self.process = self.start([BINARY, '--usb-endpoint', str(root), '--no-denoise',
                                   '--ctl-port', str(self.ctl_port),
                                   '--status-file', str(self.status_path),
                                   '-p', str(self.backend.getsockname()[1]), *extra], env=env)
        self.received = bytearray()
        if wait_file:
            self.wait_status(lambda s: s['state'] == 'warming' and self.status_path.exists())

    def query(self, command='status'):
        self.control.sendto(command.encode(), ('127.0.0.1', self.ctl_port))
        return self.control.recv(4096).rstrip(b'\0\n').decode()

    def wait_status(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                status = json.loads(self.query())
            except socket.timeout:
                continue
            if predicate(status):
                return status
            time.sleep(.01)
        self.fail('status did not reach expected state')

    def packet(self, kind, stream, payload, seq=0, timestamp=0):
        return self.wire.pack(19802, 1, kind, stream, 0, 0, seq, timestamp, len(payload)) + payload

    def receive(self, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.received) >= self.wire.size:
                header = self.wire.unpack_from(self.received)
                size = self.wire.size + header[-1]
                if len(self.received) >= size:
                    body = bytes(self.received[self.wire.size:size])
                    del self.received[:size]
                    self.assertEqual(header[:2], (19802, 1))
                    return header, body
            with selectors.DefaultSelector() as selector:
                selector.register(self.output_fd, selectors.EVENT_READ)
                if selector.select(.02):
                    self.received.extend(os.read(self.output_fd, 65536))
            if self.process.poll() is not None:
                _, error = self.process.communicate()
                self.fail(error.decode())
        self.fail('USB packet timeout')

    def negotiate(self, block):
        hello = struct.pack('<HB I H', 1, 1, 8 if block == 64 else 0, block)
        os.write(self.input_fd, self.packet(1, 1, hello))
        header, body = self.receive()
        self.assertEqual(header[2:4], (1, 1))
        self.assertEqual(struct.unpack('<HB I H', body), (2, 1, 8, block))

    def audio(self, block, count, amplitude=9000, start_seq=0):
        samples = [int(amplitude * math.sin(i * .071)) for i in range(block * count)]
        packets = []
        for i in range(count):
            payload = b''.join(struct.pack('<hh', v, -v) for v in samples[i*block:(i+1)*block])
            packets.append(self.packet(0, 3 if block == 64 else 2, payload,
                                       start_seq+i, 123000+start_seq+i))
        return samples, packets

    def send_packets(self, packets):
        for packet in packets:
            deadline = time.monotonic() + 2
            while True:
                try:
                    self.assertEqual(os.write(self.input_fd, packet), len(packet))
                    break
                except BlockingIOError:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.001)

    def expected_pcm(self, samples):
        # 独立计算高通递推；WOLA 旁路除首样本外重建原始样本。
        f32 = lambda v: struct.unpack('<f', struct.pack('<f', v))[0]
        b0, b1, b2, a1, a2 = map(f32, (.99262254, -1.98524509, .99262254,
                                                       -1.98519066, .98529951))
        x1 = x2 = y1 = y2 = 0.0
        result = []
        for i, sample in enumerate(samples):
            x = sample / 32768.0
            y = f32(f32(f32(f32(f32(b0*x) + f32(b1*x1)) + f32(b2*x2))
                        - f32(a1*y1)) - f32(a2*y2))
            x2, x1, y2, y1 = x1, x, y1, y
            result.append(int(max(-1, min(1, y)) * 32767) if i else 0)
        return result

    def fresh_ns_pcm(self, samples, block):
        class Config(ctypes.Structure):
            _fields_ = [(name, ctypes.c_bool) for name in ('denoise', 'gate', 'agc', 'limiter')]
        lib = ctypes.CDLL(str(Path(BINARY).parent / 'libmozart_pre_reference.so'))
        pointer = ctypes.POINTER(ctypes.c_float)
        lib.mozart_dsp_new.argtypes = [ctypes.POINTER(Config)]
        lib.mozart_dsp_new.restype = ctypes.c_void_p
        lib.mozart_dsp_enhance.argtypes = [ctypes.c_void_p, pointer, ctypes.c_size_t, pointer, ctypes.c_size_t]
        lib.mozart_dsp_enhance.restype = ctypes.c_size_t
        lib.mozart_dsp_free.argtypes = [ctypes.c_void_p]
        dsp = lib.mozart_dsp_new(ctypes.byref(Config(True, False, False, False)))
        self.assertTrue(dsp)
        result = []
        output = (ctypes.c_float * 1024)()
        try:
            # 每个会话使用全新 DSP，参考值不复用被测进程的 NS 状态。
            for start in range(0, len(samples) + 960, block):
                values = samples[start:start+block]
                chunk = (ctypes.c_float * block)(*(v / 32768 for v in values))
                count = lib.mozart_dsp_enhance(dsp, chunk, block, output, len(output))
                self.assertLessEqual(count, len(output))
                result.extend(int(ctypes.c_float(max(-1, min(1, output[i])) * 32767).value)
                              for i in range(count))
                if len(result) >= len(samples):
                    break
            self.assertGreaterEqual(len(result), len(samples))
            return result[:len(samples)]
        finally:
            lib.mozart_dsp_free(dsp)

    def collect_stop(self, block, samples, count, start_seq=0, expected=None):
        pcm = []
        for i in range(count):
            header, body = self.receive()
            self.assertEqual(header[2:4], (0, 3 if block == 64 else 2))
            self.assertEqual(header[6], start_seq+i)
            if block == 64:
                self.assertEqual(header[7], 123000+start_seq+i)
            self.assertEqual(len(body), block * 4)
            values = struct.unpack(f'<{block*2}h', body)
            self.assertEqual(values[::2], values[1::2])
            pcm.extend(values[::2])
        header, body = self.receive()
        self.assertEqual(header[2:4], (1, 1))
        self.assertEqual(struct.unpack('<HII', body), (6, count, count))
        if expected is None:
            expected = self.expected_pcm(samples)
        self.assertEqual(len(pcm), len(expected))
        self.assertLessEqual(max(abs(a-b) for a, b in zip(pcm, expected)), 2)
        status = self.wait_status(lambda s: s['usb']['pending'] == 0)
        self.assertEqual(status['usb']['rx'], count)
        self.assertEqual(status['usb']['tx'], count)
        return status

    def terminate(self):
        begin = time.monotonic()
        self.process.send_signal(signal.SIGTERM)
        _, error = self.process.communicate(timeout=.75)
        self.assertEqual(self.process.returncode, 0, error.decode())
        self.assertLess(time.monotonic()-begin, .75)
        self.assertEqual(json.loads(self.status_path.read_text())['state'], 'stopped')

    def test_usb64_restart_stop_and_backend_drain(self):
        self.launch()
        self.assertTrue(self.query('mode ai').startswith('err'))
        self.assertEqual(self.query('mode direct'), 'ok mode direct')
        for command in ('stage denoise', 'stage denoise maybe', 'stage denoise on extra',
                        'stage bogus on', 'mode direct extra', 'status extra', 'mode',
                        'stage denoise on\nstatus', 'stage gate on\0junk',
                        'stage gate on' + ' ' * 300):
            self.assertTrue(self.query(command).startswith('err'), command)
        self.assertEqual(self.query('stage denoise on'), 'ok stage denoise on')
        self.wait_status(lambda s: s['stages']['denoise'] == 1)
        self.assertEqual(self.query('stage denoise off'), 'ok stage denoise off')
        self.wait_status(lambda s: s['stages']['denoise'] == 0)
        self.negotiate(64)
        for session, count in enumerate((33, 33, 1), 1):
            samples, packets = self.audio(64, count, 5000 * session)
            start = self.packet(1, 1, struct.pack('<HH', 3, 64))
            # START 与首音频合并写入，覆盖 read 内部先换代再返回音频的路径。
            self.send_packets([start + packets[0], *packets[1:],
                               self.packet(1, 1, struct.pack('<H', 4))])
            header, body = self.receive()
            self.assertEqual(struct.unpack('<HH', body), (5, 64))
            status = self.collect_stop(64, samples, count)
            self.assertEqual(status['mode'], 'direct')
            self.assertEqual(status['usb']['block'], 64)
            self.assertTrue(status['usb']['negotiated'])
            self.assertEqual(status['usb']['session'], session)
            previous_processing = status['processing_us']
            time.sleep(.12)
            self.assertEqual(json.loads(self.query())['processing_us'], previous_processing)
            self.assertEqual(self.received, b'')
        status = self.wait_status(lambda s: s['skipped_replies'] >= 4)
        self.assertEqual(status['frames'], 4)
        self.assertEqual(status['direct_periods'], 67)
        with selectors.DefaultSelector() as selector:
            selector.register(self.output_fd, selectors.EVENT_READ)
            self.assertFalse(selector.select(.12), 'STOP_ACK must be sent once')
        self.terminate()

    def test_explicit128_restart_and_block_switch_reset_ns(self):
        self.launch()
        self.assertEqual(self.query('stage denoise on'), 'ok stage denoise on')
        self.wait_status(lambda s: s['stages']['denoise'] == 1)
        # 同一进程先运行两次 128，再执行 128 → 64 → 128。
        for session, block in enumerate((128, 128, 64, 128), 1):
            if session == 2:
                self.negotiate(64)
            samples, packets = self.audio(block, 19, 2000 * session)
            expected = self.fresh_ns_pcm(samples, block)
            start = self.packet(1, 1, struct.pack('<HH', 3, block))
            self.send_packets([start + packets[0], *packets[1:],
                               self.packet(1, 1, struct.pack('<H', 4))])
            header, body = self.receive()
            self.assertEqual(header[2:4], (1, 1))
            self.assertEqual(struct.unpack('<HH', body), (5, block))
            status = self.collect_stop(block, samples, 19, expected=expected)
            self.assertEqual(status['usb']['session'], session)
            self.assertEqual(status['usb']['block'], block)
            self.assertEqual(status['usb']['negotiated'], block == 64)
        self.terminate()

    def test_raw128_after_stop_requires_pre_restart(self):
        self.launch()
        samples, packets = self.audio(128, 1)
        self.send_packets([*packets, self.packet(1, 1, struct.pack('<H', 4))])
        self.collect_stop(128, samples, 1)
        self.send_packets(packets)
        _, error = self.process.communicate(timeout=2)
        self.assertNotEqual(self.process.returncode, 0, error.decode())
        status = json.loads(self.status_path.read_text())
        self.assertEqual(status['state'], 'failed')
        self.assertIn('after STOP', status['reason'])
        self.assertEqual(status['usb']['rx'], 1)
        self.assertEqual(status['usb']['tx'], 1)

    def test_legacy128_preserves_half_block_tail(self):
        self.launch('--period-frames', '17')
        samples, packets = self.audio(128, 19)
        self.send_packets([*packets, self.packet(1, 1, struct.pack('<H', 4))])
        status = self.collect_stop(128, samples, 19)
        self.assertEqual(status['usb']['block'], 128)
        self.assertFalse(status['usb']['negotiated'])
        self.wait_status(lambda s: s['skipped_replies'] >= 2)
        self.terminate()

    def test_legacy_restart_discards_previous_session_tail(self):
        self.launch()
        _, packets = self.audio(128, 11, 2000)
        self.send_packets(packets)
        for i in range(10):
            header, body = self.receive()
            self.assertEqual(header[6], i)
            self.assertEqual(len(body), 512)
        before = self.wait_status(lambda s: s['usb']['rx'] == 11)
        self.assertEqual(before['usb']['pending'], 1)
        samples, packets = self.audio(128, 19, 11000)
        self.send_packets([*packets, self.packet(1, 1, struct.pack('<H', 4))])
        status = self.collect_stop(128, samples, 19)
        self.assertEqual(status['usb']['session'], 1)
        self.assertEqual(status['frames'], 3)
        self.terminate()

    def test_stop_tail_completes_mzrt_frame(self):
        self.launch()
        self.negotiate(64)
        samples, packets = self.audio(64, 15)
        self.send_packets([self.packet(1, 1, struct.pack('<HH', 3, 64)), *packets,
                           self.packet(1, 1, struct.pack('<H', 4))])
        _, body = self.receive()
        self.assertEqual(struct.unpack('<HH', body), (5, 64))
        self.collect_stop(64, samples, 15)
        status = self.wait_status(lambda s: s['skipped_replies'] == 1)
        self.assertEqual(status['frames'], 1)
        self.terminate()

    def test_empty_stop_does_not_enhance_or_repeat_ack(self):
        self.launch()
        self.send_packets([self.packet(1, 1, struct.pack('<H', 4))])
        _, body = self.receive()
        self.assertEqual(struct.unpack('<HII', body), (6, 0, 0))
        time.sleep(.12)
        status = json.loads(self.query())
        self.assertEqual(status['processing_us']['max'], 0)
        self.assertEqual(status['usb']['pending'], 0)
        with selectors.DefaultSelector() as selector:
            selector.register(self.output_fd, selectors.EVENT_READ)
            self.assertFalse(selector.select(.12))
        self.terminate()

    def test_usb_idle_signal_and_status_updates(self):
        self.launch()
        previous = self.status_path.stat().st_mtime_ns
        deadline = time.monotonic() + .3
        while self.status_path.stat().st_mtime_ns == previous:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.001)
        # 在下一次空闲 read 已经开始后发命令，覆盖接近 200 ms 的旧路径。
        time.sleep(.01)
        begin = time.monotonic()
        self.assertEqual(self.query('stage gate on'), 'ok stage gate on')
        status = self.wait_status(lambda s: s['stages']['gate'] == 1)
        self.assertLess(time.monotonic()-begin, .15)
        self.assertEqual(status['state'], 'warming')
        self.assertEqual(status['processing_us']['max'], 0)
        self.assertEqual(status['frames'], 0)
        self.terminate()

    def test_alsa_route_callback_still_accepts_ai(self):
        reservation = self.listener()
        self.ctl_port = reservation.getsockname()[1]
        reservation.close()
        self.control = self.listener()
        self.control.settimeout(.15)
        backend = AlsaFailureTest.echo_server(self)
        self.process = self.start(ReplayTest.command(self, backend, '--pace', '-o', 'null',
                                                     '--ctl-port', str(self.ctl_port)))
        self.wait_status(lambda s: s['frames'] > 0)
        self.assertEqual(self.query('mode direct'), 'ok mode direct')
        self.wait_status(lambda s: s['mode'] == 'direct')
        self.assertEqual(self.query('mode ai'), 'ok mode ai')
        self.wait_status(lambda s: s['mode'] == 'ai')
        self.process.send_signal(signal.SIGTERM)
        _, error = self.process.communicate(timeout=.75)
        self.assertEqual(self.process.returncode, 0, error.decode())

    def test_explicit_usb_ai_fails_before_endpoint_open(self):
        result = subprocess.run([BINARY, '--usb-endpoint', str(self.wav.parent),
                                 '--mode', 'ai'], capture_output=True, timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'--mode ai', result.stderr)

    def test_control_open_failure_is_fatal_and_persisted(self):
        occupied = self.listener()
        status_path = Path(self.directory.name) / 'failed.json'
        result = subprocess.run([BINARY, '-i', str(self.wav),
                                 '--ctl-port', str(occupied.getsockname()[1]),
                                 '--status-file', str(status_path)], capture_output=True, timeout=2)
        self.assertNotEqual(result.returncode, 0)
        status = json.loads(status_path.read_text())
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(status['reason'], 'control_open')

    def test_udp_send_failure_has_queryable_reason(self):
        for target in ('primary', 'secondary'):
            with self.subTest(target=target):
                control_port = self.listener()
                self.ctl_port = control_port.getsockname()[1]
                control_port.close()
                self.control = self.listener()
                self.control.settimeout(.15)
                unreachable = self.listener()
                bad_port = unreachable.getsockname()[1]
                backend = unreachable if target == 'primary' else self.listener()
                self.status_path = Path(self.directory.name) / f'{target}-failure.json'
                command = ReplayTest.command(
                    self, backend, '--pace', '--ctl-port', str(self.ctl_port),
                    '--status-file', str(self.status_path))
                if target == 'secondary':
                    command += ['-b', f'127.0.0.1:{bad_port}']
                unreachable.close()
                self.process = self.start(command)
                status = self.wait_status(lambda s: s['send_errors'] > 0)
                expected = f'mzrt_send {target}: {os.strerror(errno.ECONNREFUSED)}'
                self.assertEqual(status['reason'], expected)
                self.terminate()
                final = json.loads(self.status_path.read_text())
                self.assertGreater(final['send_errors'], 0)
                self.assertEqual(final['reason'], expected)

    def test_status_mutex_uses_priority_inheritance_in_main_and_writer(self):
        backend = self.listener()
        path = self.wav.parent / 'status.json'
        original = self.wav.read_bytes()
        process = self.start(ReplayTest.command(
            self, backend, '--pace', '-n', '10', '--ctl-port', '0', '--status-file', str(path)),
            self.status_environment())
        _, error = process.communicate(timeout=2)
        self.assertEqual(process.returncode, 0, error.decode())
        self.assertIn(b'[mutex-shim] status protocol=inherit', error)
        self.assertIn(b'[mutex-shim] status lock thread=main', error)
        self.assertIn(b'[mutex-shim] status lock thread=writer', error)
        self.assertEqual(json.loads(path.read_text())['frames'], 10)
        self.assertEqual(self.wav.read_bytes(), original)

    def test_status_mutex_initialization_failure_stops_before_audio_or_writes(self):
        backend = self.listener()
        original = self.wav.read_bytes()
        cases = [('mutex_attr_init', errno.ENOMEM), ('mutex_protocol', errno.ENOTSUP),
                 ('mutex_init', errno.EAGAIN), ('mutex_attr_destroy', errno.EINVAL)]
        for mode, code in cases:
            with self.subTest(mode=mode):
                process = self.start(ReplayTest.command(
                    self, backend, '-o', 'null', '-n', '1', '--ctl-port', '0',
                    '--status-file', str(self.wav.parent / 'status.json')),
                    self.status_environment(mode))
                _, error = process.communicate(timeout=2)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(f'stage=status_mutex_init reason={os.strerror(code)}'.encode(), error)
                self.assertNotIn(b'[mutex-shim] audio open', error)
                self.assertNotIn(b'[mutex-shim] status lock', error)
                self.assertEqual(self.wav.read_bytes(), original)
                self.assertEqual(list(self.wav.parent.iterdir()), [self.wav])
        with selectors.DefaultSelector() as selector:
            selector.register(backend, selectors.EVENT_READ)
            self.assertFalse(selector.select(0), 'mutex initialization failure sent audio')

    def test_slow_status_write_does_not_block_usb_audio_or_control(self):
        self.launch(env=self.status_environment(delay=500_000), wait_file=False)
        before = AlsaFailureTest.wait_marker(self, self.process, b'[status-shim] begin')
        self.assertNotIn(f'tid={self.process.pid} '.encode(), before)
        self.assertIn(b'policy=0', before)
        begin = time.monotonic()
        self.negotiate(64)
        samples, packets = self.audio(64, 40)
        self.send_packets([self.packet(1, 1, struct.pack('<HH', 3, 64)), *packets,
                           self.packet(1, 1, struct.pack('<H', 4))])
        _, body = self.receive()
        self.assertEqual(struct.unpack('<HH', body), (5, 64))
        status = self.collect_stop(64, samples, 40)
        self.assertGreater(status['frames'], 0)
        self.assertEqual(self.query('stage gate on'), 'ok stage gate on')
        self.assertLess(time.monotonic() - begin, .35)
        self.assertFalse(self.status_path.exists())
        self.process.send_signal(signal.SIGTERM)
        _, error = self.process.communicate(timeout=2)
        self.assertEqual(self.process.returncode, 0, (before + error).decode())
        self.assertIn(f'tid={self.process.pid} '.encode(), error)
        final = json.loads(self.status_path.read_text())
        self.assertEqual(final['state'], 'stopped')
        self.assertEqual(final['usb']['rx'], 40)
        self.assertEqual(final['usb']['tx'], 40)
        self.assertEqual(self.status_path.stat().st_mode & 0o777, 0o644)

    def test_status_file_errors_fail_with_queryable_original_reason(self):
        cases = [('mkstemp', errno.EACCES, 'status_file'),
                 ('fchmod', errno.EPERM, 'status_file'),
                 ('write', errno.ENOSPC, 'status_file'),
                 ('zero', errno.EIO, 'status_file'),
                 ('close', errno.EIO, 'status_file'),
                 ('rename', errno.EROFS, 'status_file'),
                 ('clock', errno.EINVAL, 'status_clock'),
                 ('attr_init', errno.ENOMEM, 'status_thread_attr'),
                 ('create', errno.EAGAIN, 'status_thread')]
        def check_case(mode, code, stage, earlier_usb_error=False):
            # 独立进程并行注入错误，保留 CTest 原有的 20 s 超时。
            fixture = UsbLoopTest()
            fixture.setUp()
            try:
                fixture.launch(env=fixture.status_environment(mode, delay=250_000), wait_file=False)
                expected = f'{stage}: {os.strerror(code)}'
                before = b''
                if earlier_usb_error:
                    before = AlsaFailureTest.wait_marker(fixture, fixture.process, b'[status-shim] begin')
                    fixture.send_packets([fixture.packet(0, 3, bytes(256))])
                    expected = 'audio stream does not match negotiated block'
                before += AlsaFailureTest.wait_marker(fixture, fixture.process, b'status=failed stage=')
                failure_seen = time.monotonic()
                status = fixture.wait_status(lambda s: s['state'] == 'failed')
                if not earlier_usb_error:
                    fixture.assertLess(time.monotonic() - failure_seen, .15)
                fixture.assertEqual(status['reason'], expected)
                fixture.assertEqual(status['status_errno'], code)
                _, error = fixture.process.communicate(timeout=2)
                fixture.assertNotEqual(fixture.process.returncode, 0)
                fixture.assertIn(f'errno={code} reason={stage}: {os.strerror(code)}'.encode(), before + error)
                final = json.loads(fixture.status_path.read_text())
                fixture.assertEqual(final['state'], 'failed')
                fixture.assertEqual(final['reason'], expected)
                fixture.assertEqual(final['status_errno'], code)
                fixture.assertEqual(fixture.status_path.stat().st_mode & 0o777, 0o644)
            finally:
                self.assertTrue(fixture.doCleanups(), 'status fault fixture cleanup failed')
        cases.append(('write', errno.ENOSPC, 'status_file', True))
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(cases)) as executor:
            jobs = [(case, executor.submit(check_case, *case)) for case in cases]
            for case, job in jobs:
                with self.subTest(case=case):
                    job.result()

    def test_status_cleanup_errors_preserve_write_errno(self):
        self.launch(env=self.status_environment('write', delay=150_000,
                                                MOZART_STATUS_CLEANUP_FAULT='1'), wait_file=False)
        before = AlsaFailureTest.wait_marker(self, self.process, b'status=failed stage=status_file')
        status = self.wait_status(lambda s: s['state'] == 'failed')
        self.assertEqual(status['status_errno'], errno.ENOSPC)
        self.assertEqual(status['reason'], f'status_file: {os.strerror(errno.ENOSPC)}')
        _, error = self.process.communicate(timeout=2)
        self.assertNotEqual(self.process.returncode, 0)
        self.assertIn(b'stage=status_close', before + error)
        self.assertIn(b'status cleanup unlink', before + error)
        self.assertIn(f'errno={errno.ENOSPC}'.encode(), before + error)

    def test_status_partial_write_and_eintr_retry(self):
        for mode in ('partial', 'eintr'):
            with self.subTest(mode=mode):
                backend = self.listener()
                path = Path(self.directory.name) / f'{mode}.json'
                process = self.start(ReplayTest.command(
                    self, backend, '--pace', '-n', '2', '--ctl-port', '0', '--status-file', str(path)),
                    dict(self.status_environment(mode), MOZART_STATUS_PATH=str(path)))
                _, error = process.communicate(timeout=2)
                self.assertEqual(process.returncode, 0, error.decode())
                status = json.loads(path.read_text())
                self.assertEqual(status['state'], 'stopped')
                self.assertEqual(status['frames'], 2)
                self.assertEqual(status['status_errno'], 0)

    def test_final_status_write_error_exits_failed(self):
        backend = self.listener()
        path = Path(self.directory.name) / 'status.json'
        process = self.start(ReplayTest.command(
            self, backend, '--pace', '-n', '2', '--ctl-port', '0', '--status-file', str(path)),
            self.status_environment('final_write'))
        _, error = process.communicate(timeout=2)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(f'errno={errno.ENOSPC} reason=status_file: {os.strerror(errno.ENOSPC)}'.encode(), error)

    def test_join_errors_exit_failed(self):
        for mode, stage in [('join', 'status_join'), ('reply_join', 'reply_join')]:
            with self.subTest(mode=mode):
                backend = AlsaFailureTest.echo_server(self)
                path = Path(self.directory.name) / f'{mode}.json'
                process = self.start(ReplayTest.command(
                    self, backend, '--pace', '-n', '1', '-o', 'null', '--ctl-port', '0',
                    '--status-file', str(path)),
                    dict(self.status_environment(mode), MOZART_STATUS_PATH=str(path)))
                _, error = process.communicate(timeout=2)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(f'status=failed stage={stage}'.encode(), error)

    def test_usb_error_is_queryable_in_final_status(self):
        self.launch()
        self.send_packets([self.packet(0, 3, bytes(256))])
        _, error = self.process.communicate(timeout=2)
        self.assertNotEqual(self.process.returncode, 0, error.decode())
        status = json.loads(self.status_path.read_text())
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(status['reason'], 'audio stream does not match negotiated block')


if __name__ == '__main__':
    unittest.main()
