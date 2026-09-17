import copy
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from mcp_rendezvous import Rendezvous, Policy, completion_next, TargetChanged
from mcp_rendezvous.core import write

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT/'tests/fixtures/contract.json').read_text())


class RendezvousTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = copy.deepcopy(CONTRACT['base'])
        self.rv = Rendezvous(self.config, self.root/'state')
        self.rv.heartbeat()

    def test_shared_policy_contract(self):
        for case in CONTRACT['policies']:
            config = copy.deepcopy(CONTRACT['base'])
            if not case['path']: config = case['value']
            else:
                item = config
                for key in case['path'][:-1]: item = item[key]
                item[case['path'][-1]] = case['value']
            with self.subTest(case=case['name']):
                if case['valid']: Policy(config)
                else:
                    with self.assertRaises(ValueError): Policy(config)
        self.assertEqual((ROOT/'mcp_rendezvous/schema.json').read_bytes(), (ROOT/'node/schema.json').read_bytes())

    def test_shared_endpoint_contract(self):
        for case in CONTRACT['urls']:
            with self.subTest(url=case['url']):
                if case['valid']: self.rv.endpoint(case['url'])
                else:
                    with self.assertRaises(ValueError): self.rv.endpoint(case['url'])
        with self.assertRaises(ValueError): self.rv.endpoint('https://example.test/', 'bad\nsecret')

    def test_host_allowlist(self):
        self.config['feedback']['routes']['webhook']['allowed_hosts'] = ['*.example.test','127.0.0.1']
        rv = Rendezvous(self.config, self.root)
        rv.endpoint('http://sub.example.test/hook')
        rv.endpoint('http://127.0.0.1/hook')
        for host in ['example.test','evilexample.test','example.test.evil']:
            with self.assertRaises(ValueError): rv.endpoint('http://'+host+'/hook')

    def test_allowlist_no_worker_and_reserved_fields_fail_before_queue(self):
        for tool, action in [('unknown','run'),('build_images','list'),('podman_smart1','remove')]:
            with self.assertRaises(ValueError): self.rv.queue(tool,action,None)
        with self.assertRaises(ValueError): self.rv.queue('build_images','run',None,id='override')
        self.assertFalse((self.rv.root/'jobs').exists())
        (self.rv.root/'heartbeat').unlink()
        with self.assertRaisesRegex(RuntimeError,'not running'): self.rv.queue('build_images','run',None)

    def test_opt_in_secret_isolation_and_private_persistence(self):
        result = self.rv.configure('set','http://example.test/hook','private')
        self.assertNotIn('private',json.dumps(result))
        self.assertIsNone(self.rv.resolve())
        self.assertIsNone(self.rv.resolve(False,'http://example.test/hook'))
        self.assertEqual(self.rv.resolve(True)['secret'],'private')
        self.assertEqual(self.rv.resolve(None,'http://example.test/hook')['secret'],'private')
        self.assertEqual(self.rv.resolve(None,'http://different.test/hook')['secret'],'')
        identifier = self.rv.queue('build_images','run',self.rv.resolve(True),command=['private-command'])
        rv = Rendezvous(self.config,self.rv.root)
        self.assertNotIn('callback',rv.status(identifier))
        self.assertNotIn('command',rv.status(identifier))
        self.assertEqual(rv.path(identifier).stat().st_mode&0o777,0o600)
        self.assertEqual((rv.root/'default.json').stat().st_mode&0o777,0o600)
        self.assertIn('Do not poll, sleep',completion_next(rv.resolve(True)))
        self.assertIn('No completion notification',completion_next(None))
        with self.assertRaises(ValueError): rv.update(identifier,callback={})

    def receiver(self):
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length']))
                calls.append((self.path,body,{k.lower():v for k,v in self.headers.items()}))
                self.send_response(302 if self.path=='/redirect' else 202)
                if self.path=='/redirect': self.send_header('Location','/forbidden')
                self.end_headers()
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=http.serve_forever,daemon=True).start()
        self.addCleanup(http.server_close); self.addCleanup(http.shutdown)
        return f'http://127.0.0.1:{http.server_port}',calls

    def test_all_outcomes_deliver_signed_empty_body_and_complete_is_idempotent(self):
        url,calls=self.receiver()
        for outcome in ['success','failure','cancelled']:
            identifier=self.rv.queue('build_images','run',self.rv.resolve(None,url+'/hook','secret'))
            self.rv.deliver_pending()
            before=len(calls)
            self.rv.complete(identifier,outcome)
            self.rv.deliver_pending()
            self.rv.complete(identifier,'success')
            self.rv.deliver_pending()
            self.assertEqual(len(calls),before+1)
            self.assertEqual(self.rv.status(identifier)['outcome'],outcome)
            self.assertEqual(calls[-1][1],b'{}')
            self.assertEqual(calls[-1][2]['x-github-delivery'],identifier)
            self.assertEqual(calls[-1][2]['x-hub-signature-256'],'sha256='+hmac.new(b'secret',b'{}',hashlib.sha256).hexdigest())
        identifier=self.rv.queue('build_images','run',None)
        self.rv.complete(identifier,'success'); self.rv.deliver_pending()
        self.assertEqual(len(calls),3)

    def test_redirect_retries_keep_id_and_never_follow(self):
        url,calls=self.receiver()
        identifier=self.rv.queue('build_images','run',self.rv.resolve(None,url+'/redirect'))
        self.rv.complete(identifier,'failure'); self.rv.deliver_pending()
        self.assertEqual(self.rv.status(identifier)['notification'],'pending')
        self.rv.update(identifier,retry_after=0); self.rv.deliver_pending()
        self.assertEqual([c[0] for c in calls],['/redirect','/redirect'])
        self.assertEqual([c[2]['x-github-delivery'] for c in calls],[identifier,identifier])

    def fake(self):
        file=self.root/'herdr-fake'
        shutil.copyfile(ROOT/'tests/fixtures/fake_herdr.py',file);file.chmod(0o700)
        state={'agent':{'pane_id':'w1:p5','terminal_id':'original','agent':'codex','agent_status':'idle'},'pid':os.getpid()}
        write(Path(str(file)+'.state.json'),state)
        self.config['feedback']['routes']['herdr']['executable']=str(file)
        self.rv=Rendezvous(self.config,self.rv.root)
        return file,state

    def test_real_subprocess_fixed_herdr_command_and_replaced_agent(self):
        file,state=self.fake()
        destination=self.rv.resolve(None,herdr_target='w1:p5')
        identifier=self.rv.queue('build_images','run',destination)
        self.rv.complete(identifier,'failure');self.rv.deliver_pending()
        calls=[json.loads(line) for line in Path(str(file)+'.calls.jsonl').read_text().splitlines()]
        self.assertEqual([c for c in calls if c[:2]==['agent','prompt']],[['agent','prompt','w1:p5','finished']])
        self.assertEqual(self.rv.status(identifier)['notification'],'delivered')
        other=self.rv.queue('build_images','run',destination)
        state['agent']['terminal_id']='replacement';write(Path(str(file)+'.state.json'),state)
        self.rv.complete(other,'cancelled');self.rv.deliver_pending()
        self.assertEqual(self.rv.status(other)['notification'],'abandoned')

    def test_ambiguous_herdr_never_retries_after_restart(self):
        file,state=self.fake()
        destination=self.rv.resolve(None,herdr_target='w1:p5')
        state['uncertain']=True;write(Path(str(file)+'.state.json'),state)
        identifier=self.rv.queue('build_images','run',destination)
        self.rv.complete(identifier,'success');self.rv.deliver_pending();self.rv.deliver_pending()
        self.assertEqual(self.rv.status(identifier)['notification'],'uncertain')
        self.rv.update(identifier,notification='sending')
        rv=Rendezvous(self.config,self.rv.root);rv.recover_deliveries();rv.deliver_pending()
        self.assertEqual(rv.status(identifier)['notification'],'uncertain')
        calls=[json.loads(line) for line in Path(str(file)+'.calls.jsonl').read_text().splitlines()]
        self.assertEqual(len([c for c in calls if c[:2]==['agent','prompt']]),1)

    def test_cross_language_state_and_no_poll_instruction(self):
        config=self.root/'policy.json';write(config,self.config)
        first=self.rv.queue('build_images','run',None)
        second=self.rv.queue('podman_smart1','pull',None)
        self.rv.complete(second,'cancelled')
        script="""import {Rendezvous,completionNext} from './node/src/index.js';
const [config,root,first,second]=process.argv.slice(1);
const rv=new Rendezvous(config,root);
if((await rv.status(second)).outcome!=='cancelled')throw new Error('Python state mismatch');
await rv.complete(first,'failure');
console.log(completionNext(null));"""
        result=subprocess.run(['node','--input-type=module','-e',script,str(config),str(self.rv.root),first,second],cwd=ROOT,check=True,capture_output=True,text=True)
        self.assertEqual(self.rv.status(first)['outcome'],'failure')
        self.assertEqual(result.stdout.strip(),completion_next(None))


if __name__=='__main__': unittest.main()
