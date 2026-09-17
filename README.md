# mcp-rendezvous

[![PyPI](https://img.shields.io/pypi/v/mcp-rendezvous)](https://pypi.org/project/mcp-rendezvous/)
[![npm](https://img.shields.io/npm/v/mcp-rendezvous)](https://www.npmjs.com/package/mcp-rendezvous)

Version **0.1.0**. Python and Node.js libraries for persistent completion
feedback from selected MCP tools.
An optional REST service provides the same feedback flow to applications that
do not use MCP or either SDK.

The caller receives an accepted job immediately and ends its turn. A background
worker later sends a completion-only webhook (`POST {}`) or a fixed Herdr prompt
(`finished` plus one real Enter), for success, failure or cancellation. Results
and logs are fetched separately after notification or an explicit user request.

**[Python / PyPI](https://pypi.org/project/mcp-rendezvous/)** ·
**[Node.js / npm](https://www.npmjs.com/package/mcp-rendezvous)** ·
**[REST API](#rest-api-for-applications-without-mcp)** ·
**[OpenAPI specification](mcp_rendezvous/openapi.json)** ·
**[Issues](https://github.com/safrano9999/mcp-rendezvous/issues)**

## How it works

```mermaid
sequenceDiagram
    participant Client
    participant App as MCP server or REST application
    participant Worker as Persistent worker
    Client->>App: Start operation with optional feedback destination
    App->>Worker: Register job and start operation
    App-->>Client: Accepted, feedback_id, end this turn
    Note over Client: Free to do other work; no status polling
    Worker->>Worker: Observe success, failure or cancellation
    Worker-->>Client: Webhook POST {} or Herdr finished + Enter
    Client->>App: Fetch status and logs when needed
```

The MCP connection may use stdio or HTTP. Completion feedback travels through
the selected webhook or Herdr adapter; it does not require keeping the original
MCP request open. Applications without MCP can use the same flow through REST.
The receiving client decides how a notification resumes its work.

## Packages

| Interface | Package / documentation | Requirements |
| --- | --- | --- |
| Python SDK | [mcp-rendezvous on PyPI](https://pypi.org/project/mcp-rendezvous/), import `mcp_rendezvous` | Python 3.11+ |
| Node.js SDK | [mcp-rendezvous on npm](https://www.npmjs.com/package/mcp-rendezvous), [SDK README](node/README.md) | Node.js 20+, ESM |
| REST service | `mcp-rendezvous-rest`, included in the Python package; [OpenAPI 3.1](mcp_rendezvous/openapi.json) | Python 3.11+, POSIX file locking |
| Operator policy | [JSON Schema](mcp_rendezvous/schema.json), also exported as `mcp-rendezvous/schema.json` in npm | Shared by all three interfaces |

Herdr delivery requires Linux, a local Herdr 0.8+ instance under the service
user and an explicit live agent name or pane ID. Webhook delivery is portable.

Install the Python package from PyPI:

```sh
python -m pip install mcp-rendezvous
```

Install the Node.js package from npm:

```sh
npm install mcp-rendezvous
```

The REST command is installed with the Python package:

```sh
mcp-rendezvous-rest --help
```

For local development, use `python -m pip install -e .` or
`npm install /absolute/path/to/mcp-rendezvous/node` in the consuming project.

## Operator policy

Start with [the webhook-only example](examples/webhook.json), saved as
`feedback.json`. [The Safrano example](examples/safrano.json) allows build/pull
feedback through both webhook and Herdr; set its absolute Herdr executable path
to match your installation.

The server loads the JSON file; MCP
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

# At supervised worker startup; refresh the heartbeat regularly while running.
rv.recover_deliveries()
rv.heartbeat()

# In the selected tool, after validating the operation:
destination = rv.resolve(True, url="https://receiver.example/finished")
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

// At supervised worker startup; refresh the heartbeat regularly while running.
await rv.recoverDeliveries();
await rv.heartbeat();

const destination = await rv.resolve(true, 'https://receiver.example/finished');
const jobId = await rv.queue('build_images', 'run', destination, { build_id: 'example' });
// Dispatch/register the operation with the application's persistent worker.
const response = { feedback_id: jobId, next: completionNext(destination) };

// Later, in the worker:
await rv.complete(jobId, 'success');
await rv.deliverPending();
```

Replace the example URL with your receiver. These snippets show the application
and worker lifecycle together; the application's supervised worker runs the
operation after the tool has returned its accepted response. To use Herdr with
the corresponding policy, resolve `herdr_target="w1:p5"` in Python or
`await rv.resolve(true, '', '', 'w1:p5')` in Node.js, using your actual agent or
pane target.

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

The complete request and response contract is available in the
[OpenAPI specification](mcp_rendezvous/openapi.json) and from the running
service's authenticated `/openapi.json` endpoint.

## Safrano MCP integration

[SAFRANO_MCP](https://github.com/safrano9999/SAFRANO_MCP) (private repository;
access required) uses this library for
GitHub build and smart1 pull completion feedback. Its application worker owns
the build monitoring and pull queue. In automatic mode, successful build stages
can start their pulls while later stages continue building. A terminal failure
can notify immediately; success is reported after all selected builds and pulls
finish. The library supplies the persistent notification mechanism, while the
MCP server defines when that operation is finished.

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
Credentials are never needed for development, tests or local builds.
