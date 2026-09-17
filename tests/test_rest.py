import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from mcp_rendezvous import Rendezvous
from mcp_rendezvous.core import write
from mcp_rendezvous.rest import FeedbackHTTPServer, CREATE_SCHEMA, COMPLETE_SCHEMA

ROOT=Path(__file__).resolve().parents[1]
CONFIG=json.loads((ROOT/'examples/safrano.json').read_text())


class RESTTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.token=self.root/'token';self.token.write_text('t'*48);self.token.chmod(0o600)
        self.rv=Rendezvous(copy.deepcopy(CONFIG),self.root/'state')
        self.server=FeedbackHTTPServer(('127.0.0.1',0),self.rv,self.token)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.close)

    def close(self):
        if self.server:
            self.server.shutdown();self.server.server_close();self.server=None

    def request(self,method,path,data=None,token='t'*48):
        headers={'Content-Type':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        req=urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}'+path,
            data=json.dumps(data).encode() if data is not None else None,headers=headers,method=method)
        try:
            with urllib.request.urlopen(req,timeout=5) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as error:
            with error:return error.code,json.load(error)

    def test_auth_rotation_and_openapi_match_runtime_schemas(self):
        self.assertEqual(self.request('GET','/health',token='')[0],401)
        self.assertEqual(self.request('GET','/health')[0],200)
        status,spec=self.request('GET','/openapi.json')
        self.assertEqual(status,200)
        self.assertEqual(spec['components']['schemas']['CreateJob'],CREATE_SCHEMA)
        self.assertEqual(spec['components']['schemas']['CompleteJob'],COMPLETE_SCHEMA)
        self.token.write_text('n'*48)
        self.assertEqual(self.request('GET','/health')[0],401)
        self.assertEqual(self.request('GET','/health',token='n'*48)[0],200)
        self.token.chmod(0o644)
        self.assertEqual(self.request('GET','/health',token='n'*48)[0],503)

    def test_allowed_operations_only_and_no_arbitrary_commands(self):
        for data in [
            {'tool':'build_images','action':'list'},
            {'tool':'unknown','action':'run'},
            {'tool':'build_images','action':'run','command':['id']},
            {'tool':'build_images','action':'run','feedback':'true'},
        ]:
            with self.subTest(data=data):self.assertEqual(self.request('POST','/v1/jobs',data)[0],400)
        self.assertFalse((self.rv.root/'jobs').exists())

    def test_register_complete_status_and_conflict_without_mcp(self):
        status,data=self.request('POST','/v1/jobs',{'tool':'build_images','action':'run','feedback':False,'metadata':{'run_id':'external-123'}})
        self.assertEqual(status,202)
        self.assertIn('End this turn immediately',data['next'])
        identifier=data['feedback_id']
        self.assertEqual(self.request('GET','/v1/jobs/'+identifier)[1]['state'],'pending')
        status,done=self.request('POST','/v1/jobs/'+identifier+'/complete',{'outcome':'cancelled'})
        self.assertEqual(status,200);self.assertEqual(done['outcome'],'cancelled')
        self.assertEqual(done['metadata'],{'run_id':'external-123'})
        self.assertNotIn('callback',done)
        self.assertEqual(self.request('POST','/v1/jobs/'+identifier+'/complete',{'outcome':'cancelled'})[0],200)
        self.assertEqual(self.request('POST','/v1/jobs/'+identifier+'/complete',{'outcome':'success'})[0],409)
        self.assertEqual(self.request('GET','/v1/jobs/'+'a'*32)[0],404)
        self.assertEqual(self.request('GET','/v1/jobs/../../token')[0],404)

    def test_real_rest_completion_triggers_empty_webhook_without_client_polling(self):
        received=threading.Event();calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                calls.append(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(202);self.end_headers();received.set()
        receiver=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=receiver.serve_forever,daemon=True).start()
        self.addCleanup(receiver.server_close);self.addCleanup(receiver.shutdown)
        status,job=self.request('POST','/v1/jobs',{'tool':'build_images','action':'run','callback_url':f'http://127.0.0.1:{receiver.server_port}/hook','callback_secret':'private'})
        self.assertEqual(status,202);self.assertNotIn('private',json.dumps(job))
        self.assertFalse(received.is_set())
        self.assertEqual(self.request('POST','/v1/jobs/'+job['feedback_id']+'/complete',{'outcome':'failure'})[0],200)
        self.assertTrue(received.wait(5),'REST completion did not produce callback')
        self.assertEqual(calls,[b'{}'])

    def test_external_pending_job_survives_service_restart_and_worker_is_exclusive(self):
        _,job=self.request('POST','/v1/jobs',{'tool':'podman_smart1','action':'pull','feedback':False})
        with self.assertRaises(BlockingIOError):FeedbackHTTPServer(('127.0.0.1',0),self.rv,self.token)
        self.close()
        self.server=FeedbackHTTPServer(('127.0.0.1',0),Rendezvous(CONFIG,self.rv.root),self.token)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.assertEqual(self.request('GET','/v1/jobs/'+job['feedback_id'])[1]['state'],'pending')
        self.assertEqual(self.request('POST','/v1/jobs/'+job['feedback_id']+'/complete',{'outcome':'success'})[0],200)


if __name__=='__main__':unittest.main()
