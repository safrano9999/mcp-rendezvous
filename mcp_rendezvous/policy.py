import copy
import json
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit
from jsonschema import Draft7Validator


class Policy:
    def __init__(self, config):
        if isinstance(config, (str, Path)):
            config = json.loads(Path(config).read_text())
        schema = json.loads(files('mcp_rendezvous').joinpath('schema.json').read_text())
        if not Draft7Validator(schema).is_valid(config):
            raise ValueError('Invalid mcp-rendezvous operator policy')
        self.config = copy.deepcopy(config)
        self.routes = self.config['feedback']['routes']

    def allow(self, tool, action):
        if action not in self.config['tools'].get(tool, {}).get('actions', []):
            raise ValueError('Tool/action is not permitted by the feedback policy')

    def route(self, name):
        if name not in self.routes: raise ValueError('Feedback route is not permitted')
        return self.routes[name]

    def endpoint(self, url, secret=''):
        route = self.route('webhook')
        if not isinstance(url, str) or not isinstance(secret, str):
            raise ValueError('Callback URL and secret must be strings')
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in {'http', 'https'} and parsed.hostname and
                     parsed.username is None and parsed.password is None and '#' not in url and
                     parsed.port != 0 and not any(ord(c) < 33 or ord(c) == 127 for c in url) and
                     '\\' not in url and not any(c in secret for c in '\r\n') and
                     0 < len(url) <= 4096 and len(secret) <= 4096)
        except ValueError:
            valid = False
        if not valid: raise ValueError('Invalid callback URL or secret')
        hosts = route.get('allowed_hosts')
        host = parsed.hostname.lower()
        if hosts and not any(host == allowed or (allowed.startswith('*.') and host.endswith(allowed[1:])) for allowed in hosts):
            raise ValueError('Callback hostname is not permitted')
        return {'url': url, 'secret': secret}
