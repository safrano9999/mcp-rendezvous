"""Optional REST binding for non-MCP producers; never executes their operations."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

from jsonschema import Draft7Validator
from .core import Rendezvous, completion_next

CREATE_SCHEMA = {
    'type':'object', 'additionalProperties':False, 'required':['tool','action'],
    'properties': {
        'tool':{'type':'string','minLength':1,'maxLength':64},
        'action':{'type':'string','minLength':1,'maxLength':64},
        'feedback':{'type':['boolean','null']},
        'callback_url':{'type':'string','maxLength':4096},
        'callback_secret':{'type':'string','maxLength':4096},
        'herdr_target':{'type':'string','maxLength':128},
        'metadata':{'type':'object'},
    },
}
COMPLETE_SCHEMA = {
    'type':'object','additionalProperties':False,'required':['outcome'],
    'properties':{'outcome':{'enum':['success','failure','cancelled']}},
}


class APIError(Exception):
    def __init__(self, status, code): self.status, self.code = status, code


class FeedbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, rendezvous, token_file):
        self.rv = rendezvous
        self.token_file = Path(token_file)
        self.read_token()
        self.rv.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.worker_lock = open(self.rv.root/'worker.lock','a')
        try:
            fcntl.flock(self.worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            super().__init__(address, Handler)
        except Exception:
            self.worker_lock.close()
            raise
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.lock_guard = threading.Lock()
        self.job_locks = {}
        self.rv.recover_deliveries()
        self.rv.heartbeat()
        self.notifier = threading.Thread(target=self.deliver_loop, daemon=True)
        self.notifier.start()

    def read_token(self):
        if self.token_file.stat().st_mode & 0o077:
            raise ValueError('REST bearer file must be owner-only (0600)')
        token = self.token_file.read_text().strip()
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError('REST bearer must be at least 32 ASCII characters without whitespace')
        return token

    def job_lock(self, identifier):
        with self.lock_guard:
            return self.job_locks.setdefault(identifier, threading.Lock())

    def deliver_loop(self):
        while not self.stop_event.is_set():
            self.rv.heartbeat()
            for path in (self.rv.root/'jobs').glob('*.json'):
                if self.stop_event.is_set(): break
                try:
                    data = json.loads(path.read_text())
                    lock = self.job_lock(data['id'])
                    if not lock.acquire(blocking=False): continue
                    try: self.rv.process_delivery(path, json.loads(path.read_text()))
                    finally: lock.release()
                except Exception:
                    # One damaged job must not prevent other deliveries; no private data in logs.
                    continue
            self.wake_event.wait(1)
            self.wake_event.clear()

    def server_close(self):
        self.stop_event.set()
        self.wake_event.set()
        self.notifier.join(timeout=35)
        super().server_close()
        self.worker_lock.close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'mcp-rendezvous'
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args): pass

    def respond(self, status, value):
        body = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.end_headers()
        self.wfile.write(body)

    def authorize(self):
        try: token = self.server.read_token()
        except (OSError, ValueError): raise APIError(503,'authentication_unavailable') from None
        header = self.headers.get('Authorization','')
        if not secrets.compare_digest(header.encode(), ('Bearer '+token).encode()):
            raise APIError(401,'unauthorized')

    def body(self, schema):
        if self.headers.get('Transfer-Encoding'):
            raise APIError(400,'transfer_encoding_not_supported')
        if self.headers.get_content_type() != 'application/json': raise APIError(415,'json_required')
        try: length = int(self.headers.get('Content-Length','-1'))
        except ValueError: raise APIError(400,'invalid_content_length') from None
        if length < 0: raise APIError(411,'content_length_required')
        if length > 65536: raise APIError(413,'body_too_large')
        try:
            def reject_constant(_): raise ValueError()
            data = json.loads(self.rfile.read(length), parse_constant=reject_constant)
        except (ValueError, UnicodeError): raise APIError(400,'invalid_json') from None
        if not Draft7Validator(schema).is_valid(data): raise APIError(400,'invalid_request')
        return data

    def handle_api(self):
        self.authorize()
        if self.command == 'GET' and self.path == '/health':
            return 200, {'status':'ok','version':'0.1.0'}
        if self.command == 'GET' and self.path == '/openapi.json':
            return 200, json.loads(files('mcp_rendezvous').joinpath('openapi.json').read_text())
        rv = self.server.rv
        if self.command == 'POST' and self.path == '/v1/jobs':
            data = self.body(CREATE_SCHEMA)
            rv.policy.allow(data['tool'],data['action'])
            destination = rv.resolve(data.get('feedback'),data.get('callback_url',''),
                                     data.get('callback_secret',''),data.get('herdr_target',''))
            identifier = rv.queue(data['tool'],data['action'],destination,metadata=data.get('metadata',{}))
            return 202, {'feedback_id':identifier,'state':'pending','next':completion_next(destination)}
        match = re.fullmatch(r'/v1/jobs/([0-9a-f]{32})(/complete)?', self.path)
        if not match: raise APIError(404,'not_found')
        identifier, complete = match.groups()
        if self.command == 'GET' and not complete: return 200, rv.status(identifier)
        if self.command == 'POST' and complete:
            data = self.body(COMPLETE_SCHEMA)
            with self.server.job_lock(identifier):
                status = rv.status(identifier)
                if status['state'] == 'finished' and status.get('outcome') != data['outcome']:
                    raise APIError(409,'outcome_already_recorded')
                result = rv.complete(identifier,data['outcome'])
            self.server.wake_event.set()
            return 200, result
        raise APIError(405,'method_not_allowed')

    def route(self):
        try:
            status, value = self.handle_api()
        except APIError as error: status, value = error.status, {'error':error.code}
        except FileNotFoundError: status, value = 404, {'error':'not_found'}
        except (ValueError, TypeError): status, value = 400, {'error':'invalid_request'}
        except TimeoutError: status, value = 408, {'error':'request_timeout'}
        except Exception: status, value = 503, {'error':'feedback_unavailable'}
        self.respond(status,value)

    do_GET = route
    do_POST = route


def main():
    parser = argparse.ArgumentParser(description='mcp-rendezvous REST feedback service; no build/pull execution')
    parser.add_argument('--config', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=8768, type=int)
    args = parser.parse_args()
    server = FeedbackHTTPServer((args.host,args.port),Rendezvous(args.config,args.state_dir),args.token_file)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == '__main__': main()
