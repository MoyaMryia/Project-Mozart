#!/usr/bin/env python3
"""Reference voice registry, disk-backed speech jobs, and one owned ALSA output.

Runs on loopback behind Mozart's HTTP API. Inference is isolated in a warm child
process so cancellation/timeouts can release the engine without stopping captions.
"""
import argparse
import base64
from collections import OrderedDict, deque
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
import wave
from speech_chunks import estimated_audio_seconds, split_speech_text
from durable_jobs import DiskJobs, DiskPending

TERMINAL = {'completed', 'failed', 'cancelled', 'expired'}


class ApiError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def atomic_json(path, value):
    temporary = path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


class SpeechService:
    def __init__(self, root, worker_command, engine='pocket', playback_device='default',
                 queue_limit=4, timeout=90, history_limit=128, preload=False,
                 delivery_policy='realtime'):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root/'voices').mkdir(exist_ok=True)
        (self.root/'results').mkdir(exist_ok=True)
        self.command, self.engine, self.device = worker_command, engine, playback_device
        self.queue_limit, self.timeout, self.history_limit = queue_limit, timeout, history_limit
        self.lock = threading.Condition(threading.RLock())
        self.delivery_policy = delivery_policy
        self.storage_warning = None
        self.voices = {p.stem: json.loads(p.read_text()) for p in (self.root/'voices').glob('*.json')}
        self.jobs, self.pending, self.keys = OrderedDict(), deque(), OrderedDict()
        if self.delivery_policy == 'coverage':
            self.jobs = DiskJobs(self.root/'speech-jobs.sqlite3', history_limit,
                pinned=lambda: {getattr(self, 'active', None), getattr(self, 'active_play', None)}-{None})
            self.pending = DiskPending(self.jobs)
        self.play_pending = deque()
        self.playback_limit_seconds = 12
        self.live_audio_budget_seconds = 24
        self.deferred_limit = 4
        self.active_play_piece = None
        paths = [] if isinstance(self.jobs, DiskJobs) and len(self.jobs) else sorted((self.root/'results').glob('*.json'), key=lambda p: p.stat().st_mtime)
        for path in paths:
            job = json.loads(path.read_text())
            if job['status'] not in TERMINAL and self.delivery_policy != 'coverage':
                job.update(status='failed', error='Speech service restarted before completion')
                atomic_json(path, job)
            self.jobs[job['id']] = job
            if job.get('utterance_id') and self.delivery_policy != 'coverage':
                self.keys[job['utterance_id']] = job['id']
        if self.delivery_policy == 'coverage':
            for key in self.jobs.ids("status NOT IN ('completed','failed','cancelled','expired')"):
                job = self.jobs[key]
                if 'playback_started_at' in job:
                    job.update(status='failed', error='Playback was interrupted; automatic replay is disabled')
                    self.save_job(job)
                else:
                    self.reset_waiting(job)
        self.prune()
        self.session = {'enabled': False, 'voice_id': '', 'language': 'en', 'playback': False}
        self.child = self.player = None
        self.output_buffer = b''
        self.closed = False
        self.runtime = None
        self.preload = preload
        self.warmup_error = None
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()
        self.play_worker = threading.Thread(target=self.play_loop, daemon=True)
        self.play_worker.start()

    def status(self):
        with self.lock:
            coverage = self.delivery_policy == 'coverage'
            jobs = self.jobs.recent(self.history_limit) if coverage else [dict(j) for j in reversed(self.jobs.values())]
            return {'available': True, 'engine': self.engine, 'runtime': self.runtime,
                'delivery_policy': self.delivery_policy,
                'storage_warning': self.storage_warning,
                'unfinished_jobs': self.jobs.unfinished() if coverage else sum(j['status'] not in TERMINAL for j in self.jobs.values()),
                'pending_jobs': len(self.pending),
                'resident_jobs': len(self.jobs.cache) if coverage else len(self.jobs),
                'warmup_error':self.warmup_error,
                'languages': ['en'] if self.engine == 'pocket' else ['en', 'zh'],
                'reference_tts': True, 'streaming_playback': False,
                'chunked_playback': True, 'chunk_target_words': 12,
                'live_audio_budget_seconds': self.live_audio_budget_seconds,
                'audio_backlog_seconds': self.audio_backlog_seconds(),
                'buffered_audio_seconds': self.buffered_audio_seconds(),
                'deferred_jobs': 0 if coverage else sum(j.get('admission_pending', False) and j['status'] not in TERMINAL for j in self.jobs.values()),
                'session': dict(self.session), 'queue_limit': self.queue_limit,
                'playback_device': self.device, 'playback_queue_limit_seconds': self.playback_limit_seconds,
                'voices': list(self.voices.values()),
                'jobs': jobs}

    def save_job(self, job):
        if self.delivery_policy == 'coverage':
            self.jobs[job['id']] = job
        try:
            atomic_json(self.root/'results'/f"{job['id']}.json", job)
        except OSError as error:
            if self.delivery_policy != 'coverage':
                raise
            self.storage_warning = str(error)
            print(f'[speech] JSON snapshot failed; the SQLite task remains stored: {error}', file=sys.stderr, flush=True)

    def reset_waiting(self, job):
        for name in ('error', 'finished_at', 'started_at', 'queue_seconds', 'admitted_at',
                     'audio_ready_at', 'generation_finished_at', 'duration_seconds',
                     'synthesis_seconds', 'sample_rate', 'first_callback_seconds', 'expired_stage'):
            job.pop(name, None)
        job.update(status='queued', chunks=[], generated_chunks=0, played_chunks=0,
                   generation_done=False, played_duration_seconds=0, admission_pending=False)
        self.save_job(job)
        self.pending.append(job['id'])

    def add_voice(self, body):
        import numpy as np
        import soundfile as sf
        name = str(body.get('name', '')).strip()
        if not name or len(name) > 80:
            raise ApiError(400, 'Reference name must contain 1–80 characters')
        try:
            raw = base64.b64decode(body['audio_base64'], validate=True)
            if len(raw) > 5*1024**2:
                raise ValueError('Reference exceeds 5 MiB')
            with sf.SoundFile(io.BytesIO(raw)) as audio:
                if audio.format != 'WAV' or not 8000 <= audio.samplerate <= 48000:
                    raise ValueError('Upload a WAV at 8–48 kHz')
                duration = len(audio)/audio.samplerate
                if not 3 <= duration <= 15 or audio.channels > 2:
                    raise ValueError('Reference must be 3–15 seconds, mono or stereo')
                rate = audio.samplerate
                pcm = audio.read(dtype='float32', always_2d=True).mean(axis=1)
            if not np.isfinite(pcm).all() or np.max(np.abs(pcm)) > 1 or np.sqrt(np.mean(pcm**2)) < .0001:
                raise ValueError('Reference is silent, non-finite or clips')
        except (KeyError, ValueError, RuntimeError) as error:
            raise ApiError(400, str(error)) from error
        transcript = str(body.get('transcript', '')).strip()
        if len(transcript) > 1000:
            raise ApiError(400, 'Reference transcript exceeds 1000 characters')
        if self.engine == 'zipvoice' and not transcript:
            raise ApiError(400, 'ZipVoice requires the exact reference transcript')
        with self.lock:
            if len(self.voices) >= 32:
                raise ApiError(409, 'Voice registry is full (32 profiles)')
            voice_id = uuid.uuid4().hex
            path = self.root/'voices'/f'{voice_id}.wav'
            sf.write(path, pcm, rate, subtype='PCM_16')
            voice = {'id': voice_id, 'name': name, 'transcript': transcript,
                'reference_language': str(body.get('language', 'zh')),
                'revision': hashlib.sha256(path.read_bytes()).hexdigest(),
                'duration_seconds': duration, 'engine': self.engine, 'status': 'needs_preview',
                'identity_approved': False}
            atomic_json(path.with_suffix('.json'), voice)
            self.voices[voice_id] = voice
            return dict(voice)

    def delete_voice(self, voice_id):
        with self.lock:
            if voice_id not in self.voices:
                raise ApiError(404, 'Reference voice not found')
            busy = self.jobs.voice_busy(voice_id) if self.delivery_policy == 'coverage' else any(j['voice_id'] == voice_id and j['status'] not in TERMINAL for j in self.jobs.values())
            if busy:
                raise ApiError(409, 'Reference is in use by an unfinished job')
            if self.session['voice_id'] == voice_id:
                self.session.update(enabled=False, voice_id='')
            for suffix in ['.wav', '.json']:
                (self.root/'voices'/f'{voice_id}{suffix}').unlink(missing_ok=True)
            del self.voices[voice_id]
            return {'deleted': voice_id}

    def submit(self, body):
        text = str(body.get('text', '')).strip()
        language = body.get('language', 'en')
        degraded = body.get('speech_degraded', False)
        if not isinstance(degraded, bool):
            raise ApiError(400, 'speech_degraded must be a boolean')
        if language not in (['en'] if self.engine == 'pocket' else ['en', 'zh']):
            raise ApiError(422, 'Selected engine does not support this output language')
        if not text or len(text) > 500:
            raise ApiError(400, 'Speech text must contain 1–500 characters')
        if language == 'en' and not degraded and any('\u3400' <= c <= '\u9fff' for c in text):
            raise ApiError(422, 'English output contains Chinese text; translation is required')
        with self.lock:
            key = str(body.get('utterance_id', ''))
            if len(key) > 128:
                raise ApiError(400, 'Utterance ID exceeds 128 characters')
            existing = self.jobs.find_utterance(key) if key and self.delivery_policy == 'coverage' else self.keys.get(key)
            if existing and existing in self.jobs:
                return dict(self.jobs[existing])
            voice = self.voices.get(body.get('voice_id'))
            if voice is None:
                raise ApiError(404, 'Reference voice not found')
            if voice['engine'] != self.engine:
                raise ApiError(422, 'Reference profile belongs to a different engine')
            live = body.get('live', False)
            playback = body.get('playback', False)
            if not isinstance(live, bool) or not isinstance(playback, bool):
                raise ApiError(400, 'live and playback must be booleans')
            texts = split_speech_text(text, language) if live or playback else [text]
            estimate = sum(estimated_audio_seconds(piece, language) for piece in texts)
            unfinished = [] if self.delivery_policy == 'coverage' else [j for j in self.jobs.values() if j['status'] not in TERMINAL]
            admission_pending = False
            if self.delivery_policy == 'coverage':
                pass
            elif live and playback:
                deferred = sum(j.get('admission_pending', False) for j in unfinished)
                if len(unfinished) >= 64 or estimate > self.live_audio_budget_seconds:
                    raise ApiError(429, 'Estimated speech delay exceeds the live audio budget; captions continue')
                # Include deferred text when predicting FIFO playback, unlike
                # the actual audio-reservation budget. A bounded queue must not
                # knowingly accept work that cannot start before its deadline.
                first_piece = estimated_audio_seconds(texts[0], language)
                predicted_first_play = self.audio_backlog_seconds(include_deferred=True)+first_piece
                if predicted_first_play >= 30:
                    raise ApiError(429, 'Estimated first playback exceeds the live deadline; captions continue')
                if deferred or self.audio_backlog_seconds()+estimate > self.live_audio_budget_seconds:
                    if deferred >= self.deferred_limit:
                        raise ApiError(429, 'Deferred speech queue is full; captions continue')
                    admission_pending = True
            elif len(self.pending)+int(getattr(self, 'active', None) is not None) >= self.queue_limit:
                raise ApiError(429, 'Synthesis queue is full; captions continue')
            job_id = uuid.uuid4().hex
            job = {'id': job_id, 'text': text, 'language': language,
                'voice_id': voice['id'], 'voice_revision': voice['revision'],
                'reference_text': voice['transcript'], 'engine': self.engine,
                'utterance_id': key, 'source_text': str(body.get('source_text', ''))[:2000],
                'status': 'queued', 'created_at': time.time(),
                'live': live, 'playback': playback, 'speech_degraded': degraded,
                'admission_pending': admission_pending,
                'was_deferred': admission_pending, 'admission_wait_seconds': 0,
                'chunk_texts': texts, 'chunk_count': len(texts), 'chunks': [],
                'generated_chunks': 0, 'played_chunks': 0, 'generation_done': False,
                'estimated_duration_seconds': estimate, 'played_duration_seconds': 0,
                'max_duration_seconds': min(45, max(4, (len(text)*.35 if language == 'zh' else len(text.split())*.9)+2)),
                'result_url': f'/api/speech/jobs/{job_id}/result'}
            try:
                self.jobs[job_id] = job
                self.save_job(job)
                self.pending.append(job_id)
            except (OSError, sqlite3.Error) as error:
                raise ApiError(507, 'Speech task storage failed; inspect the service log') from error
            if key and self.delivery_policy != 'coverage':
                self.keys[key] = job_id
                while len(self.keys) > 256:
                    self.keys.popitem(last=False)
            self.prune()
            self.lock.notify_all()
            return dict(job)

    def audio_backlog_seconds(self, include_deferred=False):
        now = time.time()
        coverage = self.delivery_policy == 'coverage'
        total = self.pending.estimate() if coverage else 0
        jobs = (self.jobs[key] for key in self.jobs.ids("pending=0 AND status NOT IN ('completed','failed','cancelled','expired')")) if coverage else self.jobs.values()
        for job in jobs:
            if job['status'] in TERMINAL or not job['playback'] or (job.get('admission_pending') and not include_deferred):
                continue
            duration = job.get('duration_seconds', 0)
            if not job.get('generation_done'):
                duration = max(duration, job.get('estimated_duration_seconds', 0))
            remaining = duration-job.get('played_duration_seconds', 0)
            if self.active_play_piece and self.active_play_piece['job_id'] == job['id']:
                remaining -= min(self.active_play_piece['duration_seconds'],
                                 max(0, now-self.active_play_piece['playback_started_at']))
            total += max(0, remaining)
        return total

    def buffered_audio_seconds(self):
        total = sum(p['duration_seconds'] for p in self.play_pending
                    if self.jobs[p['job_id']]['status'] not in TERMINAL)
        if self.active_play_piece:
            total += max(0, self.active_play_piece['duration_seconds']-
                         (time.time()-self.active_play_piece['playback_started_at']))
        return total

    def prune(self):
        coverage = self.delivery_policy == 'coverage'
        candidates = self.jobs.history_to_remove(self.history_limit) if coverage else list(self.jobs)
        for job_id in candidates:
            if not coverage and len(self.jobs) <= self.history_limit:
                break
            if self.jobs[job_id]['status'] in TERMINAL and job_id not in {getattr(self, 'active', None), getattr(self, 'active_play', None)}:
                del self.jobs[job_id]
                (self.root/'results'/f'{job_id}.wav').unlink(missing_ok=True)
                (self.root/'results'/f'{job_id}.json').unlink(missing_ok=True)
                for path in (self.root/'results').glob(f'{job_id}-part-*.wav'):
                    path.unlink(missing_ok=True)

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise ApiError(404, 'Speech job not found')
            if job['status'] not in TERMINAL:
                job['status'] = 'cancelled'
                if self.delivery_policy == 'coverage':
                    self.pending.discard(job_id)
                else:
                    self.pending = deque(i for i in self.pending if i != job_id)
                self.play_pending = deque(p for p in self.play_pending if p['job_id'] != job_id)
                if getattr(self, 'active', None) == job_id:
                    self.kill(self.child)
                if getattr(self, 'active_play', None) == job_id:
                    self.kill(self.player)
            self.save_job(job)
            self.lock.notify_all()
            return dict(job)

    @staticmethod
    def kill(process):
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def read_child(self, timeout=None):
        end = time.monotonic()+min(self.timeout, timeout if timeout is not None else self.timeout)
        while time.monotonic() < end and not self.closed:
            while b'\n' in self.output_buffer:
                line, self.output_buffer = self.output_buffer.split(b'\n', 1)
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        return value
                except (ValueError, UnicodeDecodeError):
                    continue
            if self.child.poll() is not None:
                raise RuntimeError('Speech worker exited; inspect speech-worker.log')
            if select.select([self.child.stdout], [], [], .1)[0]:
                chunk = os.read(self.child.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError('Speech worker closed its output')
                self.output_buffer += chunk
                if len(self.output_buffer) > 1024**2:
                    raise RuntimeError('Speech worker response exceeds 1 MiB')
        raise RuntimeError('Speech worker timed out')

    def ensure_child(self):
        if self.child is None or self.child.poll() is not None:
            if self.child:
                self.child.stdin.close()
                self.child.stdout.close()
            self.child = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.worker_log, bufsize=0)
            self.output_buffer = b''
            ready = self.read_child()
            if not ready.get('ready'):
                raise RuntimeError('Speech worker did not become ready')
            self.runtime = ready
            self.warmup_error = None

    def run(self):
        with (self.root/'speech-worker.log').open('a') as self.worker_log:
            if self.preload and not self.closed:
                try:
                    self.ensure_child()
                except Exception as error:
                    self.kill(self.child)
                    self.warmup_error = str(error)
            while True:
                with self.lock:
                    self.lock.wait_for(lambda: self.pending or self.closed)
                    if self.closed:
                        return
                    job = self.jobs[self.pending.popleft()]
                    if job['status'] in TERMINAL:
                        continue
                    if self.expire_before_first_audio(job):
                        continue
                    # Keep a bounded FIFO of text awaiting capacity. It reserves
                    # no audio and never synthesizes until the existing budget
                    # allows it. The original 30-second deadline still applies.
                    while job.get('admission_pending') and not self.closed and job['status'] not in TERMINAL:
                        if self.expire_before_first_audio(job):
                            break
                        if self.audio_backlog_seconds()+job['estimated_duration_seconds'] <= self.live_audio_budget_seconds:
                            job['admission_pending'] = False
                            job['admitted_at'] = time.time()
                            job['admission_wait_seconds'] = job['admitted_at']-job['created_at']
                            break
                        self.lock.wait(timeout=.1)
                    if self.closed:
                        return
                    if job['status'] in TERMINAL:
                        continue
                    self.active = job['id']
                    job['status'] = 'processing'
                    job['started_at'] = time.time()
                    job['queue_seconds'] = job['started_at']-job['created_at']
                output = self.root/'results'/f"{job['id']}.wav"
                temporary = output.with_suffix('.tmp')
                try:
                    self.ensure_child()
                    for index, text in enumerate(job['chunk_texts']):
                        with self.lock:
                            # Backpressure before computing, rather than discarding
                            # a finished WAV. Include the active player's remainder.
                            while (job['playback'] and self.buffered_audio_seconds()+
                                   estimated_audio_seconds(text, job['language']) > self.playback_limit_seconds):
                                if job['status'] in TERMINAL or self.closed or self.expire_before_first_audio(job):
                                    break
                                self.lock.wait(timeout=.1)
                            if job['status'] in TERMINAL or self.closed or self.expire_before_first_audio(job):
                                break
                        path = self.root/'results'/f"{job['id']}-part-{index:03d}.wav"
                        partial = path.with_suffix('.tmp')
                        limit = min(12, max(4, estimated_audio_seconds(text, job['language'])*2.5+2))
                        # Download-only non-live jobs retain their existing contract.
                        if not job['live'] and not job['playback']:
                            limit = job['max_duration_seconds']
                        request = {**job, 'text': text, 'max_duration_seconds': limit,
                            'reference': str(self.root/'voices'/f"{job['voice_id']}.wav"),
                            'output': str(partial)}
                        try:
                            self.child.stdin.write(json.dumps(request).encode()+b'\n')
                            self.child.stdin.flush()
                            result = self.read_child(timeout=20 if job['live'] and self.delivery_policy != 'coverage' else None)
                            if 'error' in result:
                                raise RuntimeError(result['error'])
                            with self.lock:
                                # Estimates are imperfect. Hold the completed piece
                                # until its actual duration fits; never drop it just
                                # because playback is temporarily ahead of synthesis.
                                while (job['playback'] and self.buffered_audio_seconds()+
                                       result['duration_seconds'] > self.playback_limit_seconds):
                                    if job['status'] in TERMINAL or self.closed or self.expire_before_first_audio(job):
                                        break
                                    self.lock.wait(timeout=.1)
                                if job['status'] in TERMINAL or self.closed:
                                    break
                                if self.expire_before_first_audio(job):
                                    break
                                partial.replace(path)
                                chunk = {**result, 'job_id': job['id'], 'index': index,
                                    'text': text, 'filename': path.name, 'status': 'ready',
                                    'audio_ready_at': time.time()}
                                job['chunks'].append(chunk)
                                job['generated_chunks'] += 1
                                job['duration_seconds'] = sum(p['duration_seconds'] for p in job['chunks'])
                                job['synthesis_seconds'] = sum(p.get('synthesis_seconds', 0) for p in job['chunks'])
                                job['sample_rate'] = result['sample_rate']
                                job.setdefault('audio_ready_at', chunk['audio_ready_at'])
                                if index == 0:
                                    job['first_callback_seconds'] = result.get('first_callback_seconds')
                                if job['playback']:
                                    self.play_pending.append(chunk)
                                    if 'playback_started_at' not in job:
                                        job['status'] = 'ready'
                                    self.lock.notify_all()
                                self.save_job(job)
                        finally:
                            partial.unlink(missing_ok=True)
                    with self.lock:
                        if job['status'] in TERMINAL or self.closed:
                            continue
                    # Preserve one ordered downloadable WAV for the logical job.
                    with wave.open(str(temporary), 'wb') as combined:
                        settings = None
                        for chunk in job['chunks']:
                            with wave.open(str(self.root/'results'/chunk['filename']), 'rb') as source:
                                current = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                                if settings is None:
                                    settings = current
                                    combined.setnchannels(current[0])
                                    combined.setsampwidth(current[1])
                                    combined.setframerate(current[2])
                                if current != settings:
                                    raise RuntimeError('Speech chunk WAV formats differ')
                                combined.writeframes(source.readframes(source.getnframes()))
                    with self.lock:
                        if job['status'] in TERMINAL or self.closed:
                            continue
                        temporary.replace(output)
                        job['generation_done'] = True
                        job['generation_finished_at'] = time.time()
                        if not job['playback'] or job['played_chunks'] == job['chunk_count']:
                            self.complete(job)
                except Exception as error:
                    self.kill(self.child)
                    with self.lock:
                        if job['status'] not in TERMINAL:
                            job.update(status='failed', error=str(error))
                            if getattr(self, 'active_play', None) == job['id']:
                                self.kill(self.player)
                            self.play_pending = deque(p for p in self.play_pending if p['job_id'] != job['id'])
                finally:
                    temporary.unlink(missing_ok=True)
                    with self.lock:
                        if job['status'] in TERMINAL:
                            job['finished_at'] = time.time()
                        self.save_job(job)
                        self.active = None
                        self.prune()
                        self.lock.notify_all()

    def expire_before_first_audio(self, job):
        if self.delivery_policy == 'coverage':
            return False
        first_output = 'playback_started_at' if job['playback'] else 'audio_ready_at'
        if (job['live'] and first_output not in job and
                time.time()-job['created_at'] > 30):
            stage = ('admission' if job.get('admission_pending') else
                     'playback' if job.get('audio_ready_at') else 'synthesis')
            job.update(status='expired', finished_at=time.time(), expired_stage=stage,
                       error=f'Live speech did not reach first output within 30 seconds ({stage})')
            self.play_pending = deque(p for p in self.play_pending if p['job_id'] != job['id'])
            self.save_job(job)
            self.lock.notify_all()
            return True
        return False

    def complete(self, job):
        job['status'] = 'completed'
        voice = self.voices[job['voice_id']]
        voice['status'] = 'preview_available'
        atomic_json(self.root/'voices'/f"{voice['id']}.json", voice)

    def play_loop(self):
        with (self.root/'playback.log').open('a') as log:
            while True:
                with self.lock:
                    self.lock.wait_for(lambda: self.play_pending or self.closed)
                    if self.closed:
                        return
                    chunk = self.play_pending.popleft()
                    job = self.jobs[chunk['job_id']]
                    if job['status'] in TERMINAL or self.expire_before_first_audio(job):
                        continue
                    self.active_play = job['id']
                    self.active_play_piece = chunk
                    chunk['status'] = 'playing'
                    chunk['playback_started_at'] = time.time()
                    job['status'] = 'playing'
                    job.setdefault('playback_started_at', chunk['playback_started_at'])
                    self.save_job(job)
                try:
                    with self.lock:
                        if job['status'] in TERMINAL or self.closed:
                            continue
                        self.player = subprocess.Popen(['aplay', '-q', '-D', self.device,
                            str(self.root/'results'/chunk['filename'])], stderr=log)
                    code = self.player.wait(timeout=chunk['duration_seconds']+10)
                    if code:
                        raise RuntimeError(f'Playback failed (aplay exit {code})')
                    if self.device == 'null':
                        remaining = chunk['duration_seconds']-(time.time()-chunk['playback_started_at'])
                        if remaining > 0:
                            with self.lock:
                                self.lock.wait_for(lambda: job['status'] in TERMINAL or self.closed, timeout=remaining)
                    with self.lock:
                        if job['status'] not in TERMINAL and not self.closed:
                            chunk.update(status='completed', finished_at=time.time())
                            job['played_chunks'] += 1
                            job['played_duration_seconds'] += chunk['duration_seconds']
                            if job['generation_done'] and job['played_chunks'] == job['chunk_count']:
                                self.complete(job)
                except Exception as error:
                    self.kill(self.player)
                    with self.lock:
                        if job['status'] not in TERMINAL:
                            job.update(status='failed', error=str(error))
                            self.play_pending = deque(p for p in self.play_pending if p['job_id'] != job['id'])
                            if getattr(self, 'active', None) == job['id']:
                                self.kill(self.child)
                finally:
                    with self.lock:
                        if job['status'] in TERMINAL:
                            job['finished_at'] = time.time()
                        self.save_job(job)
                        self.active_play = None
                        self.active_play_piece = None
                        self.prune()
                        self.lock.notify_all()

    def close(self):
        restore = []
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.delivery_policy == 'coverage':
                keys = self.jobs.ids("pending=0 AND status NOT IN ('completed','failed','cancelled','expired')")
                jobs = [self.jobs[key] for key in keys]
            else:
                jobs = list(self.jobs.values())
            for job in jobs:
                if job['status'] not in TERMINAL:
                    if self.delivery_policy == 'coverage' and 'playback_started_at' not in job:
                        restore.append(json.loads(json.dumps(job)))
                    self.cancel(job['id'])
            self.lock.notify_all()
        self.kill(self.child)
        self.kill(self.player)
        self.play_worker.join(timeout=5)
        self.worker.join(timeout=5)
        if not self.worker.is_alive() and not self.play_worker.is_alive():
            with self.lock:
                for job in restore:
                    self.reset_waiting(job)
                if self.delivery_policy == 'coverage':
                    self.jobs.close()
        if self.child:
            self.child.stdin.close()
            self.child.stdout.close()

    def route(self, method, path, body):
        parts = urlsplit(path).path.strip('/').split('/')
        with self.lock:
            if parts == ['api', 'speech', 'status'] and method == 'GET':
                return self.status()
            if parts == ['api', 'voices']:
                if method == 'GET':
                    return {'voices': list(self.voices.values())}
                if method == 'POST':
                    return self.add_voice(body)
            if len(parts) == 3 and parts[:2] == ['api', 'voices'] and method == 'DELETE':
                return self.delete_voice(parts[2])
            if parts == ['api', 'speech', 'session']:
                if method == 'GET':
                    return dict(self.session)
                if method == 'POST':
                    candidate = {**self.session, **{k: body[k] for k in self.session if k in body}}
                    if not isinstance(candidate['enabled'], bool) or not isinstance(candidate['playback'], bool):
                        raise ApiError(400, 'Session enabled and playback must be booleans')
                    if candidate['language'] not in self.status()['languages']:
                        raise ApiError(422, 'Output language unsupported')
                    if candidate['enabled'] and candidate['voice_id'] not in self.voices:
                        raise ApiError(404, 'Select a reference voice first')
                    self.session = candidate
                    return dict(self.session)
            if parts == ['api', 'speech', 'events'] and method == 'POST':
                if not self.session['enabled']:
                    return {'skipped': 'speech session disabled'}
                return self.submit({**body, **self.session, 'live': True})
            if parts == ['api', 'speech', 'stop'] and method == 'POST':
                self.session['enabled'] = False
                keys = self.jobs.ids("status NOT IN ('completed','failed','cancelled','expired')") if self.delivery_policy == 'coverage' else list(self.jobs)
                for job_id in keys:
                    self.cancel(job_id)
                self.prune()
                return {'stopped': True}
            if parts == ['api', 'speech', 'jobs']:
                if method == 'GET':
                    return {'jobs': self.status()['jobs']}
                if method == 'POST':
                    return self.submit(body)
            if len(parts) >= 4 and parts[:3] == ['api', 'speech', 'jobs']:
                job = self.jobs.get(parts[3])
                if job is None:
                    raise ApiError(404, 'Speech job not found')
                if len(parts) == 4 and method == 'GET':
                    return dict(job)
                if len(parts) == 4 and method == 'DELETE':
                    return self.cancel(parts[3])
                if parts[4:] == ['result'] and method == 'GET':
                    if job['status'] != 'completed':
                        raise ApiError(409, 'Speech result is not complete')
                    return (self.root/'results'/f"{job['id']}.wav").read_bytes()
        raise ApiError(404, 'Speech route not found')


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 <= length <= 7*1024**2:
                    raise ApiError(413, 'Speech request exceeds 7 MiB')
                body = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(body, dict):
                    raise ApiError(400, 'Request must be a JSON object')
                result = service.route(self.command, self.path, body)
                binary = isinstance(result, bytes)
                payload = result if binary else json.dumps(result, ensure_ascii=False).encode()
                code = 200
            except ApiError as error:
                payload, code, binary = json.dumps({'error': error.message}).encode(), error.code, False
            except (ValueError, TypeError):
                payload, code, binary = b'{"error":"Invalid JSON request"}', 400, False
            except Exception as error:
                print(f'[speech] request failed: {error}', file=sys.stderr, flush=True)
                payload, code, binary = b'{"error":"Speech service internal error"}', 500, False
            self.send_response(code)
            self.send_header('Content-Type', 'audio/wav' if binary else 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(payload)
        do_GET = do_POST = do_DELETE = handle_request
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--engine', choices=['pocket', 'zipvoice'], default='pocket')
    parser.add_argument('--vocoder', default='')
    parser.add_argument('--threads', type=int, default=2)
    parser.add_argument('--provider', choices=['cpu', 'cuda'], default='cpu')
    parser.add_argument('--precision', choices=['int8', 'float32'], default='int8')
    parser.add_argument('--provider-config', type=Path)
    parser.add_argument('--port', type=int, default=18081)
    parser.add_argument('--data-dir', type=Path, default=Path.home()/'.local/share/mozart/speech')
    parser.add_argument('--playback-device', default='default')
    parser.add_argument('--preload',action='store_true',help='Initialize the worker before admitting live input')
    parser.add_argument('--delivery-policy', choices=['coverage', 'realtime'], default='coverage')
    args = parser.parse_args()
    command = [sys.executable, str(Path(__file__).with_name('clone_worker.py')),
        '--model', args.model, '--engine', args.engine, '--threads', str(args.threads), '--vocoder', args.vocoder,
        '--provider', args.provider, '--precision', args.precision]
    if args.provider_config:
        command += ['--provider-config', str(args.provider_config)]
    service = SpeechService(args.data_dir, command, args.engine, args.playback_device,
                            preload=args.preload, delivery_policy=args.delivery_policy)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(service))
    server.daemon_threads = True
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    print(f'[speech] listening on 127.0.0.1:{args.port} engine={args.engine}', flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == '__main__':
    main()
