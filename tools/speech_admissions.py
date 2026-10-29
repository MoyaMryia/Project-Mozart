"""保存语音受理快照，按原始话语标识去重。"""
import json
import threading
import time

from durable_jobs import connect, own


class AdmissionLedger:
    """为每个已受理的话语标识保存一份持久快照。"""

    def __init__(self, path):
        self.owner = own(path)
        self.database = connect(path)
        self.lock = threading.RLock()
        with self.database:
            self.database.execute('''CREATE TABLE IF NOT EXISTS admissions (
                utterance TEXT PRIMARY KEY, job TEXT NOT NULL,
                archived INTEGER NOT NULL, updated REAL NOT NULL)''')

    def find(self, utterance):
        """返回已保存快照和归档标记，缺少记录时返回 None。"""
        with self.lock:
            row = self.database.execute(
                'SELECT job, archived FROM admissions WHERE utterance=?', (utterance,)).fetchone()
        if row is None:
            return None
        return {'job': json.loads(row[0]), 'archived': bool(row[1])}

    def record(self, job, archived=False):
        """保存当前任务快照，替换旧快照。"""
        with self.lock, self.database:
            self.database.execute('''INSERT INTO admissions VALUES (?,?,?,?)
                ON CONFLICT(utterance) DO UPDATE SET job=excluded.job,
                archived=excluded.archived, updated=excluded.updated''',
                (job['utterance_id'], json.dumps(job, ensure_ascii=False),
                 int(archived), time.time()))

    def archive(self, job):
        """在删除任务记录前保存终态快照。"""
        self.record(job, archived=True)

    def close(self):
        try:
            self.database.close()
        finally:
            self.owner.close()
