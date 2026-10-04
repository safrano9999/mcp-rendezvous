# mcp-rendezvous

Version **0.1.1**. Node.js ESM completion feedback library.

The SDK also exposes an ephemeral connection activity table. Pass the live MCP
session object as the fifth argument of `rv.queue(tool, action, destination,
values, session)`. Expose `await rv.connection(session).snapshot(since)` through a
read-only MCP tool. `since=0` returns the full table; reuse its `cursor` after the
next completion signal to read only changes. Child jobs linked by `parent_id`
are included. `rv.disconnect(session)` discards the table, not background jobs.

Status, errors and transition times are written before either completion signal.
Hooks remain `POST {}` or Herdr `finished` plus Enter. The table includes timing,
flags and step state; the application owns automatic chaining and sends no
intermediate signal for internal steps. See the root README for the shared
Python/Node connection contract and separate-worker behavior.
Supports persistent webhook `POST {}` and Herdr `finished` + real Enter.

[GitHub documentation](https://github.com/safrano9999/mcp-rendezvous#readme) ·
[npm package](https://www.npmjs.com/package/mcp-rendezvous) ·
[Python package](https://pypi.org/project/mcp-rendezvous/) ·
[REST API](https://github.com/safrano9999/mcp-rendezvous#rest-api-for-applications-without-mcp)

## Install

```sh
npm install mcp-rendezvous
```

Node.js 20+ is required. Use ESM (`.mjs` or `"type": "module"` in package.json).

## Integrate

Save the [webhook-only policy](https://github.com/safrano9999/mcp-rendezvous/blob/main/examples/webhook.json)
as `feedback.json` and replace the example callback URL with your receiver.
The application's supervised worker owns operation execution, completion
detection and notification delivery. The following shows its lifecycle and
the tool handler together:

```js
import { Rendezvous, completionNext } from 'mcp-rendezvous';
const rv = new Rendezvous('feedback.json', '/persistent/my-mcp/feedback');

// Worker startup; keep refreshing the heartbeat while it is running.
await rv.recoverDeliveries();
await rv.heartbeat();

// Tool handler: validate, register the job, then dispatch your operation.
const destination = await rv.resolve(true, 'http://receiver.example/hook');
const id = await rv.queue('build_images', 'run', destination);
const accepted = { feedback_id: id, next: completionNext(destination) };
// Return accepted immediately; the client must end its turn without polling.
// The application worker later calls:
await rv.complete(id, 'success'); // or failure / cancelled
await rv.deliverPending();
```

Load the operator allowlist using the included `mcp-rendezvous/schema.json`.
The [Herdr + webhook example](https://github.com/safrano9999/mcp-rendezvous/blob/main/examples/safrano.json)
shows both adapters. For Herdr, use
`await rv.resolve(true, '', '', 'w1:p5')` with your actual agent or pane target.
Only configured tool/action pairs and routes are permitted. Herdr arguments are
fixed, with a validated target; arbitrary commands and shell evaluation are not
supported. Policies and private callback secrets must not be client-writable.

Run one supervised persistent application worker per state directory. It must
call `heartbeat()` at least every 120 seconds, `recoverDeliveries()` at startup,
reconcile its own interrupted operations and drain finished notifications.
The library does not launch builds, pulls or detached job executors. Do not
share an instance directory concurrently between different workers/SDKs.

HTTP retries are at-least-once with a stable delivery ID and optional HMAC.
Herdr binds the original live agent; ambiguous delivery is not retried. Status
and logs stay separate from the completion signal. Node 20+ is required; Herdr
additionally requires Linux and a local Herdr 0.8+ instance.

The companion Python package includes the standalone REST service for producers
without MCP or a Node.js SDK. See the
[OpenAPI specification](https://github.com/safrano9999/mcp-rendezvous/blob/main/mcp_rendezvous/openapi.json)
and [full integration guide](https://github.com/safrano9999/mcp-rendezvous#readme).
