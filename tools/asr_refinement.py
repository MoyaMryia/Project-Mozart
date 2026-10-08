"""Run optional utterance refinement without blocking online recognition."""
import queue
import threading
import time

from asr_checks import refine_with_numeric_check, polarity_audit


class UtteranceRefiner:
    def __init__(self, build_recognizer, publish, pending_limit=2):
        self.build_recognizer, self.publish = build_recognizer, publish
        self.pending = queue.Queue(maxsize=pending_limit)
        self.jobs = {}
        self.lock = threading.RLock()
        self.closing = threading.Event()
        self.aborted = False
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def report(self, event, **changes):
        if not self.aborted:
            self.publish({**event, 'type': 'refined', 'emitted_at': time.time(), **changes})

    def submit(self, event, frames):
        with self.lock:
            if self.closing.is_set():
                return
            try:
                self.pending.put_nowait((dict(event), tuple(frames)))
                self.jobs[event['seq']] = dict(event)
            except queue.Full:
                self.report(event, refinement_status='skipped',
                            refinement_error='Refinement queue is full; online text remains available')

    def run(self):
        try:
            recognizer = self.build_recognizer()
            initialization_error = None
        except Exception as error:
            recognizer, initialization_error = None, str(error)
        while not self.aborted and (not self.closing.is_set() or not self.pending.empty()):
            try:
                event, frames = self.pending.get(timeout=.1)
            except queue.Empty:
                continue
            began = time.monotonic()
            try:
                if initialization_error is not None:
                    raise RuntimeError(initialization_error)
                text, numeric = refine_with_numeric_check(recognizer,
                    [sample for frame in frames for sample in frame], event['text'])
                if not text.strip():
                    raise ValueError('Refinement returned no text')
                changes = {'refinement_status': 'completed', 'refined_text': text,
                           'final_decode_ms': round((time.monotonic()-began)*1000),
                           'asr_polarity_audit': polarity_audit(event['text'], text)}
                if numeric is not None:
                    changes['asr_numeric_audit'] = numeric
            except Exception as error:
                changes = {'refinement_status': 'failed', 'refinement_error': str(error)}
            with self.lock:
                self.report(event, **changes)
                self.jobs.pop(event['seq'], None)
            self.pending.task_done()

    def close(self, timeout=2):
        self.closing.set()
        self.worker.join(timeout=timeout)
        with self.lock:
            if self.worker.is_alive():
                for event in self.jobs.values():
                    self.report(event, refinement_status='skipped',
                                refinement_error='Refinement stopped at the shutdown deadline')
                self.jobs.clear()
                self.aborted = True
        return not self.worker.is_alive()
