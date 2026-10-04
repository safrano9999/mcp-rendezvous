# mcp-rendezvous

[![PyPI](https://img.shields.io/pypi/v/mcp-rendezvous)](https://pypi.org/project/mcp-rendezvous/)
[![npm](https://img.shields.io/npm/v/mcp-rendezvous)](https://www.npmjs.com/package/mcp-rendezvous)

Version **0.1.1**. Python and Node.js libraries for persistent completion
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

## Connection activity table

Both SDKs include a read-only activity view for a **logical MCP
connection**. The MCP adapter supplies its session object; callers cannot select
another connection by ID. Register jobs with that object when queueing:

```python
# In an MCP tool; session is ctx.session, supplied by the MCP server.
job_id = rv.queue("build_images", "run", destination, session=ctx.session,
                  auto_pull=True)

# Expose this through a read-only MCP tool such as get_connection_activity(since=0).
result = rv.connection(ctx.session).snapshot(since=0)
# After the next finished signal, pass result["cursor"] as since.
```

```javascript
const jobId = await rv.queue('build_images', 'run', destination,
  { auto_pull: true }, session);
const result = await rv.connection(session).snapshot(0);
// Later: await rv.connection(session).snapshot(result.cursor)
```

Only connection membership and its view cursor live in memory. Python stores the
shared table in a fixed **`activity.sqlite3`** file inside the instance's state
directory. It survives disconnects and process restarts. SQLite transactions
serialize writers across threads/processes, including read/modify/write updates
to the same job. No additional runtime dependency is needed (`sqlite3` is part
of Python). A persistent server-wide cursor supports queries across restarts:

```python
result = rv.table.snapshot(since=0)  # All jobs in this server's shared table.
changes = rv.table.snapshot(since=result["cursor"])
```

Expose this server-wide history only to authorized operators; connection views
remain isolated by session membership. The Python table contains redacted rows,
never callback credentials or executor commands. Its file is private (0600).
The existing private JSON job files remain for application workers. Each Python
write publishes the file and table under one SQLite write lock; notifications
follow the committed write. On construction, Rendezvous imports legacy files and
repairs a crash between file publication and table commit. A missing executor
file does not erase the persistent table's last recorded status.

The Node SDK continues to read its durable atomic JSON job records; the SQLite
table API currently belongs to the Python SDK used by Safrano. The result, error,
timestamps and timeline are
written **before** either the Herdr `finished` signal or the empty webhook
`POST {}`. Signals carry no status, logs or next-step instructions. The client
reads the table, finds relevant job IDs, and requests details/logs separately.

Each response contains `entries`, `total`, `cursor`, `since`, `scope: connection`.
Python also returns `persistent: true`, `ephemeral: false`, and
`membership: connection`; Node marks its in-memory view `ephemeral: true`.
The Python shared table uses `scope: server` and a separate persistent cursor.
`since=0` returns the full view. Passing a previous
cursor returns only changed rows; reads never acknowledge or consume changes.
Elapsed time alone does not create a change. Unknown/future cursors are rejected.
Rows include the ordinary redacted job status, flags/metadata, `change_cursor`,
a transition `timeline` and `timing`: UTC queue/start/update/finish/notification
timestamps and elapsed/queue/run seconds. Unknown execution starts remain null;
worker-observed starts are marked `started_source: observed`, provider timestamps
can be marked `provider`. Timeline timestamps are Unix seconds of observation.

Automatic child jobs use `parent_id`; they appear recursively in the initiating
connection's table even when queued by a separate worker. Applications still
own operation execution and automatic continuation:

1. Record a successful internal step and queue the configured next step.
2. Give internal child jobs no callback destination. Do not complete the parent yet.
3. At the chain's end, or on a stopping error, complete the parent and deliver its
   single signal. Record failures before delivery; never advance a failed chain.

Safrano uses its existing build → pull → optional update runner for this contract.
The table itself never dispatches, retries or makes policy decisions. Other MCP
adapters, including n8n integrations, can use the same SDK/session API; jobs from
another server/connection are not automatically imported. The standalone REST
service retains its job API and does not invent MCP sessions for HTTP requests.

Call `rv.disconnect(session)` on explicit connection teardown. Weak session keys
also release view membership when session objects are collected. Reconnect starts
an empty connection view; the shared table, background jobs and their original
destinations survive. Neither callbacks nor
shell commands are exposed in the table. Application metadata must be nonsecret,
as with `status()`. Use the exported atomic `write` helper for custom workers to
record transition times; old records remain readable without fabricated history.

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

The shared job format is atomic JSON, private files (0600) in a persistent
directory (0700). Python additionally maintains `activity.sqlite3` for its durable
shared table. Keep operation metadata nonsecret: `status()` hides callback
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
