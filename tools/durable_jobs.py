"""Store waiting work on disk. Keep a limited cache of speech records."""
from collections import OrderedDict
from collections.abc import MutableMapping
import json
import fcntl
from pathlib import Path
import queue
import sqlite3
import threading
import time

TERMINAL = ('completed', 'failed', 'cancelled', 'expired')


def connect(path):
    database = sqlite3.connect(str(path), check_same_thread=False)
    database.execute('PRAGMA journal_mode=WAL')
    database.execute('PRAGMA synchronous=FULL')
    return database


def own(path):
    owner = open(str(path)+'.lock', 'a')
    try:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        owner.close()
        raise RuntimeError('Another process owns this disk queue') from None
    return owner


class DiskJobs(MutableMapping):
    def __init__(self, path, cache_limit=128, pinned=lambda: set()):
        self.owner = own(path)
        self.database = connect(path)
        self.lock = threading.RLock()
        self.cache, self.cache_limit, self.pinned = OrderedDict(), cache_limit, pinned
        self.database.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
            utterance TEXT NOT NULL, voice TEXT NOT NULL, created REAL NOT NULL,
            estimate REAL NOT NULL, pending INTEGER NOT NULL)''')
        self.database.execute('CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(pending, created)')
        self.database.execute('CREATE INDEX IF NOT EXISTS jobs_utterance ON jobs(utterance)')
        self.database.execute('CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status)')
        self.database.execute('CREATE INDEX IF NOT EXISTS jobs_voice ON jobs(voice,status)')
        self.database.commit()

    def trim(self, protect=()):
        pinned = self.pinned() | set(protect)
        for key in list(self.cache):
            if len(self.cache) <= self.cache_limit:
                break
            job = self.cache[key]
            if key not in pinned and (job['status'] == 'queued' or job['status'] in TERMINAL):
                del self.cache[key]

    def __getitem__(self, key):
        with self.lock:
            if key not in self.cache:
                row = self.database.execute('SELECT payload FROM jobs WHERE id=?', (key,)).fetchone()
                if row is None:
                    raise KeyError(key)
                self.cache[key] = json.loads(row[0])
            self.cache.move_to_end(key)
            result = self.cache[key]
            self.trim((key,))
            return result

    def __setitem__(self, key, job):
        with self.lock, self.database:
            self.database.execute('''INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, status=excluded.status,
                utterance=excluded.utterance, voice=excluded.voice, estimate=excluded.estimate,
                pending=CASE WHEN excluded.status IN ('completed','failed','cancelled','expired')
                             THEN 0 ELSE jobs.pending END''',
                (key, json.dumps(job, ensure_ascii=False), job['status'], job.get('utterance_id', ''),
                 job['voice_id'], job['created_at'], job.get('estimated_duration_seconds', 0),
                 int(job['status'] == 'queued')))
            self.cache[key] = job
            self.cache.move_to_end(key)
            self.trim((key,))

    def __delitem__(self, key):
        with self.lock, self.database:
            if not self.database.execute('DELETE FROM jobs WHERE id=?', (key,)).rowcount:
                raise KeyError(key)
            self.cache.pop(key, None)

    def __iter__(self):
        with self.lock:
            cursor = self.database.execute('SELECT id FROM jobs ORDER BY created, rowid')
        while True:
            with self.lock:
                rows = cursor.fetchmany(64)
            if not rows:
                return
            yield from (row[0] for row in rows)

    def __len__(self):
        with self.lock:
            return self.database.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]

    def ids(self, clause, parameters=()):
        with self.lock:
            return [r[0] for r in self.database.execute(
                'SELECT id FROM jobs WHERE '+clause+' ORDER BY created, rowid', parameters)]

    def unfinished(self):
        with self.lock:
            return self.database.execute("SELECT COUNT(*) FROM jobs WHERE status NOT IN ('completed','failed','cancelled','expired')").fetchone()[0]

    def voice_busy(self, voice):
        with self.lock:
            return self.database.execute("SELECT 1 FROM jobs WHERE voice=? AND status NOT IN ('completed','failed','cancelled','expired') LIMIT 1", (voice,)).fetchone() is not None

    def find_utterance(self, utterance):
        with self.lock:
            row = self.database.execute('SELECT id FROM jobs WHERE utterance=? ORDER BY created LIMIT 1', (utterance,)).fetchone()
            return row[0] if row else None

    def recent(self, limit):
        with self.lock:
            keys = [r[0] for r in self.database.execute('SELECT id FROM jobs ORDER BY created DESC, rowid DESC LIMIT ?', (limit,))]
            keys = list(dict.fromkeys(list(self.pinned())+keys))
            return [dict(self[key]) for key in keys if key in self]

    def history_to_remove(self, limit):
        with self.lock:
            return [r[0] for r in self.database.execute("SELECT id FROM jobs WHERE status IN ('completed','failed','cancelled','expired') ORDER BY created DESC, rowid DESC LIMIT -1 OFFSET ?", (limit,)) if r[0] not in self.pinned()]

    def close(self):
        self.database.close()
        self.owner.close()


class DiskPending:
    def __init__(self, jobs):
        self.jobs = jobs

    def __len__(self):
        with self.jobs.lock:
            return self.jobs.database.execute('SELECT COUNT(*) FROM jobs WHERE pending=1').fetchone()[0]

    def append(self, key):
        with self.jobs.lock, self.jobs.database:
            self.jobs.database.execute('UPDATE jobs SET pending=1 WHERE id=?', (key,))

    def popleft(self):
        with self.jobs.lock, self.jobs.database:
            row = self.jobs.database.execute('SELECT id FROM jobs WHERE pending=1 ORDER BY created, rowid LIMIT 1').fetchone()
            if row is None:
                raise IndexError('The disk queue is empty')
            self.jobs.database.execute('UPDATE jobs SET pending=0 WHERE id=?', row)
            return row[0]

    def discard(self, key):
        with self.jobs.lock, self.jobs.database:
            self.jobs.database.execute('UPDATE jobs SET pending=0 WHERE id=?', (key,))

    def estimate(self):
        with self.jobs.lock:
            return self.jobs.database.execute('SELECT COALESCE(SUM(estimate),0) FROM jobs WHERE pending=1').fetchone()[0]


class DiskFifo:
    """Keep one leased item until its worker completes the request."""
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.owner = own(path)
        self.database = connect(path)
        self.lock = threading.RLock()
        self.current = None
        with self.database:
            self.database.execute('CREATE TABLE IF NOT EXISTS work (key TEXT UNIQUE, payload TEXT, leased INTEGER DEFAULT 0)')
            self.database.execute('UPDATE work SET leased=0')

    def put_nowait(self, item):
        with self.lock, self.database:
            self.database.execute('INSERT OR IGNORE INTO work(key,payload) VALUES (?,?)',
                                  (item.get('work_key', item['record']['utterance_id']), json.dumps(item, ensure_ascii=False)))

    def get(self, timeout=.1):
        deadline = time.monotonic()+timeout
        while True:
            with self.lock, self.database:
                row = self.database.execute('SELECT key,payload FROM work WHERE leased=0 ORDER BY rowid LIMIT 1').fetchone()
                if row:
                    self.database.execute('UPDATE work SET leased=1 WHERE key=?', (row[0],))
                    self.current = row[0]
                    return json.loads(row[1])
            if time.monotonic() >= deadline:
                raise queue.Empty
            time.sleep(.01)

    def record(self, key):
        with self.lock:
            row = self.database.execute('SELECT payload FROM work WHERE key=?', (key,)).fetchone()
            return json.loads(row[0])['record'] if row else None

    def save_record(self, key, record):
        with self.lock, self.database:
            row = self.database.execute('SELECT payload FROM work WHERE key=?', (key,)).fetchone()
            if row:
                payload = json.loads(row[0])
                payload['record'] = record
                self.database.execute('UPDATE work SET payload=? WHERE key=?', (json.dumps(payload, ensure_ascii=False), key))

    def task_done(self, retain=False):
        with self.lock, self.database:
            if retain:
                self.database.execute('UPDATE work SET leased=0 WHERE key=?', (self.current,))
            else:
                self.database.execute('DELETE FROM work WHERE key=?', (self.current,))
            self.current = None

    def empty(self):
        with self.lock:
            return self.database.execute('SELECT 1 FROM work WHERE leased=0 LIMIT 1').fetchone() is None

    def close(self):
        self.database.close()
        self.owner.close()
