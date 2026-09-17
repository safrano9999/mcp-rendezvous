import { readFile, writeFile, mkdir, open, rename, unlink, stat, readdir } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { randomUUID, createHmac } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import Ajv from 'ajv';
import { Herdr, TargetChanged, NotReady, DeliveryUncertain } from './herdr.js';
export { Herdr, TargetChanged, NotReady, DeliveryUncertain };

const validate = new Ajv({ strict: false }).compile(JSON.parse(readFileSync(new URL('../schema.json', import.meta.url))));
const now = () => Date.now() / 1000;
const load = async file => JSON.parse(await readFile(file, 'utf8'));
const optional = async file => { try { return await load(file); } catch (e) { if (e.code === 'ENOENT') return null; throw e; } };
export const STOP_AFTER_DISPATCH = 'End this turn immediately and return control to the user. Do not poll, sleep, wait for completion, or start a status loop. ';
export function completionNext(destination) {
  if (!destination) return STOP_AFTER_DISPATCH + 'No completion notification was requested.';
  const channel = destination.kind === 'herdr' ? 'Herdr (finished + Enter)' : 'webhook';
  return STOP_AFTER_DISPATCH + `You will be notified via ${channel} when finished, regardless of outcome. Fetch status/logs only after that notification or an explicit user request.`;
}

export async function write(file, data) {
  await mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const temp = path.join(path.dirname(file), '.write-' + randomUUID());
  let handle;
  try {
    handle = await open(temp, 'wx', 0o600);
    await handle.writeFile(JSON.stringify(data));
    await handle.sync();
    await handle.close(); handle = null;
    await rename(temp, file);
  } finally {
    if (handle) await handle.close();
    await unlink(temp).catch(e => { if (e.code !== 'ENOENT') throw e; });
  }
}

