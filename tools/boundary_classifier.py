"""Find source boundaries with the Chinese NNSplit model."""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import queue
import threading
import time


MODEL_SHA256 = '6cfdf98b039e38e84f7efea279b87666de12b3928576a1b331a0c8260eb34894'


def classifier_prefix(history, committed):
    """Require repeated source observations before classification."""
    if len(history) < 2 or history[-1][0]-history[0][0] < .2:
        return None
    prefix = os.path.commonprefix([item[1] for item in history])
    return prefix if prefix.startswith(committed) and len(prefix)-len(committed) >= 12 else None


def choose_boundary(text, committed, scores, threshold=.5, max_chars=24):
    """Use a model boundary or a length limit. Preserve every source character."""
    start = len(committed)
    eligible = [end for end in range(start+8, min(len(text)-4, start+max_chars)+1)
                if not (text[end-1].isalnum() and text[end].isalnum()
                        and text[end-1].isascii() and text[end].isascii())]
    if not eligible:
        return None
    predicted = [end for end in eligible if scores.get(end, 0) >= threshold]
    if predicted:
        end, reason = predicted[0], 'model'
    elif len(text)-start >= max_chars+4:
        # The user permits incomplete clauses to reduce submission delay.
        end, reason = max(eligible, key=lambda end: (scores.get(end, 0), end)), 'length_limit'
    else:
        return None
    return {'end': end, 'score': scores.get(end, 0), 'reason': reason}


class BoundaryClassifier:
    """Run the fixed Chinese model with one CPU thread and bounded input."""

    def __init__(self, path, threshold=.5, max_chars=24):
        import numpy as np
        import onnxruntime as ort
        self.np = np
        path = Path(path)
        self.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if self.sha256 != MODEL_SHA256:
            raise ValueError('Boundary model SHA-256 does not match the supported Chinese model')
        if not 0 < threshold < 1 or not 12 <= max_chars <= 64:
            raise ValueError('Boundary threshold or source length limit is invalid')
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), options, providers=['CPUExecutionProvider'])
        instructions = json.loads(self.session.get_modelmeta().custom_metadata_map['split_sequence'])['instructions']
        if instructions[0] != ['Sentence', {'PredictionIndex': 0}]:
            raise ValueError('Boundary model sentence output is invalid')
        self.threshold, self.max_chars = threshold, max_chars

    def predict(self, text, committed):
        began = time.monotonic()
        # Retain left context. Limit UTF-8 input to less than 500 bytes.
        offset = max(0, len(committed)-24)
        view = text[offset:offset+120]
        raw = view.encode('utf-8')
        values = self.np.zeros((1, (len(raw)+11)//2*2), dtype=self.np.uint8)
        values[0, 5:5+len(raw)] = self.np.frombuffer(raw, dtype=self.np.uint8)
        output = self.session.run(['output'], {'input': values})[0]
        if output.shape != (*values.shape, 2) or not self.np.isfinite(output).all():
            raise ValueError('Boundary model returned invalid predictions')
        probabilities = 1/(1+self.np.exp(-self.np.clip(output[0, :, 0], -80, 80)))
        scores, byte_end = {}, 0
        for index, char in enumerate(view):
            begin = byte_end
            byte_end += len(char.encode('utf-8'))
            # Upstream rounds a predicted byte boundary to the next UTF-8 boundary.
            scores[offset+index+1] = float(probabilities[5+begin:5+byte_end].max())
        decision = choose_boundary(text, committed, scores, self.threshold, self.max_chars)
        return {'cut': decision, 'elapsed_ms': (time.monotonic()-began)*1000,
                'model_sha256': self.sha256, 'threshold': self.threshold,
                'max_chars': self.max_chars, 'input_chars': len(view)}


class BoundaryWorker:
    """Replace waiting partial requests. Do not delay the source-caption reader."""

    def __init__(self, model, callback):
        self.model, self.callback = model, callback
        self.pending = queue.Queue(maxsize=1)
        self.stopped = threading.Event()
        self.seen = OrderedDict()
        self.thread = threading.Thread(target=self.run, name='boundary-classifier', daemon=True)
        self.thread.start()

    def submit(self, key, event, prefix, committed):
        identity = (prefix, committed)
        if self.seen.get(key) == identity or self.stopped.is_set():
            return
        self.seen[key] = identity
        self.seen.move_to_end(key)
        while len(self.seen) > 128:
            self.seen.popitem(last=False)
        item = (key, deepcopy(event), prefix, committed)
        try:
            self.pending.put_nowait(item)
        except queue.Full:
            try:
                old_key, _, old_prefix, old_committed = self.pending.get_nowait()
                if self.seen.get(old_key) == (old_prefix, old_committed):
                    self.seen.pop(old_key)
            except queue.Empty:
                pass
            self.pending.put_nowait(item)

    def run(self):
        while not self.stopped.is_set():
            try:
                _, event, prefix, committed = self.pending.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                result = self.model.predict(prefix, committed)
            except Exception as error:
                result = {'cut': None, 'error': str(error)}
            if not self.stopped.is_set():
                self.callback(event, {**result, 'prefix': prefix, 'committed': committed})

    def close(self):
        self.stopped.set()
        self.thread.join()
