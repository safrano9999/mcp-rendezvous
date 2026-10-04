import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.request
import uuid
import threading
from weakref import WeakKeyDictionary, finalize

from .policy import Policy
from .herdr import Herdr, TargetChanged
from .activity import ConnectionActivity, GENERATED, stamp
from .table import ActivityTable

STOP_AFTER_DISPATCH = ('End this turn immediately and return control to the user. '
                       'Do not poll, sleep, wait for completion, or start a status loop. ')


def completion_next(destination):
    if not destination:
        return STOP_AFTER_DISPATCH + 'No completion notification was requested.'
    channel = 'Herdr (finished + Enter)' if destination.get('kind') == 'herdr' else 'webhook'
    return STOP_AFTER_DISPATCH + (f'You will be notified via {channel} when finished, regardless of outcome. '
                                 'Fetch status/logs only after that notification or an explicit user request.')


def write(path, data):
    path = Path(path)
    if (isinstance(data, dict) and data.get('schema_version') == 1
            and data.get('id') == path.stem and 'tool' in data and 'state' in data):
        if path.parent.name == 'jobs':
            return ActivityTable(path.parent.parent).publish(path, data, _atomic_write)
        previous = json.loads(path.read_text()) if path.exists() else None
        stamp(data, previous, time.time())
    _atomic_write(path, data)


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class Rendezvous:
    """One private state directory per MCP instance, with one delivery worker.

    The application owns operation execution and completion detection. Both SDKs
    share this on-disk format, but must not concurrently own the same instance.
    """
    def __init__(self, config, state_dir):
        self.policy = Policy(config)
        self.root = Path(state_dir)
        self.table = ActivityTable(self.root)
        self.table.recover()
        self.herdr = Herdr(self.policy.route('herdr')) if 'herdr' in self.policy.routes else None
        self._connections = WeakKeyDictionary()
        self._connections_lock = threading.RLock()

    def connection(self, session):
        with self._connections_lock:
            if session not in self._connections:
                activity = ConnectionActivity(self)
                self._connections[session] = activity
                finalize(session, activity.close)
            return self._connections[session]

    def disconnect(self, session):
        with self._connections_lock:
            activity = self._connections.pop(session, None)
            if activity is not None: activity.close()

    def endpoint(self, url, secret=''): return self.policy.endpoint(url, secret)

    def destination(self, url='', secret='', herdr_target=''):
        if herdr_target:
            if url or secret: raise ValueError('Choose herdr_target OR callback_url/callback_secret')
            self.policy.route('herdr')
            return self.herdr.bind(herdr_target)
        return self.endpoint(url, secret)

    def configure(self, action='get', url='', secret='', herdr_target=''):
        path = self.root / 'default.json'
        if action == 'set': write(path, self.destination(url, secret, herdr_target))
        elif action == 'clear': path.unlink(missing_ok=True)
        elif action != 'get': raise ValueError('Unknown feedback configuration action')
        value = json.loads(path.read_text()) if path.exists() else None
        return {'configured': value is not None, 'callback_url': value.get('url') if value else None,
                'herdr_target': value.get('identity', {}).get('pane_id') if value else None,
                'transport': ('herdr' if value.get('kind') == 'herdr' else 'webhook') if value else None,
                'signed': bool(value and value.get('secret')),
                'feedback_default': self.policy.config['feedback']['default']}

    def resolve(self, enabled=None, url='', secret='', herdr_target=''):
        if enabled is not None and not isinstance(enabled, bool): raise ValueError('feedback must be boolean')
        if enabled is None: enabled = bool(url or herdr_target or self.policy.config['feedback']['default'])
        if not enabled: return None
        path = self.root / 'default.json'
        saved = json.loads(path.read_text()) if path.exists() else None
        if url or herdr_target:
            if not herdr_target and not secret and saved and saved.get('url') == url: secret = saved['secret']
            return self.destination(url, secret, herdr_target)
        if secret: raise ValueError('callback_secret requires callback_url')
        if not saved: raise ValueError('Feedback requires herdr_target, callback_url or configure_feedback(action=set)')
        if saved.get('kind') == 'herdr':
            self.policy.route('herdr')
            if self.herdr.bind(saved['identity']['pane_id']) != saved:
                raise TargetChanged('Saved Herdr agent has changed; configure again')
            return saved
        return self.endpoint(**saved)

    def heartbeat(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / 'heartbeat').touch(mode=0o600)

    def queue(self, tool, action, destination, *, kind=None, session=None, **values):
        self.policy.allow(tool, action)
        forbidden = {'schema_version','id','tool','action','callback','created','notification','attempts'} | GENERATED
        if forbidden.intersection(values): raise ValueError('Reserved job fields cannot be overwritten')
        if values.get('state', 'pending') not in {'pending','dispatching','finished'}:
            raise ValueError('Invalid initial job state')
        if destination:
            self.policy.route('herdr' if destination.get('kind') == 'herdr' else 'webhook')
            if destination.get('kind') != 'herdr': self.endpoint(destination['url'], destination['secret'])
        heartbeat = self.root / 'heartbeat'
        if not heartbeat.exists() or time.time() - heartbeat.stat().st_mtime > 120:
            raise RuntimeError('Completion worker is not running; operation was not dispatched')
        identifier = uuid.uuid4().hex
        activity = self.connection(session) if session is not None else None
        if activity is not None: activity.track(identifier)
        try:
            write(self.path(identifier), {'schema_version':1, 'id':identifier, 'tool':tool, 'action':action,
                  'kind':kind or tool, 'callback':destination, 'created':time.time(), 'state':'pending',
                  'notification':'pending' if destination else 'disabled', 'attempts':0, **values})
        except Exception:
            if activity is not None: activity.untrack(identifier)
            raise
        return identifier

    def path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
            raise ValueError('Invalid feedback_id')
        return self.root / 'jobs' / (identifier + '.json')

    def update(self, identifier, **values):
        if ({'id','tool','action','callback','created','schema_version'} | GENERATED).intersection(values):
            raise ValueError('Immutable job fields cannot be overwritten')
        path = self.path(identifier)
        self.table.mutate(path, lambda data: data.update(values), _atomic_write)

    def complete(self, identifier, outcome, **values):
        if outcome not in {'success','failure','cancelled'}: raise ValueError('Invalid terminal outcome')
        if {'state','outcome','finished','notification'}.intersection(values): raise ValueError('Reserved completion fields')
        path = self.path(identifier)
        if ({'id','tool','action','callback','created','schema_version'} | GENERATED).intersection(values):
            raise ValueError('Immutable job fields cannot be overwritten')
        def update(data):
            if data['state'] != 'finished':
                data.update(**values, state='finished', outcome=outcome, finished=time.time())
        self.table.mutate(path, update, _atomic_write)
        return self.status(identifier)

    def status(self, identifier):
        data = json.loads(self.path(identifier).read_text())
        return {k:v for k,v in data.items() if k not in {'callback','command'}}

    def notify(self, data):
        self.endpoint(data['callback']['url'], data['callback']['secret'])
        body = b'{}'
        headers = {'Content-Type':'application/json', 'X-GitHub-Delivery':data['id']}
        secret = data['callback']['secret']
        if secret: headers['X-Hub-Signature-256'] = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(data['callback']['url'], data=body, headers=headers, method='POST')
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as response:
                if not 200 <= response.status < 300: raise RuntimeError('Callback rejected')
        except urllib.error.HTTPError as error:
            error.close()
            raise

    def deliver(self, path, data):
        self.policy.allow(data['tool'], data['action'])
        if data['state'] != 'finished': raise ValueError('Only finished jobs can notify')
        # Publish result + timeline to connection views BEFORE either transport.
        write(path, data)
        if data['callback'].get('kind') == 'herdr':
            self.policy.route('herdr')
            self.herdr.ready(data['callback'])
            data['notification'] = 'sending'
            write(path, data)
            self.herdr.send(data['callback'])
        else: self.notify(data)
        data.update(notification='delivered', delivered=time.time())
        data.pop('callback', None)
        data.pop('callback_error', None)
        write(path, data)

    def process_delivery(self, path, data):
        if data['state'] != 'finished' or data['notification'] in {'disabled','delivered','uncertain','abandoned'}: return
        if time.time() < data.get('retry_after', 0): return
        try: self.deliver(path, data)
        except TargetChanged:
            data.update(notification='abandoned', callback_error='HerdrTargetChanged')
            write(path, data)
        except Exception as error:
            data['attempts'] += 1
            data.update(callback_error=type(error).__name__, retry_after=time.time() + min(300, 15 * data['attempts']))
            if data['notification'] == 'sending': data['notification'] = 'uncertain'
            write(path, data)

    def recover_deliveries(self):
        for path in (self.root / 'jobs').glob('*.json'):
            data = json.loads(path.read_text())
            if data['notification'] == 'sending':
                data.update(notification='uncertain', callback_error='worker_interrupted_during_delivery')
                write(path, data)

    def deliver_pending(self):
        for path in (self.root / 'jobs').glob('*.json'):
            self.process_delivery(path, json.loads(path.read_text()))