export class Policy {
  constructor(config) {
    if (typeof config === 'string') config = JSON.parse(readFileSync(config, 'utf8'));
    if (!validate(config)) throw new Error('Invalid mcp-rendezvous operator policy');
    this.config = structuredClone(config);
    this.routes = this.config.feedback.routes;
  }
  allow(tool, action) {
    if (!Object.hasOwn(this.config.tools, tool) || !this.config.tools[tool].actions.includes(action)) throw new Error('Tool/action is not permitted by the feedback policy');
  }
  route(name) {
    if (!Object.hasOwn(this.routes, name)) throw new Error('Feedback route is not permitted');
    return this.routes[name];
  }
  endpoint(url, secret = '') {
    const route = this.route('webhook');
    if (typeof url !== 'string' || typeof secret !== 'string' || !url || url.length > 4096 || secret.length > 4096 ||
        !/^https?:\/\/[^/?#]+/i.test(url) || /[\x00-\x20\x7f\\#]/.test(url) || /[\r\n]/.test(secret)) throw new Error('Invalid callback URL or secret');
    let parsed;
    try { parsed = new URL(url); } catch { throw new Error('Invalid callback URL or secret'); }
    const authority = url.split('://')[1]?.split('/')[0] ?? '';
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || authority.includes('@') || parsed.port === '0') throw new Error('Invalid callback URL or secret');
    const host = parsed.hostname.toLowerCase();
    if (route.allowed_hosts && !route.allowed_hosts.some(allowed => host === allowed || (allowed.startsWith('*.') && host.endsWith(allowed.slice(1))))) throw new Error('Callback hostname is not permitted');
    return { url, secret };
  }
}

export class Rendezvous {
  constructor(config, stateDir) {
    this.policy = new Policy(config);
    this.root = path.resolve(stateDir);
    this.herdr = this.policy.routes.herdr ? new Herdr(this.policy.route('herdr')) : null;
  }
  endpoint(url, secret = '') { return this.policy.endpoint(url, secret); }
  async destination(url = '', secret = '', herdrTarget = '') {
    if (herdrTarget) {
      if (url || secret) throw new Error('Choose herdr_target OR callback_url/callback_secret');
      this.policy.route('herdr');
      return this.herdr.bind(herdrTarget);
    }
    return this.endpoint(url, secret);
  }
  async configure(action = 'get', url = '', secret = '', herdrTarget = '') {
    const file = path.join(this.root, 'default.json');
    if (action === 'set') await write(file, await this.destination(url, secret, herdrTarget));
    else if (action === 'clear') await unlink(file).catch(e => { if (e.code !== 'ENOENT') throw e; });
    else if (action !== 'get') throw new Error('Unknown feedback configuration action');
    const value = await optional(file);
    return { configured: value !== null, callback_url: value?.url ?? null, herdr_target: value?.identity?.pane_id ?? null,
      transport: value ? (value.kind === 'herdr' ? 'herdr' : 'webhook') : null, signed: Boolean(value?.secret), feedback_default: this.policy.config.feedback.default };
  }
  async resolve(enabled = null, url = '', secret = '', herdrTarget = '') {
    if (enabled !== null && typeof enabled !== 'boolean') throw new Error('feedback must be boolean');
    if (enabled === null) enabled = Boolean(url || herdrTarget || this.policy.config.feedback.default);
    if (!enabled) return null;
    const saved = await optional(path.join(this.root, 'default.json'));
    if (url || herdrTarget) {
      if (!herdrTarget && !secret && saved?.url === url) secret = saved.secret;
      return this.destination(url, secret, herdrTarget);
    }
    if (secret) throw new Error('callback_secret requires callback_url');
    if (!saved) throw new Error('Feedback requires herdr_target, callback_url or configure_feedback(action=set)');
    if (saved.kind === 'herdr') {
      this.policy.route('herdr');
      if (!isDeepStrictEqual(await this.herdr.bind(saved.identity.pane_id), saved)) throw new TargetChanged('Saved Herdr agent has changed; configure again');
      return saved;
    }
    return this.endpoint(saved.url, saved.secret);
  }
  async heartbeat() {
    await mkdir(this.root, { recursive: true, mode: 0o700 });
    const file = path.join(this.root, 'heartbeat');
    await writeFile(file, '', { mode: 0o600 });
  }
  path(id) {
    if (typeof id !== 'string' || id.length !== 32 || !/^[0-9a-f]{32}$/.test(id)) throw new Error('Invalid feedback_id');
    return path.join(this.root, 'jobs', id + '.json');
  }
  async queue(tool, action, destination, values = {}) {
    this.policy.allow(tool, action);
    if (['schema_version','id','tool','action','callback','created','notification','attempts'].some(k => Object.hasOwn(values, k))) throw new Error('Reserved job fields cannot be overwritten');
    if (!['pending','dispatching','finished'].includes(values.state ?? 'pending')) throw new Error('Invalid initial job state');
    if (destination) {
      this.policy.route(destination.kind === 'herdr' ? 'herdr' : 'webhook');
      if (destination.kind !== 'herdr') this.endpoint(destination.url, destination.secret);
    }
    let live = false;
    try { live = now() - (await stat(path.join(this.root, 'heartbeat'))).mtimeMs / 1000 <= 120; } catch {}
    if (!live) throw new Error('Completion worker is not running; operation was not dispatched');
    const id = randomUUID().replaceAll('-', '');
    await write(this.path(id), { schema_version: 1, id, tool, action, kind: values.kind ?? tool, callback: destination,
      created: now(), state: 'pending', notification: destination ? 'pending' : 'disabled', attempts: 0, ...values });
    return id;
  }
  async update(id, values) {
    if (['id','tool','action','callback','created','schema_version'].some(k => Object.hasOwn(values, k))) throw new Error('Immutable job fields cannot be overwritten');
    const file = this.path(id);
    await write(file, { ...await load(file), ...values });
  }
  async complete(id, outcome, values = {}) {
    if (!['success','failure','cancelled'].includes(outcome)) throw new Error('Invalid terminal outcome');
    if (['state','outcome','finished','notification'].some(k => Object.hasOwn(values, k))) throw new Error('Reserved completion fields');
    if ((await load(this.path(id))).state === 'finished') return this.status(id);
    await this.update(id, { ...values, state: 'finished', outcome, finished: now() });
    return this.status(id);
  }
  async status(id) {
    const { callback, command, ...visible } = await load(this.path(id));
    return visible;
  }
  async notify(data) {
    this.endpoint(data.callback.url, data.callback.secret);
    const headers = { 'Content-Type': 'application/json', 'X-GitHub-Delivery': data.id };
    if (data.callback.secret) headers['X-Hub-Signature-256'] = 'sha256=' + createHmac('sha256', data.callback.secret).update('{}').digest('hex');
    const response = await fetch(data.callback.url, { method: 'POST', headers, body: '{}', redirect: 'manual', signal: AbortSignal.timeout(15000) });
    await response.body?.cancel();
    if (response.status < 200 || response.status >= 300) throw new Error('Callback rejected');
  }
  async deliver(file, data) {
    this.policy.allow(data.tool, data.action);
    if (data.callback.kind === 'herdr') {
      this.policy.route('herdr');
      await this.herdr.ready(data.callback);
      data.notification = 'sending';
      await write(file, data);
      await this.herdr.send(data.callback);
    } else await this.notify(data);
    data.notification = 'delivered'; data.delivered = now();
    delete data.callback; delete data.callback_error;
    await write(file, data);
  }
  async processDelivery(file, data) {
    if (data.state !== 'finished' || ['disabled','delivered','uncertain','abandoned'].includes(data.notification) || now() < (data.retry_after ?? 0)) return;
    try { await this.deliver(file, data); }
    catch (error) {
      if (error instanceof TargetChanged) { data.notification = 'abandoned'; data.callback_error = 'HerdrTargetChanged'; }
      else {
        data.attempts++;
        data.callback_error = error.constructor.name;
        data.retry_after = now() + Math.min(300, 15 * data.attempts);
        if (data.notification === 'sending') data.notification = 'uncertain';
      }
      await write(file, data);
    }
  }
  async jobs() {
    const dir = path.join(this.root, 'jobs');
    try { return (await readdir(dir)).filter(n => /^[0-9a-f]{32}\.json$/.test(n)).map(n => path.join(dir, n)); }
    catch (e) { if (e.code === 'ENOENT') return []; throw e; }
  }
  async recoverDeliveries() {
    for (const file of await this.jobs()) {
      const data = await load(file);
      if (data.notification === 'sending') {
        data.notification = 'uncertain'; data.callback_error = 'worker_interrupted_during_delivery';
        await write(file, data);
      }
    }
  }
  async deliverPending() {
    for (const file of await this.jobs()) await this.processDelivery(file, await load(file));
  }
}
