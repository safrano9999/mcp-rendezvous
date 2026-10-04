import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { Rendezvous, write } from '../src/index.js';

const contract = JSON.parse(await readFile(new URL('../../tests/fixtures/contract.json', import.meta.url)));
async function setup(t) {
  const dir = await mkdtemp(path.join(tmpdir(), 'rendezvous-activity-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const rv = new Rendezvous(contract.base, dir), session = {};
  await rv.heartbeat();
  return { rv, session, table: rv.connection(session) };
}

test('connection view isolates jobs, includes descendants and redacts delivery data', async t => {
  const { rv, session, table } = await setup(t);
  const id = await rv.queue('build_images', 'run', null, { auto_pull: true, command: ['private-command'] }, session);
  const child = await rv.queue('podman_smart1', 'pull', null, { parent_id: id });
  const other = {};
  const foreign = await rv.queue('build_images', 'run', await rv.destination('http://test.local/hook', 'private-secret'), {}, other);
  const result = await table.snapshot();
  assert.deepEqual(new Set(result.entries.map(row => row.id)), new Set([id, child]));
  assert.deepEqual((await rv.connection(other).snapshot()).entries.map(row => row.id), [foreign]);
  assert.ok(!JSON.stringify(result).includes('private'));
  assert.ok(!JSON.stringify(await rv.connection(other).snapshot()).includes('private'));
});

test('explicit cursors replay changes and do not treat elapsed time as a change', async t => {
  const { rv, session, table } = await setup(t);
  const id = await rv.queue('build_images', 'run', null, {}, session);
  const initial = await table.snapshot();
  assert.deepEqual((await table.snapshot(initial.cursor)).entries, []);
  await rv.update(id, { state: 'running', phase: 'build' });
  const [one, two] = await Promise.all([table.snapshot(initial.cursor), table.snapshot(initial.cursor)]);
  assert.equal(one.cursor, two.cursor);
  assert.equal(one.entries[0].state, 'running');
  assert.deepEqual((await table.snapshot(one.cursor)).entries, []);
  await assert.rejects(table.snapshot(one.cursor + 1), /cursor/);
});

test('worker writes generate times and timeline, including missing finished time', async t => {
  const { rv, session, table } = await setup(t);
  const id = await rv.queue('build_images', 'run', null, {}, session);
  const data = JSON.parse(await readFile(rv.path(id), 'utf8'));
  data.state = 'running'; data.phase = 'build';
  await write(rv.path(id), data);
  await rv.complete(id, 'success');
  const row = (await table.snapshot()).entries[0];
  assert.deepEqual(row.timeline.map(event => event.changes.state), ['pending', 'running', 'finished']);
  assert.ok(row.timing.finished_at.endsWith('Z'));
  assert.ok(row.timing.queue_seconds >= 0);
  assert.ok(row.timing.run_seconds >= 0);
  await rv.update(id, { outcome: 'success' });
  assert.equal((await rv.status(id)).revision, row.revision);
});

test('both transports see the published error before sending the empty trigger', async t => {
  const { rv, session, table } = await setup(t);
  for (const transport of ['webhook', 'herdr']) {
    const id = await rv.queue('build_images', 'run', null, {}, session);
    const data = JSON.parse(await readFile(rv.path(id), 'utf8'));
    Object.assign(data, { state: 'finished', outcome: 'failure', error: 'build_failed', notification: 'pending',
      callback: transport === 'herdr' ? { kind: 'herdr' } : await rv.destination('http://test.local/hook') });
    let calls = 0;
    const signal = async () => {
      calls++;
      const row = (await table.snapshot()).entries.find(row => row.id === id);
      assert.equal(row.state, 'finished'); assert.equal(row.error, 'build_failed');
      assert.ok(row.timing.finished_at);
    };
    rv.policy.route = () => ({});
    rv.notify = signal;
    rv.herdr = { ready: async () => {}, send: signal };
    await rv.deliver(rv.path(id), data);
    assert.equal(calls, 1);
  }
});

test('disconnect clears the view but leaves durable jobs for the worker', async t => {
  const { rv, session, table } = await setup(t);
  const id = await rv.queue('build_images', 'run', null, {}, session);
  rv.disconnect(session);
  await assert.rejects(table.snapshot(), /closed/);
  assert.deepEqual((await rv.connection({}).snapshot()).entries, []);
  assert.equal((await rv.status(id)).state, 'pending');
});

test('missing records remain errors, never a successful completion', async t => {
  const { rv, session, table } = await setup(t);
  const id = await rv.queue('build_images', 'run', null, {}, session);
  await rm(rv.path(id));
  assert.equal((await table.snapshot()).entries[0].error, 'job_record_unavailable');
});
