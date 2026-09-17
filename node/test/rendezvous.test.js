import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, mkdtemp, rm, copyFile, chmod, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from 'node:http';
import { createHmac } from 'node:crypto';
import { Rendezvous, Policy, completionNext, write } from '../src/index.js';

const root = fileURLToPath(new URL('../../', import.meta.url));
const contract = JSON.parse(await readFile(path.join(root, 'tests/fixtures/contract.json')));
async function setup(t) {
  const dir = await mkdtemp(path.join(tmpdir(), 'rendezvous-node-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const config = structuredClone(contract.base);
  const rv = new Rendezvous(config, path.join(dir, 'state'));
  await rv.heartbeat();
  return { dir, config, rv };
}

test('shared policy contract and identical packaged schema', async () => {
  for (const entry of contract.policies) {
    let config = structuredClone(contract.base);
    if (!entry.path.length) config = entry.value;
    else {
      let item = config;
      for (const key of entry.path.slice(0, -1)) item = item[key];
      item[entry.path.at(-1)] = entry.value;
    }
    if (entry.valid) assert.doesNotThrow(() => new Policy(config), entry.name);
    else assert.throws(() => new Policy(config), undefined, entry.name);
  }
  assert.equal(await readFile(path.join(root,'mcp_rendezvous/schema.json'),'utf8'),await readFile(path.join(root,'node/schema.json'),'utf8'));
});

test('shared endpoint contract and host allowlist', async t => {
  const { config, rv, dir } = await setup(t);
  for (const entry of contract.urls) {
    if (entry.valid) assert.doesNotThrow(() => rv.endpoint(entry.url), entry.url);
    else assert.throws(() => rv.endpoint(entry.url), undefined, entry.url);
  }
  assert.throws(() => rv.endpoint('https://example.test/', 'bad\nsecret'));
  config.feedback.routes.webhook.allowed_hosts = ['*.example.test','127.0.0.1'];
  const limited = new Rendezvous(config, dir);
  limited.endpoint('http://sub.example.test/hook');
  limited.endpoint('http://127.0.0.1/hook');
  for (const host of ['example.test','evilexample.test','example.test.evil']) assert.throws(() => limited.endpoint(`http://${host}/hook`));
});

test('operation allowlist and missing worker fail before queue', async t => {
  const { rv } = await setup(t);
  for (const [tool, action] of [['unknown','run'],['build_images','list'],['podman_smart1','remove']]) await assert.rejects(rv.queue(tool,action,null));
  await assert.rejects(rv.queue('build_images','run',null,{id:'override'}));
  await rm(path.join(rv.root,'heartbeat'));
  await assert.rejects(rv.queue('build_images','run',null),/not running/);
});

test('opt-in and saved secrets stay private across instances', async t => {
  const { rv, config } = await setup(t);
  const result = await rv.configure('set','http://example.test/hook','private');
  assert.ok(!JSON.stringify(result).includes('private'));
  assert.equal(await rv.resolve(), null);
  assert.equal(await rv.resolve(false,'http://example.test/hook'), null);
  assert.equal((await rv.resolve(true)).secret,'private');
  assert.equal((await rv.resolve(null,'http://example.test/hook')).secret,'private');
  assert.equal((await rv.resolve(null,'http://other.test/hook')).secret,'');
  const id = await rv.queue('build_images','run',await rv.resolve(true),{command:['private-command']});
  const restored = new Rendezvous(config,rv.root);
  const status = await restored.status(id);
  assert.ok(!('callback' in status)); assert.ok(!('command' in status));
  assert.equal((await stat(rv.path(id))).mode & 0o777,0o600);
  assert.equal((await stat(path.join(rv.root,'default.json'))).mode & 0o777,0o600);
  assert.match(completionNext(await rv.resolve(true)),/Do not poll, sleep/);
  assert.match(completionNext(null),/No completion notification/);
  await assert.rejects(rv.update(id,{callback:{}}));
});

async function receiver(t) {
  const calls = [];
  const server = createServer(async (request,response) => {
    const chunks = [];
    for await (const data of request) chunks.push(data);
    calls.push({ path:request.url, body:Buffer.concat(chunks).toString(), headers:request.headers });
    response.writeHead(request.url === '/redirect' ? 302 : 202, request.url === '/redirect' ? {location:'/forbidden'} : {});
    response.end();
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  t.after(() => new Promise(resolve => {server.close(resolve);server.closeAllConnections();}));
  return {url:`http://127.0.0.1:${server.address().port}`,calls};
}

test('all outcomes send signed empty body once after completion', async t => {
  const {rv} = await setup(t), {url,calls} = await receiver(t);
  for (const outcome of ['success','failure','cancelled']) {
    const id = await rv.queue('build_images','run',await rv.resolve(null,url+'/hook','secret'));
    await rv.deliverPending();
    const before=calls.length;
    await rv.complete(id,outcome);await rv.deliverPending();
    await rv.complete(id,'success');await rv.deliverPending();
    assert.equal(calls.length,before+1);
    assert.equal((await rv.status(id)).outcome,outcome);
    assert.equal(calls.at(-1).body,'{}');
    assert.equal(calls.at(-1).headers['x-github-delivery'],id);
    assert.equal(calls.at(-1).headers['x-hub-signature-256'],'sha256='+createHmac('sha256','secret').update('{}').digest('hex'));
  }
  const id=await rv.queue('build_images','run',null);
  await rv.complete(id,'success');await rv.deliverPending();
  assert.equal(calls.length,3);
});

test('redirect is never followed; retries preserve delivery ID', async t => {
  const {rv}=await setup(t),{url,calls}=await receiver(t);
  const id=await rv.queue('build_images','run',await rv.resolve(null,url+'/redirect'));
  await rv.complete(id,'failure');await rv.deliverPending();
  assert.equal((await rv.status(id)).notification,'pending');
  await rv.update(id,{retry_after:0});await rv.deliverPending();
  assert.deepEqual(calls.map(c=>c.path),['/redirect','/redirect']);
  assert.deepEqual(calls.map(c=>c.headers['x-github-delivery']),[id,id]);
});

async function fake(t) {
  const {rv,config,dir}=await setup(t);
  const file=path.join(dir,'herdr-fake');
  await copyFile(path.join(root,'tests/fixtures/fake_herdr.py'),file);await chmod(file,0o700);
  const state={agent:{pane_id:'w1:p5',terminal_id:'original',agent:'codex',agent_status:'idle'},pid:process.pid};
  await write(file+'.state.json',state);
  config.feedback.routes.herdr.executable=file;
  return {rv:new Rendezvous(config,rv.root),file,state,config};
}
async function prompts(file) {
  return (await readFile(file+'.calls.jsonl','utf8')).trim().split('\n').map(line=>JSON.parse(line)).filter(args=>args[0]==='agent'&&args[1]==='prompt');
}

test('Herdr uses fixed argv and rejects replaced agents', async t => {
  const {rv,file,state}=await fake(t);
  const destination=await rv.resolve(null,'','','w1:p5');
  const id=await rv.queue('build_images','run',destination);
  await rv.complete(id,'failure');await rv.deliverPending();
  assert.deepEqual(await prompts(file),[['agent','prompt','w1:p5','finished']]);
  assert.equal((await rv.status(id)).notification,'delivered');
  const other=await rv.queue('build_images','run',destination);
  state.agent.terminal_id='replacement';await write(file+'.state.json',state);
  await rv.complete(other,'cancelled');await rv.deliverPending();
  assert.equal((await rv.status(other)).notification,'abandoned');
  assert.equal((await prompts(file)).length,1);
});

test('blocked agents are deferred, ambiguous delivery is not retried after restart', async t => {
  const {rv,file,state,config}=await fake(t);
  const destination=await rv.resolve(null,'','','w1:p5');
  const id=await rv.queue('build_images','run',destination);
  state.agent.agent_status='blocked';await write(file+'.state.json',state);
  await rv.complete(id,'success');await rv.deliverPending();
  assert.equal((await rv.status(id)).notification,'pending');
  assert.equal((await prompts(file)).length,0);
  state.agent.agent_status='idle';state.uncertain=true;await write(file+'.state.json',state);
  await rv.update(id,{retry_after:0});await rv.deliverPending();await rv.deliverPending();
  assert.equal((await rv.status(id)).notification,'uncertain');
  await rv.update(id,{notification:'sending'});
  const restored=new Rendezvous(config,rv.root);
  await restored.recoverDeliveries();await restored.deliverPending();
  assert.equal((await restored.status(id)).notification,'uncertain');
  assert.equal((await prompts(file)).length,1);
});
