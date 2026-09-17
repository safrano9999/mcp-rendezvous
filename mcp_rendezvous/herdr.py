"""Fixed completion prompt for an explicitly selected local Herdr agent."""
import json
from pathlib import Path
import re
import subprocess


class TargetChanged(RuntimeError):
    """The original agent is gone; never send to its replacement or a shell."""


class NotReady(RuntimeError):
    """Retry a read-only check later, without touching an approval UI."""


class DeliveryUncertain(RuntimeError):
    """Input may have arrived; retrying could submit a second prompt."""


class Herdr:
    def __init__(self, route):
        self.executable = route['executable']
        self.args = tuple(route['args'])

    def read(self, *args):
        result = subprocess.run([str(self.executable), *args], capture_output=True, text=True, timeout=10)
        if result.returncode:
            try:
                code = json.loads(result.stderr)['error']['code']
            except (ValueError, KeyError, TypeError):
                code = None
            if code in {'agent_not_found', 'pane_not_found'}:
                raise TargetChanged('Original Herdr agent is no longer available')
            raise NotReady('Herdr is currently unavailable')
        return json.loads(result.stdout)['result']


    def identity(self, agent):
        process = self.read('pane', 'process-info', '--pane', agent['pane_id'])['process_info']
        pid = process.get('foreground_process_group_id')
        if not isinstance(pid, int) or pid <= 0 or pid == process.get('shell_pid'):
            raise TargetChanged('Herdr pane no longer has an agent in the foreground')
        try:
            # Field 22 is starttime. Include the boot identity to reject reused PIDs.
            start = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except (OSError, IndexError):
            raise TargetChanged('Original Herdr process is no longer available') from None
        return {key: agent.get(key) for key in ('pane_id', 'terminal_id', 'agent', 'agent_session')} | {
            'pid': pid, 'start': start, 'boot': boot,
        }


    def bind(self, target):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_:.-]{0,127}', target):
            raise ValueError('herdr_target must be an explicit pane ID or unique live agent name')
        agent = self.read('agent', 'get', target)['agent']
        if not agent.get('agent') or not agent.get('terminal_id'):
            raise TargetChanged('herdr_target must contain a live agent')
        return {'kind': 'herdr', 'identity': self.identity(agent)}


    def ready(self, destination):
        expected = destination['identity']
        agent = self.read('agent', 'get', expected['pane_id'])['agent']
        if self.identity(agent) != expected:
            raise TargetChanged('Original Herdr agent has changed')
        if agent.get('agent_status') not in {'idle', 'done', 'working'} or agent.get('launch_pending'):
            raise NotReady('Herdr agent is not accepting prompts')


    def send(self, destination):
        # No shell, caller-supplied command, text, key sequence, or focused-pane fallback.
        # Herdr 0.8+ submits literal text, then one correctly encoded Enter after 300 ms.
        try:
            result = subprocess.run(
                [str(self.executable), *[destination['identity']['pane_id'] if arg == '{target}' else arg for arg in self.args]],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode or json.loads(result.stdout)['result']['type'] != 'agent_prompted':
                raise DeliveryUncertain('Herdr did not confirm prompt submission')
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            raise DeliveryUncertain('Herdr prompt submission could not be confirmed') from None
