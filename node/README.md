# mcp-rendezvous

Local preview 0.1.0, not yet published. Node.js ESM completion feedback library.
Supports persistent webhook `POST {}` and Herdr `finished` + real Enter.

```js
import { Rendezvous, completionNext } from 'mcp-rendezvous';
const rv = new Rendezvous('feedback.json', '/persistent/my-mcp/feedback');
const destination = await rv.resolve(true, 'http://receiver.example/hook');
const id = await rv.queue('my_tool', 'run', destination);
const accepted = { feedback_id: id, next: completionNext(destination) };
// Return accepted immediately; the client must end its turn without polling.
// The application worker later calls:
await rv.complete(id, 'success'); // or failure / cancelled
await rv.deliverPending();
```

Load the operator allowlist using the included `mcp-rendezvous/schema.json`.
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
