import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import { isDeepStrictEqual } from 'node:util';

export const GENERATED = ['revision', 'updated', 'timeline'];
const EVENT_FIELDS = ['state', 'phase', 'outcome', 'build_status', 'steps', 'notification',
  'preparation_status', 'containerfile_updated', 'build_ready'];
const meaningful = value => Object.fromEntries(Object.entries(value).filter(([key]) => !GENERATED.includes(key)));

export function stamp(data, previous, now) {
  const old = previous ?? {};
  if (data.state === 'running' && data.started == null) {
    data.started = old.started ?? now;
    data.started_source ??= 'observed';
  }
  if (data.state === 'finished' && data.finished == null) data.finished = old.finished ?? now;
  const changed = !previous || !isDeepStrictEqual(meaningful(data), meaningful(old));
  data.revision = (old.revision ?? 0) + Number(changed);
  data.updated = changed ? now : (old.updated ?? now);
  data.timeline = structuredClone(old.timeline ?? []);
  const changes = {};
  for (const key of EVENT_FIELDS) {
    if ((key in data || key in old) && (!previous || !isDeepStrictEqual(data[key], old[key]))) {
      changes[key] = structuredClone(data[key] ?? null);
    }
  }
  if (Object.keys(changes).length) data.timeline.push({ at: now, changes });
}

function timing(data, now) {
  const get = key => typeof data[key] === 'number' && Number.isFinite(data[key]) ? data[key] : null;
  const iso = value => value == null ? null : new Date(value * 1000).toISOString();
  const created = get('created'), started = get('started'), finished = get('finished');
  const end = finished ?? now;
  return {
    queued_at: iso(created), started_at: iso(started), updated_at: iso(get('updated')),
    finished_at: iso(finished), notified_at: iso(get('delivered')),
    elapsed_seconds: created == null ? null : Math.max(0, end - created),
    queue_seconds: created == null || started == null ? null : Math.max(0, started - created),
    run_seconds: started == null ? null : Math.max(0, end - started),
  };
}

// Membership alone is ephemeral. Reading the atomic job records also observes
// writes from a separate worker, committed before its completion-only signal.
export class ConnectionActivity {
  constructor(rv) {
    this.rv = rv;
    this.roots = new Set();
    this.seen = new Map();
    this.versions = new Map();
    this.cursor = 0;
    this.closed = false;
    this.pending = Promise.resolve();
  }
  track(id) {
    this.rv.path(id);
    if (this.closed) throw new Error('Connection activity is closed');
    this.roots.add(id);
  }
  untrack(id) { this.roots.delete(id); }
  close() {
    this.closed = true;
    this.roots.clear(); this.seen.clear(); this.versions.clear();
  }
  snapshot(since = 0) {
    const result = this.pending.then(() => this.read(since));
    this.pending = result.catch(() => {});
    return result;
  }
  async read(since) {
    if (this.closed) throw new Error('Connection activity is closed');
    if (!Number.isSafeInteger(since) || since < 0 || since > this.cursor) throw new Error('Invalid cursor for this connection');
    const jobs = new Map();
    if (this.roots.size) {
      let files;
      try { files = await readdir(path.join(this.rv.root, 'jobs')); }
      catch (error) { if (error.code !== 'ENOENT') throw error; files = []; }
      for (const file of files.filter(name => /^[0-9a-f]{32}\.json$/.test(name))) {
        try {
          const data = JSON.parse(await readFile(path.join(this.rv.root, 'jobs', file), 'utf8'));
          if (data?.id === file.slice(0, -5)) jobs.set(data.id, data);
        } catch (error) {
          if (!(error instanceof SyntaxError) && !['ENOENT', 'EACCES'].includes(error.code)) throw error;
        }
      }
    }
    if (this.closed) throw new Error('Connection activity is closed');
    const owned = new Set(this.roots);
    let count;
    do {
      count = owned.size;
      for (const [id, data] of jobs) if (owned.has(data.parent_id)) owned.add(id);
    } while (owned.size !== count);
    const now = Date.now() / 1000, entries = [];
    const ids = [...owned].sort((a, b) => (jobs.get(a)?.created ?? 0) - (jobs.get(b)?.created ?? 0) || a.localeCompare(b));
    for (const id of ids) {
      const data = jobs.get(id);
      const row = data ? structuredClone(Object.fromEntries(Object.entries(data).filter(([key]) => !['callback', 'command'].includes(key))))
        : { id, state: 'unavailable', error: 'job_record_unavailable' };
      if (!isDeepStrictEqual(this.seen.get(id), row)) {
        this.versions.set(id, ++this.cursor);
        this.seen.set(id, structuredClone(row));
      }
      const version = this.versions.get(id);
      if (version <= since) continue;
      row.change_cursor = version;
      row.timing = timing(row, now);
      entries.push(row);
    }
    return { scope: 'connection', ephemeral: true, cursor: this.cursor, since, total: owned.size, entries };
  }
}
