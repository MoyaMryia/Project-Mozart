"""独立投递已通过校验的播报请求，持久保存状态与通知。"""
import json
import threading
import time
import urllib.error

from durable_jobs import connect, own

REQUEST_TIMEOUT_SECONDS = 3
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1
SUBMISSION_PRIORITY = ('failed', 'retrying', 'sending', 'queued', 'skipped', 'submitted')


def admission_result(response):
    """在 HTTP 响应入口检查一次服务端的投递结果。"""
    if not isinstance(response, dict):
        raise ValueError('Speech admission response must be an object')
    job = response.get('id')
    skipped = response.get('skipped')
    if isinstance(job, str) and job and skipped is None:
        return 'submitted', job
    if isinstance(skipped, str) and skipped and job is None:
        return 'skipped', skipped
    raise ValueError('Speech admission response needs a job ID or an explicit skipped reason')


class SpeechOutbox:
    def __init__(self, path, request, publish):
        self.request, self.publish = request, publish
        self.lock = threading.RLock()
        self.abort_event = threading.Event()
        self.changed = threading.Event()
        self.closing = threading.Event()
        self.worker_error = None
        self.owner = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.owner = own(path)
        self.database = connect(path if path is not None else ':memory:')
        with self.database:
            self.database.execute('''CREATE TABLE IF NOT EXISTS speech_outbox (
                utterance TEXT PRIMARY KEY, body TEXT NOT NULL, context TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
                retry_at REAL, notified INTEGER NOT NULL DEFAULT 0)''')
            self.database.execute("""UPDATE speech_outbox SET state='retrying',
                error='Interrupted before speech admission was confirmed', retry_at=?, notified=0
                WHERE state='sending'""", (time.time(),))
        self.worker = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.worker.start()

    def raise_worker_error(self):
        if self.worker_error is not None:
            raise RuntimeError(f'Speech delivery worker failed: {self.worker_error}') from self.worker_error

    @staticmethod
    def fields(row):
        state, attempts, error, result, retry_at = row
        changes = {'speech_submission_status': state, 'speech_submission_attempts': attempts,
                   'speech_last_error': error, 'speech_retry_at': retry_at,
                   'speech_error': error if state == 'failed' else ''}
        if state == 'submitted':
            changes['speech'] = result
        elif state == 'skipped':
            changes['speech_skipped_reason'] = result
        return changes

    def enqueue(self, body, context):
        key = body['utterance_id']
        self.raise_worker_error()
        with self.lock, self.database:
            if self.closing.is_set():
                raise RuntimeError('Speech delivery queue is closed')
            existing = self.database.execute(
                'SELECT body FROM speech_outbox WHERE utterance=?', (key,)).fetchone()
            if existing is not None:
                if json.loads(existing[0]) != body:
                    raise ValueError('Speech utterance ID already has a different request')
            else:
                self.database.execute('''INSERT INTO speech_outbox
                    (utterance, body, context, state) VALUES (?,?,?,'queued')''',
                    (key, json.dumps(body, ensure_ascii=False), json.dumps(context, ensure_ascii=False)))
            row = self.database.execute('''SELECT state,attempts,error,result,retry_at
                FROM speech_outbox WHERE utterance=?''', (key,)).fetchone()
        self.changed.set()
        return self.fields(row)

    def lookup(self, key):
        with self.lock:
            row = self.database.execute('''SELECT context,state,attempts,error,result,retry_at
                FROM speech_outbox WHERE utterance=?''', (key,)).fetchone()
        return None if row is None else (json.loads(row[0]), self.fields(row[1:]))

    def acknowledge(self, key, changes, context):
        with self.lock, self.database:
            self.database.execute('''UPDATE speech_outbox SET notified=1,context=?
                WHERE utterance=? AND state=? AND attempts=?''',
                (json.dumps(context, ensure_ascii=False), key,
                 changes['speech_submission_status'], changes['speech_submission_attempts']))

    def abort(self):
        self.closing.set()
        self.abort_event.set()
        self.changed.set()

    def _state(self, key, state, *, error=None, result='', retry_at=None):
        with self.lock, self.database:
            if error is None:
                self.database.execute('''UPDATE speech_outbox SET state=?,result=?,retry_at=?,notified=0
                    WHERE utterance=?''', (state, result, retry_at, key))
            else:
                self.database.execute('''UPDATE speech_outbox
                    SET state=?,error=?,result=?,retry_at=?,notified=0 WHERE utterance=?''',
                    (state, error, result, retry_at, key))

    def _notify(self):
        with self.lock:
            row = self.database.execute('''SELECT utterance,context,state,attempts,error,result,retry_at
                FROM speech_outbox WHERE notified=0 ORDER BY rowid LIMIT 1''').fetchone()
        if row is None:
            return False
        key, context, *state = row
        context, changes = json.loads(context), self.fields(state)
        if not self.publish(context, changes):
            self.abort()
            return False
        self.acknowledge(key, changes, context)
        return True

    def _next(self):
        with self.lock:
            return self.database.execute('''SELECT utterance,body,attempts,retry_at
                FROM speech_outbox WHERE state IN ('queued','retrying') ORDER BY rowid LIMIT 1''').fetchone()

    def _attempt(self, row):
        key, body, attempts, _ = row
        if attempts >= MAX_ATTEMPTS:
            self._state(key, 'failed', error='Speech admission remains unconfirmed after the attempt limit')
            return
        attempts += 1
        with self.lock, self.database:
            self.database.execute("""UPDATE speech_outbox
                SET state='sending',attempts=?,retry_at=NULL,notified=0 WHERE utterance=?""", (attempts, key))
        self._notify()
        if self.abort_event.is_set():
            with self.lock, self.database:
                self.database.execute("""UPDATE speech_outbox
                    SET state='queued',attempts=attempts-1,notified=0 WHERE utterance=?""", (key,))
            return
        retryable = False
        try:
            response = self.request(json.loads(body))
            state, result = admission_result(response)
        except urllib.error.HTTPError as error:
            message = f'HTTP {error.code}: {error.reason}'
            retryable = error.code in (408, 429) or error.code >= 500
            error.close()
        except OSError as error:
            message, retryable = str(error), True
        except ValueError as error:
            message = str(error)
        else:
            self._state(key, state, result=result)
            return
        if retryable and attempts < MAX_ATTEMPTS:
            self._state(key, 'retrying', error=message, retry_at=time.time()+RETRY_DELAY_SECONDS)
        else:
            self._state(key, 'failed', error=message)

    def run(self):
        try:
            while not self.abort_event.is_set():
                self.changed.clear()
                if self._notify():
                    continue
                row = self._next()
                if row is None:
                    if self.closing.is_set():
                        break
                    self.changed.wait(.1)
                    continue
                retry_at = row[3]
                if retry_at is not None:
                    remaining = retry_at-time.time()
                    if remaining > 0:
                        self.changed.wait(remaining)
                        continue
                self._attempt(row)
        except Exception as error:
            self.worker_error = error
            raise

    def close(self):
        self.closing.set()
        self.changed.set()
        self.worker.join()
        try:
            with self.lock:
                self.database.close()
        finally:
            if self.owner is not None:
                self.owner.close()
        self.raise_worker_error()
