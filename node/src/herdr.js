import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile } from 'node:fs/promises';
import { isDeepStrictEqual } from 'node:util';

const exec = promisify(execFile);
export class TargetChanged extends Error {}
export class NotReady extends Error {}
export class DeliveryUncertain extends Error {}

export class Herdr {
  constructor(route) {
    this.executable = route.executable;
    this.args = [...route.args];
  }
  async read(...args) {
    let output;
    try {
      output = await exec(this.executable, args, { timeout: 10000, maxBuffer: 1024 * 1024, shell: false });
    } catch (error) {
      let code;
      try { code = JSON.parse(error.stderr).error.code; } catch {}
      if (['agent_not_found', 'pane_not_found'].includes(code)) throw new TargetChanged('Original Herdr agent is unavailable');
      throw new NotReady('Herdr is currently unavailable');
    }
    return JSON.parse(output.stdout).result;
  }
  async identity(agent) {
    const { process_info: process } = await this.read('pane', 'process-info', '--pane', agent.pane_id);
    const pid = process.foreground_process_group_id;
    if (!Number.isInteger(pid) || pid <= 0 || pid === process.shell_pid) throw new TargetChanged('Agent is no longer in the foreground');
    let start, boot;
    try {
      const stat = await readFile(`/proc/${pid}/stat`, 'utf8');
      start = stat.slice(stat.lastIndexOf(')') + 1).trim().split(/\s+/)[19];
      boot = (await readFile('/proc/sys/kernel/random/boot_id', 'utf8')).trim();
      if (!start) throw new Error();
    } catch { throw new TargetChanged('Original Herdr process is unavailable'); }
    return { pane_id: agent.pane_id, terminal_id: agent.terminal_id ?? null, agent: agent.agent ?? null,
      agent_session: agent.agent_session ?? null, pid, start, boot };
  }
  async bind(target) {
    if (typeof target !== 'string' || target.trim() !== target || !/^[A-Za-z0-9][A-Za-z0-9_:.-]{0,127}$/.test(target)) throw new Error('Invalid explicit Herdr target');
    const { agent } = await this.read('agent', 'get', target);
    if (!agent.agent || !agent.terminal_id) throw new TargetChanged('Target must contain a live agent');
    return { kind: 'herdr', identity: await this.identity(agent) };
  }
  async ready(destination) {
    const { agent } = await this.read('agent', 'get', destination.identity.pane_id);
    if (!isDeepStrictEqual(await this.identity(agent), destination.identity)) throw new TargetChanged('Original Herdr agent has changed');
    if (!['idle', 'done', 'working'].includes(agent.agent_status) || agent.launch_pending) throw new NotReady('Herdr is not accepting prompts');
  }
  async send(destination) {
    try {
      const args = this.args.map(arg => arg === '{target}' ? destination.identity.pane_id : arg);
      const result = await exec(this.executable, args, { timeout: 10000, maxBuffer: 1024 * 1024, shell: false });
      if (JSON.parse(result.stdout).result.type !== 'agent_prompted') throw new Error();
    } catch { throw new DeliveryUncertain('Herdr prompt submission could not be confirmed'); }
  }
}
