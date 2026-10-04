"""Persistent activity table shared by concurrent clients and the job worker.

SQLite serializes writes across threads/processes. Private executor job files are
retained for existing adapters; they are published under the same write lock.
Recovery imports those files after migration or an interrupted table commit.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

from .activity import stamp, timing


class ActivityTable:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'activity.sqlite3'

    @contextmanager
    def transaction(self, writing=True):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create privately before sqlite opens it (also safe across processes).
        self.path.touch(mode=0o600, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS activity (id TEXT PRIMARY KEY, cursor INTEGER NOT NULL, record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS activity_meta (id INTEGER PRIMARY KEY CHECK(id=1), cursor INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO activity_meta VALUES (1, 0)')
            db.execute('BEGIN IMMEDIATE' if writing else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def save(db, data):
        visible = {k: v for k, v in data.items() if k not in {'callback', 'command'}}
        record = json.dumps(visible, sort_keys=True, allow_nan=False)
        old = db.execute('SELECT record FROM activity WHERE id=?', (data['id'],)).fetchone()
        if old and old[0] == record: return
        db.execute('UPDATE activity_meta SET cursor=cursor+1 WHERE id=1')
        cursor = db.execute('SELECT cursor FROM activity_meta WHERE id=1').fetchone()[0]
        db.execute('INSERT INTO activity VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor, record=excluded.record',
                   (data['id'], cursor, record))

    def recover(self):
        """Import legacy jobs and repair a crash between file publication and commit."""
        with self.transaction() as db:
            for path in sorted((self.root / 'jobs').glob('*.json')):
                data = json.loads(path.read_text())
                if data.get('id') == path.stem and 'tool' in data and 'state' in data:
                    self.save(db, data)

    def publish(self, path, data, atomic_write):
        with self.transaction() as db:
            previous = json.loads(path.read_text()) if path.exists() else None
            stamp(data, previous, time.time())
            atomic_write(path, data)
            self.save(db, data)

    def mutate(self, path, update, atomic_write):
        """Read/modify/write under one lock, so parallel field updates are retained."""
        with self.transaction() as db:
            previous = json.loads(path.read_text())
            data = json.loads(json.dumps(previous))
            update(data)
            stamp(data, previous, time.time())
            atomic_write(path, data)
            self.save(db, data)

    def snapshot(self, since=0):
        if type(since) is not int or since < 0: raise ValueError('Invalid table cursor')
        with self.transaction(writing=False) as db:
            cursor = db.execute('SELECT cursor FROM activity_meta WHERE id=1').fetchone()[0]
            if since > cursor: raise ValueError('Invalid table cursor')
            total = db.execute('SELECT count(*) FROM activity').fetchone()[0]
            rows = db.execute('SELECT record, cursor FROM activity WHERE cursor>? ORDER BY cursor', (since,)).fetchall()
        now = time.time()
        entries = []
        for record, revision in rows:
            row = json.loads(record)
            row.update(change_cursor=revision, timing=timing(row, now))
            entries.append(row)
        return {'scope': 'server', 'persistent': True, 'ephemeral': False,
                'cursor': cursor, 'since': since, 'total': total, 'entries': entries}

    def records(self):
        with self.transaction(writing=False) as db:
            return {identifier: json.loads(record) for identifier, record in db.execute('SELECT id, record FROM activity')}
