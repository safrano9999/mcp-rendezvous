"""Connection views over the persistent, shared activity table.

Workers may live in another process. There is no second status cache to update:
Publishing a job record updates the table before any completion notification.
"""
from copy import deepcopy
from datetime import datetime, timezone
import math
import threading
import time


GENERATED = {'revision', 'updated', 'timeline'}
EVENT_FIELDS = ('state', 'phase', 'outcome', 'build_status', 'steps', 'notification',
                'preparation_status', 'containerfile_updated', 'build_ready')


def stamp(data, previous, now):
    """Called by the atomic writer, including for application-owned worker writes."""
    if data.get('state') == 'running' and data.get('started') is None:
        started = (previous or {}).get('started')
        data['started'] = now if started is None else started
        data.setdefault('started_source', 'observed')
    if data.get('state') == 'finished' and data.get('finished') is None:
        finished = (previous or {}).get('finished')
        data['finished'] = now if finished is None else finished
    old = previous or {}
    meaningful = lambda value: {k: v for k, v in value.items() if k not in GENERATED}
    changed = previous is None or meaningful(data) != meaningful(old)
    data['revision'] = old.get('revision', 0) + int(changed)
    data['updated'] = now if changed else old.get('updated', now)
    data['timeline'] = deepcopy(old.get('timeline', []))
    changes = {key: deepcopy(data.get(key)) for key in EVENT_FIELDS
               if (key in data or key in old) and (previous is None or data.get(key) != old.get(key))}
    if changes:
        data['timeline'].append({'at': now, 'changes': changes})


def timing(data, now):
    def timestamp(key):
        value = data.get(key)
        return value if type(value) in (int, float) and math.isfinite(value) else None
    def iso(value):
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z') if value is not None else None
    created, started, finished = (timestamp(k) for k in ('created', 'started', 'finished'))
    end = finished if finished is not None else now
    return {
        'queued_at': iso(created), 'started_at': iso(started),
        'updated_at': iso(timestamp('updated')), 'finished_at': iso(finished),
        'notified_at': iso(timestamp('delivered')),
        'elapsed_seconds': max(0, end - created) if created is not None else None,
        'queue_seconds': max(0, started - created) if created is not None and started is not None else None,
        'run_seconds': max(0, end - started) if started is not None else None,
    }


class ConnectionActivity:
    """A read-only view; the application supplies the actual connection object.

    No connection identifier is persisted or accepted from an MCP caller. A
    WeakKeyDictionary in Rendezvous drops membership when that session is gone.
    The cursor is explicit: reading does not acknowledge or consume changes.
    """
    def __init__(self, rendezvous):
        self.rv = rendezvous
        self._roots = set()
        self._seen = {}
        self._versions = {}
        self._cursor = 0
        self._closed = False
        self._lock = threading.RLock()

    def track(self, identifier):
        self.rv.path(identifier)  # Validate before altering membership.
        with self._lock:
            if self._closed: raise ValueError('Connection activity is closed')
            self._roots.add(identifier)

    def untrack(self, identifier):
        with self._lock:
            self._roots.discard(identifier)

    def close(self):
        with self._lock:
            self._closed = True
            self._roots.clear()
            self._seen.clear()
            self._versions.clear()

    def snapshot(self, since=0):
        with self._lock:
            if self._closed: raise ValueError('Connection activity is closed')
            if type(since) is not int or not 0 <= since <= self._cursor:
                raise ValueError('Invalid cursor for this connection')
            jobs = self.rv.table.records() if self._roots else {}
            owned = set(self._roots)
            while True:
                children = {identifier for identifier, data in jobs.items()
                            if data.get('parent_id') in owned}
                added = children - owned
                if not added: break
                owned.update(added)
            now = time.time()
            rows = []
            for identifier in sorted(owned, key=lambda key: (jobs.get(key, {}).get('created', 0), key)):
                data = jobs.get(identifier)
                if data is None:
                    row = {'id': identifier, 'state': 'unavailable', 'error': 'job_record_unavailable'}
                else:
                    row = {k: deepcopy(v) for k, v in data.items() if k not in {'callback', 'command'}}
                if self._seen.get(identifier) != row:
                    self._cursor += 1
                    self._seen[identifier] = deepcopy(row)
                    self._versions[identifier] = self._cursor
                version = self._versions[identifier]
                if version <= since: continue
                row['change_cursor'] = version
                row['timing'] = timing(row, now)
                rows.append(row)
            return {'scope': 'connection', 'persistent': True, 'ephemeral': False,
                    'membership': 'connection', 'cursor': self._cursor,
                    'since': since, 'total': len(owned), 'entries': rows}
