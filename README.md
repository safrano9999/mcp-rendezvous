# mcp-rendezvous

Local preview **0.1.0**. Python and Node.js libraries for persistent completion
feedback from selected MCP tools. Neither package has been published.
An optional REST service provides the same feedback flow to applications that
do not use MCP or either SDK.

The caller receives an accepted job immediately and ends its turn. A background
worker later sends a completion-only webhook (`POST {}`) or a fixed Herdr prompt
(`finished` plus one real Enter), for success, failure or cancellation. Results
and logs are fetched separately after notification or an explicit user request.

## Packages

- Python: `mcp-rendezvous`, import `mcp_rendezvous`, Python 3.11+.
- Node.js: `mcp-rendezvous`, ESM, Node 20+.
- Operator policy: `mcp_rendezvous/schema.json`, also included in the npm package.
- Herdr delivery requires Linux, a local Herdr 0.8+ instance under the service
  user and an explicit live agent name or pane ID. Webhook delivery is portable.

Install the local Python checkout with `python -m pip install -e .`. For Node,
run `npm install /absolute/path/to/mcp-rendezvous/node` in the consuming project.
The package names are provisional until registry ownership is checked at release.

## Operator policy

`examples/safrano.json` shows the allowlist. The server loads the JSON file; MCP
callers cannot replace it. Only listed tool/action pairs may create jobs. Herdr
and webhook are built-in adapters. The Herdr executable is operator-configured,
but its arguments are restricted to `agent prompt {target} finished`; only the
validated target can vary. Commands never go through a shell. Arbitrary command,
prompt or key-sequence inputs are not supported.

Optional `webhook.allowed_hosts` restricts destination hostnames, including
`*.example.test`. Without that property any valid HTTP(S) endpoint is permitted.
Host matching is not DNS/IP isolation; use network policy for that requirement.
Redirects are never followed. Secrets belong in private runtime configuration,
not the policy or source control.

## Python integration

```python
from mcp_rendezvous import Rendezvous, completion_next

rv = Rendezvous("feedback.json", "/persistent/my-mcp/feedback")

# In the selected tool, after validating the operation:
destination = rv.resolve(True, herdr_target="w1:p5")
job_id = rv.queue("build_images", "run", destination, build_id="example")
# Dispatch/register the operation with the application's persistent worker.
response = {"feedback_id": job_id, "next": completion_next(destination)}

# In that worker, once the operation has actually ended:
rv.complete(job_id, "success")  # also accepts failure or cancelled
rv.deliver_pending()
```

## Node.js integration

```javascript
import { Rendezvous, completionNext } from 'mcp-rendezvous';
const rv = new Rendezvous('feedback.json', '/persistent/my-mcp/feedback');
const destination = await rv.resolve(true, '', '', 'w1:p5');
const jobId = await rv.queue('build_images', 'run', destination, { build_id: 'example' });
// Dispatch/register the operation with the application's persistent worker.
const response = { feedback_id: jobId, next: completionNext(destination) };

// Later, in the worker:
await rv.complete(jobId, 'success');
await rv.deliverPending();
```

`resolve(enabled, url, secret, herdr_target)` has the same positional arguments
in both SDKs. A per-call destination opts in; `false` suppresses feedback. A
saved default is used with `true`. `configure('set', url, secret)` or
`configure('set', '', '', target)` replaces that instance's shared default;
per-call destinations leave it unchanged. Python also supports keyword arguments.

## Worker contract and persistence

The libraries do **not** detach a thread from a short-lived MCP call and pretend
it survives process exit. Run a persistent, supervised application worker.
That worker owns execution/completion detection, calls `heartbeat()` regularly
(at least once per 120 seconds), and drains finished notifications. Queueing
fails before dispatch if its heartbeat is missing or stale. Keep only **one
worker per state directory**, with application-level singleton supervision/lock.
Do not run both SDKs concurrently against the same directory.

At worker startup call `recover_deliveries()` / `recoverDeliveries()`. The
application must reconcile its interrupted operations separately and mark
them failed or cancelled when appropriate. Call `complete` only at terminal
completion; it is idempotent. Safrano's existing systemd worker handles its
GitHub cascade monitoring and interrupted smart1 pulls.

The shared state format is atomic JSON, private files (0600) in a persistent
directory (0700). Keep operation metadata nonsecret: `status()` hides callback
destinations and the legacy `command` field, but returns other operation fields.
Errors expose class names only, never callback secrets. Do not grant MCP callers
direct write access to the policy, state directory or registered handlers.

Webhook delivery retries with the same `X-GitHub-Delivery` ID. Optional HMAC uses
`X-Hub-Signature-256`. Receivers should deduplicate this at-least-once transport.
Herdr binds the original pane/terminal/agent/process, waits through approval
screens, and abandons replaced agents. Ambiguous submission or a crash during
submission becomes `uncertain`, with no blind second Enter. Herdr input
acceptance does not prove the client processed the turn; checking identity and
sending input are separate Herdr API calls.

## REST API for applications without MCP

The Python package includes `mcp-rendezvous-rest`. This optional service owns a
delivery worker and private state directory. The producing application performs
its operation and reports completion through REST; it does not need an MCP
connection or client-side polling. Use a separate state directory from an
existing MCP worker (for example Safrano's); ownership is locked exclusively.

```sh
mcp-rendezvous-rest --config feedback.json \
  --state-dir /persistent/rendezvous-rest \
  --token-file /private/rendezvous-bearer
```

The default listener is `127.0.0.1:8768`. The bearer file must be private (0600),
with at least 32 random ASCII characters. All routes require
`Authorization: Bearer <token>`; rotation is picked up on the next request.
For access outside the local machine, use your authenticated TLS reverse proxy
or private encrypted network. The server never executes submitted commands.

| Request | Purpose |
| --- | --- |
| `POST /v1/jobs` | Register an allowed tool/action and feedback destination; returns `202` immediately. |
| `POST /v1/jobs/{feedback_id}/complete` | Record `success`, `failure` or `cancelled`; delivery happens in the background. |
| `GET /v1/jobs/{feedback_id}` | Read status after feedback, with callback secrets omitted. |
| `GET /health` | Service health. |
| `GET /openapi.json` | OpenAPI 3.1 document. |

Registration example (choose `herdr_target` **or** `callback_url`):

```json
{
  "tool": "build_images",
  "action": "run",
  "callback_url": "http://receiver.example/finished",
  "metadata": {"run_id": "external-123"}
}
```

The producer later posts `{"outcome":"success"}` to the completion route.
Repeating the same outcome is idempotent; a different recorded outcome returns
409. Pending external operations survive REST-service restarts. The API does
not infer their outcome or build/pull anything itself. The same JSON policy
controls allowed names even when those names represent REST operations rather
than MCP tools. Request bodies are limited to 64 KiB.

## Development and local artifacts

```sh
npm --prefix node ci
python -m unittest discover -s tests -v
npm --prefix node test
python -m pip wheel --no-deps . --wheel-dir dist
npm pack ./node --pack-destination dist
```

Both SDKs consume the shared contract fixtures in `tests/fixtures`. Tests also
exercise real local HTTP delivery, Herdr command construction with a harmless
fake executable, interruption handling and cross-language state compatibility.
Before publication, confirm package names, author/license metadata and registry
accounts; credentials are never needed for development, tests or local builds.
