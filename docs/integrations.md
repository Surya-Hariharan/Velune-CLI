# Application integrations (VAIR)

The **Velune Application Integration Runtime** lets the agent operate the applications a developer works with —
containers, HTTP APIs, databases, background processes — through one framework, `velune/resources/`. It is an
extension of the existing agent loop, not a second one: an integration operation is an ordinary tool call that
passes through the same permission gate as a file edit or a shell command.

| Integration | What it operates | Guide |
| --- | --- | --- |
| `docker` | Docker Engine (containers, images, volumes, networks, Compose) through the Docker SDK | [docker.md](docker.md) |
| `http` | HTTP APIs: requests, assertions, collections, OpenAPI | [http.md](http.md) |
| `postgres`, `mysql`, `supabase` | Databases: schema, read queries, approved changes and transactions | [database.md](database.md) |
| `process` | Background jobs the agent started | [usage-guide.md](usage-guide.md) |

## How a call travels

```text
model ── integrations_list / integrations_enable / <integration>__<operation> / integration_call
   │            (A2: discovery exposes tool definitions; it grants nothing)
   ▼
ResourceOperationTool  ──►  connector.describe_actions()    "what would this do, and how risky?"
   │                         (scoped, risk can only be raised, blocked actions are denied everywhere)
   ▼
authorize_and_execute ──►  PermissionGate  (MANUAL / PLAN / AUTO, session grants, audit)   (A1)
   │
   ▼
connector.execute()  ──►  SDK / driver / HTTP   ──►  bounded, redacted, structured result
```

* **One decision point.** Connectors *describe* a call (`describe_actions`); the gate *decides*. There is no
  second approver, and a connector cannot make a call less guarded than its declared tier.
* **Scopes.** Every operation names a dotted scope (`docker.container.start`, `http.request.write`,
  `database.query.write`, `supabase.service_role.use`). An action without a valid scope is denied.
* **Modes.** PLAN executes no mutation. MANUAL asks for every change. AUTO runs only what each integration
  documents as safe to run unattended (Docker: what Velune created; HTTP: loopback reads; databases: reads only).
* **Discovery (A2).** Each connector's `capabilities()` is the single source of truth. Operations are grouped
  into categories (`inspect`, `containers`, `schema`, `query_read`, `transactions`, …) so a task enables only what
  it needs; a tool budget keeps definitions compact and `integration_call` reaches the rest. Availability reflects
  the application (a stopped daemon, an unreachable database) and one integration's failure never marks another
  unavailable.

## Shared building blocks

| Piece | Where | Purpose |
| --- | --- | --- |
| `ResourceConnector`, `ResourceCapability`, `ResourceResult` | `resources/base.py` | The connector contract; results are structured and never raise |
| `ResourceManager` | `resources/manager.py` | Registration, lifecycle, discovery, config, the only execution entry point |
| `ResourceOperationTool` | `resources/bridge.py` | Presents an operation as a tool so it can run only through the gate |
| Capability catalog | `resources/capabilities.py` | Manifests, categories, availability, the model-facing tools |
| Secrets | `resources/secrets.py` | Encrypted keystore namespaces (`resource:<type>:<name>`); redaction registration |
| Named connections | `resources/connections.py` | Resolve `connection="staging"` to a profile; endpoint and password stay together in the keystore |
| Ownership | `resources/ownership.py`, `resources/docker/ownership.py` | Which application resources Velune created, proven rather than inferred |
| SQL core | `resources/sql_base.py`, `sql_parse.py`, `sql_results.py`, `sql_safety.py` | Statement analysis, bounded read-only execution, transactions, result shaping |

## Cross-cutting guarantees

* **Credentials** live only in the encrypted keystore, are referred to by name, are registered for redaction, and
  never appear in results, errors, previews, logs or model context.
* **Ownership is proven.** The Docker ledger records what Velune created by stable id and creation fingerprint.
  The record file is **authenticated** (HMAC-SHA256 with a key held in the keystore, bound to the daemon and the
  workspace), written atomically under a cross-process lock, merged across processes, and set aside if it is
  corrupt or fails verification — nothing in an unverifiable file is trusted. Labels are claims, not proof.
* **Untrusted input everywhere.** Model arguments are validated; SQL is tokenized and classified structurally;
  Compose files are parsed structurally (PyYAML `safe_load` with limits) and scanned; HTTP destinations are
  classified and pinned; nothing is run through a shell.
* **Bounded output.** Every operation caps rows, lines, bytes and characters, and says when it truncated.
* **Failure is data.** A connector returns a normalized error (`connection_failed`, `authentication_failed`,
  `authorization_denied`, `not_found`, `validation_failed`, `conflict`, `timeout`, …) with a hint; it does not raise.

## Adding an integration

Write one `ResourceConnector` subclass: declare `capabilities()` (action, tier, scope, category, flat parameter
schema), implement `connect` / `disconnect` / `execute`, override `describe_actions` only to *add* context or raise
risk, and register it in `ResourceManager.register_defaults`. Discovery, the tool bridge, authorization, the audit
trail and the tool budget need no changes. Tests use an in-memory fake of the application — see
`tests/docker_fakes.py`, `tests/sql_fakes.py` and the HTTP mock transport.
