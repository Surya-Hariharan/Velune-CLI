# HTTP and API integration

Velune can send HTTP requests, check responses, run API collections and explore OpenAPI specifications on
your behalf — against a server on your own machine or a remote API — through the same integration runtime as
Docker and the databases. It does not need the Postman application or any cloud service.

This page covers what you can do, how to configure it, how requests are judged and secured, and what is not
implemented. For the runtime it plugs into (discovery, tool budget, permission gate) see
[development.md](development.md#capability-discovery-and-dynamic-tool-exposure); for the threat model see
[SECURITY.md](../SECURITY.md#http-and-api-integration).

## Quick start

1. Tell Velune where your API lives. In `velune.toml`:

   ```toml
   [resources.http]
   trusted_origins = ["http://localhost:5000"]

   [resources.http.environments.development]
   base_url = "http://localhost:5000"
   variables = { api_version = "v1" }
   secret_variables = { demo_password = "aidly-demo-password" }
   auth = "aidly-dev-token"
   ```

2. Store the secrets once (they go into the encrypted keystore, not the file):

   ```text
   /resource configure http aidly-dev-token      # type: bearer, origin: http://localhost:5000, token: ...
   /resource configure http aidly-demo-password  # type: secret, value: ...
   ```

3. Ask: *"Test the login API on my local Aidly backend."* The agent discovers the integration
   (`integrations_list`), enables it (`integrations_enable`), and on its next turn uses `http__request`, checks
   the response with assertions and explains the result.

The HTTP integration needs no configuration to send a request to a public URL; configuration adds
environments, trust and credentials.

## Operations

| Operation | What it does | Needs approval |
| --- | --- | --- |
| `request` | Send one request (GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS) and return a structured response; optional `expect` assertions and `select` paths | by method and destination (below) |
| `diagnose` | Find why an API is not responding: URL, DNS, connection, TLS, then a HEAD/GET; reports the failing step | like a GET |
| `environments` | List environments: base URL, variable *names*, never secret values | no |
| `collection_list` / `collection_show` | See saved collections and their requests and assertions | no |
| `collection_run` | Run a collection (or one request) in order and summarize passes and failures | for **every** request in it, together |
| `collection_import` / `collection_export` | Convert to or from a collection file in the workspace (Velune format, Postman v2.1 subset) | yes (writes a file) |
| `openapi_load` | Load an OpenAPI 3.x spec from a workspace file or a URL | no for a file; network rules for a URL |
| `openapi_search` | Search a loaded spec for endpoints, or show one endpoint in detail | no |
| `openapi_call` | Call an endpoint of a loaded spec, checked against the spec first | by the endpoint's method and destination |

The operations are grouped in four categories — `requests`, `environments`, `collections`, `openapi` — so
`integrations_enable` can expose only what a task needs. API specs are searched, not turned into tools: a
spec with hundreds of operations still costs the model one `openapi_call` tool.

### Request arguments

| Argument | Meaning |
| --- | --- |
| `method`, `url` | The method, and an absolute URL **or** a path starting with `/` (joined to the environment's base URL). `{{variables}}` are allowed. |
| `environment` | Environment name. Optional when one is `default_environment` or the only one configured. |
| `query` | List of `name=value`. |
| `headers` | List of `Name: value`. `Host`, `Content-Length`, `Transfer-Encoding`, `Connection`, `Upgrade`, `Expect` and the method-override headers are refused. |
| `json` / `form` / `multipart` / `body` + `content_type` | At most one body. `json` is a JSON document string; `multipart` accepts `name=value` or `name=@relative/path` to upload a workspace file. |
| `auth` | A credential **reference** (never a secret). Defaults to the environment's credential; `none` sends no credentials. |
| `timeout` | Overall limit in seconds (at most 120), covering redirects. |
| `expect` | Assertions (below). |
| `select` | JSON paths to return from a large JSON response. |

### Results

Every call returns plain JSON, never an HTTP-library object:

```json
{
  "ok": true,
  "data": {
    "request_id": "9f2c…", "method": "POST", "url": "http://localhost:5000/v1/auth/login",
    "environment": "development",
    "status": 200, "reason": "OK", "outcome": "success", "duration_ms": 38.4,
    "content_type": "application/json", "size_bytes": 212,
    "headers": {"content-type": "application/json"},
    "json": {"data": {"token": "***REDACTED***", "user": {"id": 1, "status": "active"}}},
    "redirects": [],
    "assertions": {"passed": true, "total": 2, "failed_count": 0, "results": [...]}
  }
}
```

`ok` means *a response was received*. The HTTP verdict is `data.outcome`: `success`, `redirect`,
`client_error`, `server_error`, `authentication_failed` (401), `authorization_denied` (403), or
`assertion_failed` when an `expect` check failed. A request that never got a response is `ok: false` with an
`error.code`:

| Code | Meaning |
| --- | --- |
| `invalid_url` | Not an `http(s)` URL, has embedded credentials, unusual IP notation, control characters … |
| `network_policy_blocked` | The destination is not allowed (metadata, link-local, a private or loopback address that is not trusted) |
| `dns_resolution_failed` | The name did not resolve |
| `connection_failed` | Refused or dropped — usually *nothing is listening on that port* |
| `timeout` | Connect, read, write, pool or the overall deadline |
| `tls_validation_failed` | Certificate untrusted, expired or for another name |
| `response_too_large` | The body exceeds `max_response_bytes` (checked while streaming) |
| `redirect_rejected` | A redirect hop was refused, or there were too many |
| `invalid_response` | Not valid HTTP, or an unsupported / corrupt content encoding |
| `authentication_failed` | A credential is missing, invalid or bound to a different origin |
| `validation_failed` | Bad arguments (malformed header, two body kinds, an unknown variable …) |

Response bodies are bounded (6,000 characters shown; larger JSON is previewed and marked `body_truncated`),
binary bodies are reported by size only, and a field whose name looks like a secret (`password`, `token`,
`authorization`, `cookie`, …) or is listed in `redact_response_fields` is masked. A login token is therefore
**not** shown to the model; collections can still use it (see `extract`).

## Assertions

Assertions are evaluated locally by plain comparison — never by asking a model whether a response "looks
right", and never by running code or user-supplied expressions or regular expressions.

```text
status == 200            status != 500        status in 200-299       status in 200,201
header content-type contains application/json      header x-request-id exists      header x-debug absent
json data.id exists      json data.status == active     json data.items type array
json data.count >= 1     json data.name contains Ada    json data.deleted == null
body contains welcome    body not_contains error
duration < 500
```

JSON paths are dotted keys and `[index]` steps (`data.items[0].id`, optional leading `$.`); there are no
wildcards or filters. The structured form used in collection files is equivalent:
`{"type": "json", "path": "data.status", "equals": "active"}`. The result lists every assertion with
`passed`, `expected` and `actual`.

## Authentication

Credentials live in Velune's existing AES-GCM keystore (`resource:http:<reference>`), never in
`velune.toml`, and are referred to by **reference name** everywhere else. Each one is **bound to a single
origin** and is attached only to a request for exactly that origin — not to another host, port or scheme, not
after a redirect, not because an OpenAPI document names a different server.

| Type | Sends |
| --- | --- |
| `bearer` | `Authorization: Bearer …` |
| `basic` | `Authorization: Basic …` |
| `api_key_header` | a header (`X-API-Key` unless you choose another) |
| `api_key_query` | a query parameter — works, but URLs end up in logs, so every use carries a warning |
| `oauth2_client_credentials` | a bearer token fetched from the credential's own origin and cached until shortly before it expires |
| `secret` | nothing by itself: a stored value for a `secret_variables` entry |

OAuth2 authorization-code and device flows (which need a browser) are **not** implemented.

If a named credential's origin does not match the request, the request fails with `authentication_failed`
and nothing is sent. If it only came from an environment's default `auth`, it is skipped and a warning says so.
Secrets are registered for redaction when loaded, so they are masked wherever they would otherwise surface:
results, errors, audit records, approval previews, and the `httpx` logger.

## Environments and variables

An environment names one deployment of an API (see the quick start). `{{base_url}}`, the environment's
`variables` and its `secret_variables` can be used in URLs, headers, query values and bodies.

- An **unknown variable is an error** that lists the available names, never an empty string.
- Variable values are **literal**; a value containing `{{…}}` is rejected, so interpolation cannot recurse.
- **Secret variables** hold credential references. The value is read from the keystore only when the request is
  sent. A request that used a secret may only go to the environment's own origin (or its `extra_origins`), so
  switching environments or editing a URL cannot carry one environment's secret to another host.
- `production = true` makes every state-changing request to that environment high-risk (always confirmed, never
  covered by a session grant).
- `verify_tls = false` (for a local self-signed certificate) is available **only** in configuration, never as
  a model argument, and makes requests to that environment high-risk.
- An environment's `headers` are sent only to that environment's origin.

## API collections

A collection is a JSON file in `.velune/api/collections/<name>.json` (`resources.http.collections_dir`):

```json
{
  "format": "velune-api-collection/1",
  "name": "aidly-auth",
  "description": "Sign-up and login checks",
  "environment": "development",
  "auth": "aidly-dev-token",
  "headers": {"Accept": "application/json"},
  "variables": {"email": "demo@example.com"},
  "requests": [
    {"id": "login", "name": "Log in", "method": "POST", "url": "{{base_url}}/auth/login",
     "body": {"json": {"email": "{{email}}", "password": "{{demo_password}}"}},
     "assertions": ["status == 200", "json data.token exists"],
     "extract": [{"var": "token", "json": "data.token", "secret": true}]},
    {"id": "me", "name": "Who am I", "method": "GET", "url": "{{base_url}}/me",
     "headers": {"Authorization": "Bearer {{token}}"},
     "assertions": ["status == 200", "json data.email == demo@example.com"]}
  ]
}
```

| Field | Notes |
| --- | --- |
| request `body` | exactly one of `{"json": …}`, `{"form": {…}}`, `{"text": "…", "content_type": "…"}`, `{"multipart": {…}}` |
| request `auth` | a credential reference, or `"none"`; falls back to the collection's, then the environment's |
| `extract` | `{"var": "x", "json": "a.b"}` or `{"var": "x", "header": "X-Y"}`; later requests in the **same run** can use `{{x}}`. `"secret": true` keeps the value out of results and restricts it like a secret variable |
| `timeout` | per request, 1–300 seconds |

Collections are validated strictly: unknown fields are **rejected** (a collection has no field that could add
trust, origins or permissions), and a header such as `Authorization` or `Cookie` with a literal value is
refused — use `auth` or a `{{secret_variable}}`.

**Running a collection is authorized against every request in it at once.** The approval prompt lists each
destination and method; a `DELETE` in a collection is confirmed like any delete, plan mode refuses a collection
that contains a change before sending *any* of its requests, and a blocked destination denies the whole run.
If the collection file changes after it was authorized, the run is refused. Each request is then checked by the
network policy again when it connects. The result summarizes `passed`, `failed`, `errors`, and lists
`failed_requests` with their failed assertions.

**Postman.** `collection_import` with `format: "postman"` reads Postman Collection v2.1 *requests* (method,
URL, headers, raw / urlencoded / form-data text bodies), flattens folders and takes collection variables. It
does **not** import scripts, tests, pre-request logic, auth blocks, environments, file uploads or
authentication secrets, and lists what it skipped. `collection_export` with `format: "postman"` writes
a v2.1 file without assertions or extraction rules. This is a compatible subset, not full Postman
compatibility.

## OpenAPI

`openapi_load` reads OpenAPI **3.0 / 3.1** from a workspace file or a URL (JSON always; YAML with the
`velune-cli[api]` extra, parsed with `yaml.safe_load`). Swagger 2.0 is rejected with a message to convert it.
A spec is **untrusted input**: it is size-capped (5 MiB), only local `#/…` references are followed (bounded in
depth and count; external references are not fetched), and nothing in it is executed.

```text
openapi_load  file="openapi.json"  name="aidly"
openapi_search spec="aidly" query="create user"          → operation ids, methods, paths
openapi_search spec="aidly" operation="createUser"       → parameters, request body schema, responses, security
openapi_call   spec="aidly" operation="getUser" path_params=["id=42"] environment="development"
```

`openapi_call` checks the request against the spec **before** sending: required path/query/header parameters,
types, enums, numeric and length bounds, and the JSON body (`type`, `required`, `properties`, `items`, `enum`,
bounds). It does not implement `oneOf` / `anyOf` / `allOf`, `pattern` or `format`. The base URL comes from the
environment when there is one; otherwise from the spec's first `servers` entry, which is judged like any other
URL (an untrusted remote origin is confirmed, an internal address is denied). Credentials still come from a
named reference bound to an origin — a spec's `securitySchemes` only *describe* what an endpoint wants.

## How a request is judged

The gate decides before anything is sent. The **method** sets the base tier, and what the call touches
escalates it:

| | MANUAL | PLAN | AUTO |
| --- | --- | --- | --- |
| GET / HEAD / OPTIONS to a trusted origin | allowed | allowed | allowed |
| POST / PUT / PATCH to a trusted origin | asks | refused | allowed |
| DELETE | asks | refused | **asks** |
| a `GET` whose path looks state-changing (`/logout`, `/…/delete`, `/purge`, …) | like a write | like a write | like a write |
| a change to a `production = true` environment, to an environment with `verify_tls = false`, to a sensitive local service port | asks (never granted for the session) | refused | **asks** |
| a change to a remote origin that is not trusted, or a file upload to one | asks | refused | **asks** |
| any request to an origin that is not trusted | extra network confirmation | confirmation | allowed unless high-risk |
| cloud metadata, link-local, unspecified, multicast, reserved; a private address that is not trusted | **denied** | **denied** | **denied** |

Scopes: `http.request.read` / `.write` / `.delete`, `http.collection.read` / `.run` / `.write`,
`http.environment.read`, `http.openapi.read`. Discovery (`integrations_list` / `integrations_enable`) never
grants any of them.

The judgement uses the URL text only. It is **not a guarantee about what a server does**: a server can
change state on a `GET`, and `?_method=DELETE` style overrides cannot all be recognized. High-risk operations
are the ones to watch in the approval prompt.

## Network security

**Trusted origins.** `trusted_origins` (and each environment's own base URL) are exact `scheme://host:port`
matches — never prefixes or wildcards. A trusted origin skips the extra network confirmation and may resolve to a
loopback or private address. They are honoured **only in a trusted workspace** (`velune trust add`); a hostile repository's
`velune.toml` cannot trust itself. Link-local and cloud-metadata addresses (`169.254.0.0/16`,
`169.254.169.254`, `100.100.100.200`, `fd00:ec2::254`, `metadata.google.internal`, …) stay blocked whatever is
configured. Plain `localhost`, `127.0.0.1` and `[::1]` are reachable without being listed (the gate still
confirms an untrusted one in manual mode) because testing a local backend is the main use; ports of services
that are dangerous over HTTP (Docker's engine API, databases, Redis, etcd, the Kubernetes API, …) are
high-risk unless trusted.

**URL parsing.** URLs are parsed strictly and **rebuilt** from their validated parts; that rebuilt string — not
the original — is what the HTTP client receives. Rejected: embedded credentials (`user@host`), backslashes and
control characters, percent-encoded hosts, and IP addresses in unusual notation (`127.1`, `2130706433`,
`0x7f000001`, `0177.0.0.1`). IPv6 transition addresses (`::ffff:127.0.0.1`, NAT64, 6to4, Teredo) are classified
by the IPv4 address they embed.

**DNS rebinding — how it is prevented.** A client that checks `api.example.com` by resolving it and then lets
the HTTP library resolve it again can be answered differently the second time (public, then `127.0.0.1`). Here
the name is resolved **exactly once, inside the connection attempt**: a custom `httpcore` network backend
receives the hostname, resolves it, validates *every* returned address (one bad answer rejects the lot) and
opens the socket to the validated address as an IP literal. There is no second lookup to race. TLS is unaffected:
the handshake still uses the original name for **SNI and certificate validation**, the `Host` header is the
original name, and pooled connections are keyed by origin and were opened to a validated address (each request
builds its own client, so a pool is never shared between requests that were judged separately). A public name that
resolves to loopback or a private range is refused unless that origin is trusted. Proxies are not used (a proxy
would resolve the name itself), and Unix sockets are unreachable. The same transport now protects `web_fetch`.

**Redirects** are followed by Velune, not httpx. Every hop is parsed and classified again; a hop to a blocked
destination, an HTTPS→HTTP downgrade, or a public host → local address jump is rejected (`redirect_rejected`).
Credentials, cookies and anything a credential injected are dropped whenever the origin changes. A state-changing
request is **not** re-sent to a different origin: the 3xx is returned with `followed: false`. At most
`max_redirects` (default 5) hops; the chain is reported in `data.redirects`.

**Limits.** Connect / read / write / pool timeouts, an overall deadline that includes redirects, a request body
cap (`max_request_bytes`), a response cap applied **while streaming** (`max_response_bytes`), a connection cap,
and bounded decompression (gzip and deflate only, inflated with an output limit so a compression bomb cannot
exceed the cap). Responses are never held open: client and streams are closed on success, error and
cancellation.

## Configuration reference

```toml
[resources.http]
enabled = true                  # false hides the integration everywhere
trusted_origins = []            # exact origins: "http://localhost:5000"
allow_private_network = false   # true trusts every private address; prefer listing origins
allow_loopback = true           # false refuses localhost/127.0.0.1/[::1] unless an origin is trusted
default_environment = "development"
redact_response_fields = []     # extra JSON field names to mask in responses
connect_timeout = 5.0           # seconds
read_timeout = 30.0
write_timeout = 30.0
timeout = 30.0                  # overall, including redirects
max_response_bytes = 2097152    # 2 MiB
max_request_bytes = 1048576     # 1 MiB
max_redirects = 5
max_connections = 10
collections_dir = ".velune/api/collections"   # must stay inside the workspace
max_collection_requests = 100

[resources.http.environments.development]
base_url = "http://localhost:5000"
variables = { api_version = "v1" }
secret_variables = { demo_password = "aidly-demo-password" }   # variable -> credential reference
auth = "aidly-dev-token"                                        # credential reference
production = false
extra_origins = []              # more origins this environment's secrets may go to
verify_tls = true
headers = { Accept = "application/json" }
```

`resources.expose_to_model = false` withholds all integration tools from the model; `resources.max_exposed_tools`
is the shared tool budget (see the development guide). Install YAML support for OpenAPI with
`pip install "velune-cli[api]"`.

## Testing and development

```text
python -m pytest tests/test_http_*.py tests/test_web_fetch_pinning.py   # no network, no real keys
```

The suite runs a real HTTP server on `127.0.0.1` (`tests/http_server.py`, including a throw-away TLS
certificate) and a scripted model through the real tool loop, so redirects, limits, timeouts, TLS, pinning and
approvals exercise real code. DNS is simulated with an injected resolver; nothing leaves the machine. As on
Windows elsewhere in this repo, put the virtualenv's `Scripts` directory first on `PATH` before running tests.

To extend the integration, follow [development.md](development.md#resource-connectors-and-the-authorization-pipeline):
every new operation needs a scope, a `describe_actions` that can only *add* risk, and a test through the real
gate.

## What is implemented, and what is not

Implemented: the seven methods, query / headers / path parameters / JSON, form, multipart (text and workspace
files) and raw bodies, structured responses, bearer / basic / API-key (header and query) / OAuth2
client-credentials, environments with variables and secret variables, collections (run, inspect, import, export,
extraction between requests), local assertions, OpenAPI 3.x search and validated calls, the network policy and
pinned connections, request limits.

Not implemented, or limited: OAuth2 authorization-code / device flows; HTTP/2, WebSocket, SSE and gRPC;
proxies; client certificates (mTLS); cookie jars; Postman scripts, environments and auth blocks (see above);
OpenAPI `oneOf` / `anyOf` / `allOf`, `pattern` and `format` validation, Swagger 2.0, and external `$ref`s;
GraphQL-aware risk classification (a GraphQL mutation is a `POST` and is treated as a write); recognizing every
server-specific method override. No provider has been exercised live with these tool definitions — see the
provider notes in the development guide.
