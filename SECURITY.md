# Security Policy

This document describes how to report a vulnerability in Velune CLI, what the
project does to protect its users, and where those protections stop. It
separates controls that are implemented and enabled by default from controls
that are optional, and it lists known limitations. It is not a guarantee that
Velune CLI is free of vulnerabilities.

## Contents

- [Supported versions](#supported-versions)
- [Reporting a vulnerability](#reporting-a-vulnerability)
- [Private disclosure procedure](#private-disclosure-procedure)
- [Information to include in a report](#information-to-include-in-a-report)
- [Response and remediation expectations](#response-and-remediation-expectations)
- [Security architecture](#security-architecture)
- [Threat model and trust boundaries](#threat-model-and-trust-boundaries)
- [Filesystem and execution protections](#filesystem-and-execution-protections)
- [Credential protection](#credential-protection)
- [Network and MCP security](#network-and-mcp-security)
- [Dependency and supply-chain security](#dependency-and-supply-chain-security)
- [Known limitations and accepted risks](#known-limitations-and-accepted-risks)
- [Security acknowledgements](#security-acknowledgements)

---

## Supported versions

Velune CLI is pre-1.0 and maintained by one person. Security fixes are made on
`main` first and released as a new version.

| Version | Security fixes |
| :--- | :--- |
| `main` | Yes. Fixes land here first. |
| Latest published release (see [PyPI](https://pypi.org/project/velune-cli/)) | Yes. Critical fixes are released as a patch version. |
| Older releases | Not guaranteed. Upgrade to the latest release. |

There are no long-lived release branches, so backports to older versions are not
planned.

---

## Reporting a vulnerability

**Do not open a public GitHub issue, discussion or pull request for a security
vulnerability.**

- **Preferred:** open a private report with
  [GitHub Security Advisories](https://github.com/Surya-Hariharan/Velune-CLI/security/advisories/new).
  The maintainer can work with you there before anything is made public.
- **If you cannot use GitHub:** email the maintainer at
  `suryahariharan2006@gmail.com`, say that the message is a security report, and
  ask for a private channel before sending details. Do not put exploit details in
  a first message to a public mailing list or issue. The project does not
  currently publish a PGP key.

Issues that are not vulnerabilities (bugs, hardening ideas, questions) belong in
public [issues](https://github.com/Surya-Hariharan/Velune-CLI/issues).

---

## Private disclosure procedure

1. You report privately through one of the channels above.
2. The maintainer acknowledges the report and assigns a triage owner.
3. The maintainer reproduces the issue and assigns a severity. They may ask you
   for more detail or a proof of concept.
4. A fix is developed and tested on a private branch, with regression tests where
   applicable.
5. Disclosure is coordinated with you: a patched release, a published advisory,
   release notes, and a CVE where appropriate.
6. The advisory is closed and you are notified. You are credited unless you ask
   not to be.

Please allow a reasonable remediation window before disclosing publicly. The
maintainer will not pursue legal action against researchers who act in good
faith, avoid accessing other people's data, and follow this procedure. This is a
statement of intent, not a formal safe-harbor agreement.

---

## Information to include in a report

```text
Title:              Short summary of the issue
Repository:         Surya-Hariharan/Velune-CLI
Affected versions:  Package version or git SHA (output of `velune --version`)
Impact:             What an attacker gains (code execution, data exposure, sandbox escape, ...)
Reproduction:       Step-by-step instructions or a minimal proof of concept (redact secrets)
Environment:        Operating system, Python version, install method, provider configuration
```

`velune doctor --json` produces an environment report with home-directory paths
shortened to `~`. Review it before attaching it.

---

## Response and remediation expectations

| Severity | Typical examples | Acknowledgement target | Remediation target |
| :--- | :--- | :--- | :--- |
| Critical | Remote code execution, sandbox escape, credential or data exfiltration | 24 hours | Emergency patch as soon as practical |
| High | Privilege escalation, significant data exposure | 72 hours | 30 days |
| Medium | Limited impact, or requires user interaction | 5 business days | 90 days |
| Low | Informational or minor issues | 10 business days | Normal release cadence |

These are targets for a single-maintainer project, not commitments. Actual times
depend on complexity, dependencies on upstream fixes and maintainer availability.
If you have not received an acknowledgement within the target, follow up on the
same channel.

---

## Security architecture

Velune CLI is a local application. It has no server component of its own, and
all of its state is stored on the machine where it runs. The design goal is that
untrusted input (model output, repository contents, web and MCP responses) should
not turn into code execution, credential disclosure or cross-project data
leakage without an explicit and visible user action. The controls below work
toward that goal; they reduce risk and do not eliminate it.

| Control | Default | Where |
| :--- | :--- | :--- |
| Execution modes (Manual, Plan, Auto) and approval policy | Enabled; default mode is Manual | `velune/permissions/`, `velune/cli/execution_modes.py` |
| Path guard (workspace containment, symlink resolution) | Enabled | `velune/execution/path_guard.py` |
| Diff preview and approval of writes | Enabled in Manual mode; not applied in Auto mode for changes inside the project | `velune/execution/diff_preview.py` |
| Managed command runner (no shell, allowlist, environment scrubbing, resource limits) | Enabled | `velune/execution/sandbox.py` |
| OS-level isolation through a Docker container | **Optional, off by default** | `velune/execution/docker_sandbox.py` |
| Outbound URL validation for web fetch and MCP | Enabled | `velune/tools/web/validator.py`, `velune/mcp/security.py` |
| Workspace trust for project-level MCP servers, hooks and `base_url` overrides | Enabled | `velune/core/trust.py` |
| Encrypted credential store | Enabled | `velune/providers/crypto.py` |
| Log, traceback and tool-output redaction of known key formats and of connector credentials loaded at runtime | Enabled | `velune/core/redaction.py` |
| Integration permission scopes, resource ownership and compose-file inspection | Enabled | `velune/permissions/`, `velune/resources/` |
| Dynamic integration discovery: tools offered to the model are exposure only, never permission | Enabled; `resources.expose_to_model` turns it off | `velune/resources/capabilities.py`, `velune/resources/tools.py` |
| Docker Engine integration: SDK client, no unauthenticated network sockets, proven resource ownership, ownership-aware approvals | Enabled; `resources.docker.enabled` turns it off | `velune/resources/docker/` |
| HTTP integration network policy, connection pinning (DNS-rebinding guard) and origin-bound credentials | Enabled; `resources.http.enabled` turns it off | `velune/resources/http/` |
| Opt-in local crash reports | **Off by default** | `velune/cli/crash_reporter.py` |
| Git snapshot and rollback for plan-driven changes | Enabled | `velune/execution/rollback.py` |

### Execution modes

- **Manual** (default) asks before every change.
- **Plan** writes a plan and changes no project files until you approve it.
- **Auto** creates, edits and deletes files and runs commands inside the project
  without asking. In every mode, actions outside the project folder, high-risk
  operations (for example `git reset --hard`, `git clean`, force-push, recursive
  deletes, global installs) and access to secrets still require approval, and
  commands such as `sudo` and disk formatting are not run. These rules are
  enforced in the tool layer rather than by instructions to the model, and each
  decision is logged (`velune trace --type permission`).
- Operations on applications (Docker, databases, external MCP servers) are
  described as integration actions with a permission scope such as
  `docker.container.stop` or `mcp.<server>.<tool>` and decided by the same
  rules. An integration action without a valid scope is refused in every mode.
  Plan refuses every change. Manual asks. Auto runs ordinary changes but still
  asks for destructive operations, for anything the current task did not create
  (a container that was already running is never assumed to be the task's
  own), and for a compose file that asks for a privileged container, the host
  network, a host bind mount, the Docker socket or a device.
- The model is offered integrations through discovery tools, not all at once. Being offered a tool grants no
  permission: discovery is read-only in every mode, a call to a tool that was not enabled is refused, arguments are
  validated strictly before anything runs, and each call is decided by the rules above exactly as if it had been
  made directly. Descriptions, schemas and discovery results never contain credentials or connection details. The
  model can start, read, wait for and stop only the background commands it started, by job id, never by process id.
  `resources.expose_to_model = false` withholds every integration tool from the model.
- `/approve ask|safe|block` is a legacy alias for the execution modes: `ask`
  selects Manual, `safe` selects Auto and `block` selects Plan.

### Zero telemetry

Velune CLI contains no analytics, usage reporting or remote crash reporting, and
it sends no data to the maintainers. Token, cost and latency tracking is local.
Network traffic is limited to what you direct it to: the model providers you
configure, MCP servers, git hosts, web pages you ask it to fetch, and a request
to the PyPI index when you run `velune update`.

An opt-in, off-by-default local crash reporter (`telemetry.crash_reports_enabled`,
or `/crashreports on`) writes a redacted JSON snapshot of an unhandled crash
(exception, traceback, OS, Python and Velune versions; no local variables and no
prompt or conversation content) to `~/.velune/crash_reports/` on your own machine.
Nothing is transmitted; you may attach a report to an issue yourself.

---

## Threat model and trust boundaries

**Assets protected:** your source code and the contents of your files, provider
API keys, the files and processes on your machine, and the separation between
your projects.

**Trust boundaries.** Velune treats the following as untrusted input and applies
the controls named in this document at each boundary:

| Boundary | Untrusted input | Main controls |
| :--- | :--- | :--- |
| Model to tools | Model output and tool-call arguments | Execution modes, path guard, command runner, diff preview |
| Repository to Velune | Files, `velune.toml`, `.mcp.json`, hooks of a repository you did not write | Workspace trust, path guard, `.veluneignore` |
| Network to Velune | Web pages, MCP server responses, provider responses | URL validation, trust gating, read-only MCP server defaults |
| Archive to Velune | Backup archives passed to `velune restore` | Path and link checks on extraction; restore only archives you made |

**In scope:** a malicious or manipulated model response, a hostile repository
that you open or clone, a malicious web page or MCP server, and accidental
credential exposure through files, logs or backups.

**Out of scope:**

- An attacker who already runs code as your user, or who can already modify
  Velune's data directories, the directories on your `PATH`, or the `uv`
  executable. Velune does not defend against a same-user attacker.
- The model provider. Anything sent to a cloud provider is subject to that
  provider's handling and retention.
- Vulnerabilities in third-party tools that Velune runs on your behalf, in your
  operating system, or in dependencies that have no fixed release.

---

## Filesystem and execution protections

### Managed command execution

Velune runs shell commands through `SubprocessSandbox`
(`velune/execution/sandbox.py`). This is a **managed, resource-limited execution
environment**, and it is **not** an OS-level security sandbox. There is no
namespace, seccomp, Job Object or container isolation; commands run as the
invoking user. The controls below limit which programs can start and bound their
resources. They do not isolate a program that is allowed to run. OS-level
isolation is available only through the optional Docker sandbox described below.

- **No shell.** Commands run through `subprocess.Popen(argv, shell=False)`. The
  argument vector is parsed with `shlex`, and inline shell operators (`;`, `&&`,
  `|`, backticks, `$(...)`) are rejected, including in argument position.
- **Executable allowlist.** Only basenames on the configured allowlist
  (`execution.allowed_executables`) may run, and the resolved binary must be in a
  trusted system or virtual-environment path (a PATH-hijack guard, enforced on
  POSIX and on Windows). The validated absolute path is pinned to close the
  time-of-check to time-of-use window before execution.
- **Interpreter inline-code blocking.** The allowlist includes interpreters
  (`python`, `node`, and others). Inline-code flags (`python -c`,
  `node -e`/`--eval`/`-p`, including Python short-flag clusters such as `-Ic`) are
  rejected, so an agent cannot run arbitrary program text without first writing a
  file. In Manual mode an agent-authored file passes the diff-preview approval
  before it can be run.
- **Environment scrubbing.** `LD_PRELOAD`, `DYLD_INSERT_LIBRARIES`, `PYTHONPATH`,
  `PYTHONSTARTUP`, `BASH_ENV` and similar injection vectors are removed from the
  child environment.
- **Resource and lifetime limits.** A wall-clock timeout and a memory ceiling are
  enforced by killing the entire process tree (`psutil`), so descendants do not
  outlive a timeout or limit breach.

**Shell-based exceptions.** Two code paths run user-configured commands through a
shell by design, outside the runner above: lifecycle **hooks**
(`velune/hooks/executor.py`) and **custom slash commands** defined in TOML files
(`velune/cli/commands/file_commands.py`). Project-level hooks are loaded only for
trusted directories. Custom slash commands are read from
`~/.velune/commands/` and from `<workspace>/.velune/commands/`; a command of type
`shell` runs with your privileges when you invoke it. Workspace-level definitions are
loaded only for a directory you have trusted (`velune trust add`, or answer yes to the
trust prompt, which also appears when a repository ships only such files), exactly like
project hooks and project MCP servers; user-level definitions always load. Review those
files in repositories you do not control. CI allows only these two call sites to use a
shell.

> **Known limitation.** Allowlisted interpreters and build tools (`python`,
> `node`, `go`, `make`, and others) can execute files that exist in the
> workspace with your privileges. Treat command execution as "code you have
> allowed to run", not as a hard isolation boundary, unless the Docker sandbox is
> enabled.

### Optional Docker sandbox

For OS-level isolation, `velune/execution/docker_sandbox.py` provides a
`DockerSandbox` that runs commands inside a per-session container with the
workspace mounted as a volume, so agent-executed code cannot affect the host
beyond that mount. It is **opt-in and off by default**:

- It requires the `docker` extra and a reachable Docker daemon, and falls back to
  the managed runner automatically if Docker is unavailable.
- Enable it for all agent execution with `execution.docker_sandbox = true` in
  `velune.toml`, or start and inspect it with `/sandbox docker` and
  `/sandbox status`.
- Each session gets its own container (`velune-<random>`), which is stopped and
  removed at the end of the session. State does not persist across sessions.
- The workspace mount is writable, so the sandbox protects the rest of the host,
  not the workspace itself, and the container uses Docker's default bridge
  network, so commands inside it can still reach the network.

### Filesystem protection

- Every filesystem and git tool resolves paths through `PathGuard`
  (`velune/execution/path_guard.py`), which canonicalizes the path (resolving
  symlinks) and requires the result to stay inside the workspace root. This blocks
  `../` traversal, absolute-path escapes and symlink escapes. Access outside the
  workspace requires approval.
- Changing a file that can make other programs run code is high-risk in every
  mode and is never covered by a session-wide "always allow": anything under
  `.git/` (hooks, config), `.velune/hooks.json` and `velune.toml`
  (`protected_reason` in `velune/permissions/boundary.py`). Reading them is
  ordinary.
- File changes go through one layer (`velune/tools/filesystem/mutation.py`):
  content is written to a temporary file in the same folder, `fsync`-ed and
  swapped in with `os.replace`, so a failure leaves the old file intact. The
  folder entry is not `fsync`-ed, so a power loss right after a write can still
  lose the rename. The target's boundary is re-checked immediately before the
  swap. A file that changed on disk since the agent last read it is not
  overwritten (conflict detection), and every write is verified by hash.
- In Manual mode, writes, creations and deletions are shown through `DiffPreview`
  and are applied only after you confirm. In Auto mode, changes inside the project
  are applied without a per-change prompt; the path guard and the high-risk
  approvals described above still apply.
- Plan-driven changes create a git stash or branch snapshot before any edit, and
  the workspace is restored to that snapshot if execution fails. Velune does not
  commit council edits unless `execution.council_auto_commit = true`.

### Workspace and memory isolation

- Per-project state (the SQLite cognitive core, vector and semantic stores) is
  stored under `<app-data>/workspaces/<name>-<hash-of-absolute-path>/`
  (`velune/core/paths.py`), so two projects do not share a cognitive core,
  embeddings, retrieval index or sessions. Same-named folders at different paths
  are distinguished by the path hash.
- Switching workspaces closes the old storage handles before the services are
  bound to the new root.

### Local lifecycle commands

`velune update`, `repair` and `uninstall` never run a `uv` found in the current
directory (including alias spellings of it), and `velune purge-data` deletes only
inventoried files and refuses links and junctions. Velune does **not** isolate
itself from a same-user attacker; see
[Known limitations](#known-limitations-and-accepted-risks) for the specific
races.

### Executable search path (Windows)

On Windows, `CreateProcess` looks in the current directory before `PATH` for a
command given without a path. Velune runs `git` and other tools by bare name and
is often started inside repositories it did not write, so a `git.exe` committed
to a cloned repository could otherwise run as soon as Velune starts there.
Velune's entry point therefore sets `NoDefaultCurrentDirectoryInExePath` for the
process (verified on Python 3.11, 3.14 and 3.15, where Velune's own `subprocess`
calls then ignore a planted `git.exe`). The variable is inherited by child
processes; use an explicit path such as `.\tool.exe` to run a program from the
current directory. Code that imports Velune as a library does not get this
protection and should set the variable itself. The lifecycle commands also
resolve `uv` with their own trusted lookup (see
[docs/lifecycle.md](docs/lifecycle.md)).

### Plugins

Plugins are declarative: markdown commands, `SKILL.md` context injection,
subprocess lifecycle hooks (`velune.hooks.HookDispatcher`) and MCP server
registration (`velune/plugins/manager.py`, `velune/plugins/declarative/`). There
is no loader for arbitrary plugin Python code; an earlier experimental
code-plugin loader was disabled by default, unreachable from any shipped command,
and has been removed. Treat plugin hook scripts as arbitrary code that you choose
to run.

---

## Credential protection

- **Storage.** API keys are bring-your-own and are stored AES-GCM-encrypted on
  disk (`velune/providers/crypto.py`). They are never written into the workspace.
  Each provider also supports an environment-variable fallback (for example
  `GROQ_API_KEY`). A key saved through Velune takes precedence over an
  environment variable unless `VELUNE_PREFER_ENV_KEYS=1` is set; `/model status`
  and `velune provider test` report which one is in use, never its value.
- **Master key.** The encryption key is taken, in order of preference, from an
  existing OS keyring entry; a newly generated key stored in the keyring; a key
  derived from `VELUNE_MASTER_PASSPHRASE` (PBKDF2-HMAC-SHA256, 390,000
  iterations, for headless servers, Docker and CI); and, as a last resort, a weak
  machine-derived key, so that credentials are never written in the clear. The
  last case logs a one-time warning that recommends setting
  `VELUNE_MASTER_PASSPHRASE`.
- **Credential store integrity.** Writes are atomic, serialised by a
  cross-process lock, and read back and compared before success is reported;
  failure raises an error. An unreadable store (changed keyring or passphrase,
  truncation) is never overwritten: Velune reads `credentials.json.bak` in
  place, refuses to write, and says so at startup. `velune provider repair` moves
  an unreadable file aside (`credentials.json.unreadable-<time>`) instead of
  deleting it, and does nothing when the file is intact but the keyring is locked
  (the message then says to unlock the keyring, and Velune picks the keys up
  again without a restart). A transient keyring read failure never re-encrypts
  the store with the weaker machine-derived key and never generates a replacement
  master key.
- **A 403 is not a bad key.** Only a 401 (invalid, expired or revoked) marks a
  stored key invalid. Timeouts, rate limits, outages and permission errors leave
  it intact, and it is checked again automatically.
- **Private files from creation.** The credential store and the persisted
  master-passphrase file are created exclusively with mode `0600` (on Windows,
  with an ACL restricted to the current user before any data is written),
  replacing any stale file or symlink at that name instead of writing through it,
  and are then renamed into place (`velune/core/privatefile.py`). Earlier versions
  wrote the file first and tightened permissions afterwards, leaving a short
  window with the default umask. `icacls` runs from `%SystemRoot%\System32`, not
  by name lookup.
- **Log redaction** (`velune/core/redaction.py`). A logging filter scrubs known
  key shapes (`sk-…`, `sk-ant-…`, `xai-…`, `gsk_…`, `hf_…`, `AIza…`, OpenRouter,
  Fireworks, Replicate), `Authorization: Bearer` and `Token` headers, and the
  live values of configured provider environment variables from emitted log
  records and JSON exception traces. The same redaction is applied to tool
  output before it is returned to the model. It also masks `scheme://user:password@host`
  passwords, `password=` / `api_key=` style pairs, and the exact value of every
  connector credential loaded during the session (below). This covers known
  formats and registered values and cannot recognize every secret.
- **Connector credentials.** Database passwords and Supabase keys entered with
  `/resource configure` are stored in the same AES-GCM-encrypted store, under a
  `resource:<type>:<name>` id that cannot collide with a provider. When a
  connector loads one, the value is registered with the redaction layer, so it is
  masked in resource results, errors, approval previews, logs and tool output even
  though it has no recognizable shape. Structured results also mask the values
  of sensitive keys (`password`, `token`, `authorization`, `cookie`, …).
- **Indexing exclusions.** The `.veluneignore` template excludes `.env`, `*.pem`,
  `*.key` and other credential files from indexing. Runtime artifacts
  (`*.db-wal`, `*.db-shm`, `velune.local.toml`) are excluded from commits and
  indexing.
- **Secret scanning.** `gitleaks` runs on every push and pull request in CI
  (`.github/workflows/ci.yml`), scanning only the commits introduced. Contributors
  can opt in to a local pre-push scan (`pre-commit install --hook-type pre-push`;
  see [CONTRIBUTING.md](CONTRIBUTING.md)).

### Backup and recovery secrets

`velune backup` (`velune/recovery/archive.py`) never writes API keys to an
archive in the clear:

- By default, provider keys are excluded; the exported `providers.json` carries
  only masked (`"***"`) key fields.
- `velune backup --with-secrets` requires a passphrase and AES-GCM-encrypts the
  key values into a separate `providers.json.enc` payload, using a key derived
  only from that passphrase (`encrypt_with_passphrase`). It is independent of the
  OS-keyring key, so the archive can be restored on a machine without that
  keyring entry. The passphrase is not stored and must be supplied again on
  `velune restore`.

**Restoring an archive is a trust decision.** A backup carries the workspace
trust list and configuration, which can enable hooks. Restore only archives you
made yourself; an archive from an untrusted source can mark directories as
trusted. Extraction refuses members that escape the target directory and, on
interpreters without the tar `data` filter, also refuses symlink, hard-link and
special-file members (a link cannot be detected by the path check because it is
not on disk yet). Velune's own backups contain only regular files and
directories. A hostile archive can still be large; no size limit is enforced on
extraction.

---

## Network and MCP security

### SSRF and network hygiene

The web-fetch guard (`velune/tools/web/validator.py`) validates every outbound
URL:

- It blocks loopback, RFC 1918 private ranges, link-local (`169.254.0.0/16`,
  `fe80::/10`), carrier-grade NAT (`100.64.0.0/10`), IPv6 unique-local
  (`fc00::/7`), reserved and multicast ranges, and `0.0.0.0/8`.
- It blocks cloud-instance metadata endpoints (AWS, Azure and GCP
  `169.254.169.254`, ECS `169.254.170.2`, GCP `metadata.google.internal`,
  Alibaba `100.100.100.200`, and the IPv6 metadata address).
- It resolves hostnames with `getaddrinfo` and checks all returned addresses
  (defeating CNAME tricks), and rejects numeric-escape IP forms
  (hexadecimal, octal, decimal), embedded credentials and non-HTTP(S) schemes.
- Because a check followed by a second, independent lookup by the HTTP client is
  open to DNS rebinding, `web_fetch` now also connects through the pinned
  transport described under [HTTP and API integration](#http-and-api-integration):
  the name is resolved once, the answer is validated, and the socket is opened to
  exactly that address. (When a proxy is configured through `HTTP_PROXY` /
  `HTTPS_PROXY` / `ALL_PROXY` the proxy resolves the name itself, so only the
  URL-level validation applies.)

### Docker integration

Docker daemon access is effectively host-level privilege, so the `docker` integration
(`velune/resources/docker/`, see [docs/docker.md](docs/docker.md)) treats the connection as a security boundary:

- **Connection.** The official Docker SDK talks to a local socket or named pipe. An unauthenticated
  `tcp://` endpoint is refused outright; a network endpoint needs TLS with a CA and client certificate and
  certificate verification is never disabled; `ssh://` uses the system `ssh` client. `resources.docker.host`
  is ignored for an untrusted workspace so a repository cannot redirect Docker. Every change to a remote
  daemon needs a person. Reachability, not CLI presence, defines availability.
- **Ownership is proven, not inferred.** Velune manages unattended only objects it created, recorded by
  the daemon's stable id plus a creation fingerprint in its own data directory (outside the workspace). Labels
  are only claims — an image can ship `dev.velune.managed=true` — so a label without a record is `external`, and
  an inconsistent record is not trusted. A name is resolved to an id and the action runs on that id; a name
  re-pointed after approval is refused. A pre-existing image tag is never owned, and name collisions never adopt.
  The record file is authenticated with an HMAC-SHA256 whose key lives in the encrypted keystore (bound to the daemon
  and workspace), written atomically under a cross-process lock and merged across processes; a file that is
  corrupt, forged, copied from elsewhere or in an old format is set aside and **nothing in it is trusted**.
- **Authorization.** AUTO may create ordinary non-privileged containers and start/stop/restart/remove the
  ones Velune created; everything else — other containers, images, volumes, networks in use, privileged mode,
  capabilities, host networking, devices, non-loopback ports, Docker-socket or secret bind mounts, `compose
  down` — needs a person, and a bind mount outside the workspace is refused in AUTO. Plan mode executes no
  mutation.
- **Containment of what the model can ask for.** No reflective or `exec` operation; validated references
  that cannot be read as CLI options; commands are never run through a shell; ports bind to loopback by
  default; reserved labels cannot be forged; environment variables are shown by name only; logs and output are
  bounded and redacted; Compose files are scanned for privileged settings, host mounts, the Docker socket,
  devices and external resources before `compose up`. The scan parses the file structurally (PyYAML `safe_load`, with
  size, depth and document limits) in addition to a line scan, so flow-style YAML, quoted keys, anchors, JSON files and
  variable bind sources are seen; what cannot be parsed or inspected is reported as unverified and asks.

### Database integration

The `postgres`, `mysql` and `supabase` integrations (`velune/resources/sql_base.py` and friends, see
[docs/database.md](docs/database.md)) treat model-written SQL as untrusted input:

- **Credentials.** A connection's host, port, user, database and password are stored together in the encrypted
  keystore and referred to by name; configuration only names a credential and sets policy, so a repository's
  `velune.toml` cannot redirect a stored password. A non-local host is refused unless the *stored* connection allows
  it and requires TLS. Passwords are registered for redaction and never appear in results, errors, previews or logs;
  only the driver's primary error message is kept (detail lines can echo row values).
- **One parameterized statement.** Values travel as bound driver parameters, never spliced into text. The SQL is
  tokenized (strings, comments, `$$` quoting, quoted identifiers) and counted; several statements, none, or text
  that cannot be parsed are refused before reaching the server. Where a dialect's quoting is ambiguous every reading
  is analysed and the strongest wins, so text one engine treats as a comment cannot hide a statement.
- **Structural classification, engine-enforced read-only.** Data-modifying CTEs, `EXPLAIN ANALYZE` of a write,
  `SELECT … INTO`, row locks, and functions with side effects are not reads. Reads run in a `READ ONLY` transaction that
  is always rolled back, so a mis-classified write is still stopped by the server. Administrative statements
  (privileges, roles, session state, `COPY`, `DO`, dynamic SQL, transaction control, `KILL`, `USE`) are blocked in every
  mode and never offered.
- **Authorization.** Reads need no confirmation. Every change (data, schema, destructive, transaction commit) is
  high-risk: MANUAL and AUTO ask (an "always allow" answer is not honoured), PLAN denies it, and no database is
  assumed disposable because of its name. A read-only connection blocks every change in every mode. Discovery grants
  nothing, and connections run as the role you stored: Velune never changes role or privileges.
- **Bounds.** Row, cell and total output caps with truncation reported; client- and server-side statement timeouts with
  query cancellation and a dropped connection on timeout; idle transactions are rolled back; a failed statement can
  never be committed; sensitive column names are masked.
- **Discovery respects database privileges.** Catalog queries filter by the connected role's own privileges, so an
  object the role cannot use is not listed, and a foreign key does not reveal a table the role cannot see.
- **Supabase.** Reads use the anon key so Row Level Security applies; the service-role key is never shown to the model
  and *using* it asks in every mode, is denied in PLAN and is never covered by a session grant.

### HTTP and API integration

The `http` integration (`velune/resources/http/`, see [docs/http.md](docs/http.md))
sends requests on the agent's behalf. It runs through the same permission gate as
every other integration; what is specific to it:

- **Outbound policy.** URLs are parsed strictly and rebuilt from their validated
  parts. Cloud-metadata, link-local, unspecified, multicast and reserved addresses
  are never reachable, whatever is configured; IPv4-mapped, NAT64, 6to4 and Teredo
  forms are judged by the IPv4 address they embed; unusual IP notations, embedded
  credentials, backslashes, control characters and percent-encoded hosts are
  rejected. Loopback (`localhost`, `127.0.0.1`, `[::1]`) is reachable because
  testing a local backend is the main use, but an untrusted local origin is
  confirmed in manual mode and sensitive local ports (Docker's engine API,
  databases, Redis, etcd, the Kubernetes API, …) are high-risk. Private ranges
  need a trusted origin. `trusted_origins` are exact `scheme://host:port` matches
  and are honoured only in a trusted workspace (`velune trust add`).
- **DNS rebinding.** The hostname is resolved exactly once, *inside* the
  connection attempt, by an httpcore network backend; every returned address is
  validated and the socket is opened to a validated address as an IP literal.
  TLS still uses the original name for SNI and certificate validation, and the
  `Host` header is unchanged. A public name that resolves to loopback or a private
  range is refused unless the origin is trusted. Proxies and Unix sockets are not
  used.
- **Redirects** are followed by Velune: every hop is re-parsed and re-classified,
  blocked destinations, HTTPS→HTTP downgrades and public→local jumps are rejected,
  credentials/cookies are dropped when the origin changes, and an unsafe method is
  not re-sent to a different origin.
- **Credentials** are stored in the encrypted keystore, referred to by name, and
  **bound to one origin**: never sent to another host, port or scheme, nor
  after a redirect. They are registered for redaction and are masked in results,
  errors, audit records, approval previews and `httpx` logs. Collections cannot
  hold literal credentials and cannot add trust.
- **Limits.** Connect/read/write/pool timeouts, an overall deadline, a request cap,
  a streamed response cap with bounded gzip/deflate decompression, a redirect cap
  and a connection cap.
- **Authorization.** The method sets the base tier and the destination and path
  escalate it (production environments, untrusted remote origins, uploads,
  state-changing-looking paths). Collections are authorized against every request
  at once and refuse to run if they changed after approval. OpenAPI documents are
  untrusted input and cannot change what is trusted.

### MCP trust gating

External MCP servers are an outbound trust boundary. `velune/mcp/security.py`
validates every server URL before the client connects:

- It rejects embedded credentials and non-HTTP(S) schemes.
- It always blocks cloud-metadata and link-local targets (resolved through DNS,
  so rebinding does not bypass the check). Loopback and LAN addresses remain
  allowed, because local MCP servers are the common and legitimate case.
- An optional host allowlist (`[mcp] allowed_hosts` in `velune.toml`) restricts
  connections to the listed hosts only; when it is set, every other host is
  denied.

Connecting is not permission to call. Every call to an external server's tool
runs through the same permission gate as a built-in tool, in the scope
`mcp.<server>.<tool>`: Plan refuses it, Manual asks, Auto runs it unless the
server declares the tool destructive, and the decision is audited. A server's
tool annotations can only raise the risk, never lower it, because the server is
not trusted to describe itself. See [docs/mcp.md](docs/mcp.md#tool-call-authorization-execution-mode-level).

The MCP server that Velune exposes (`velune mcp serve`) uses stdio and offers
read-only tools unless `VELUNE_MCP_ALLOW_MUTATIONS=1` is set. See
[docs/mcp.md](docs/mcp.md).

### Workspace and project trust

Separately from URL-level checks, `velune trust add|list|forget`
(`velune/core/trust.py`) records which directories you trust. Project-level
`.mcp.json` servers (which can start local processes), project hooks
(`.velune/hooks.json`) and `base_url` overrides in a project `velune.toml` (which
can redirect authenticated API traffic) are honored only for trusted directories.
For an untrusted directory they are skipped and user-level configuration applies.
The rest of a project `velune.toml` is still read. Setting `VELUNE_TRUST_ALL=1`
disables this gate; it is intended for CI and containers whose workspace is
already known to be safe and should not be set otherwise.

---

## Dependency and supply-chain security

### Dependencies

- `mcp>=1.30.0` is declared in `pyproject.toml` to floor out CVE-2026-59950 and
  the later `mcp` advisories fixed in 1.30.0. The upper bound `<2.0.0` exists
  because 2.0 changed the server API that `velune/mcp/server.py` uses.
- `[tool.uv] constraint-dependencies` sets floors for transitive packages that
  had advisories (`msgpack>=1.2.3`, `certifi>=2026.10.18`,
  `virtualenv>=21.14.9`), and `uv.lock` pins patched versions of `filelock`,
  `opentelemetry-*` and the other dependencies.
- The former `llamacpp` extra was permanently removed: `llama-cpp-python` pulls
  in `diskcache` 5.6.3 or earlier, which deserializes cache entries with `pickle`
  (code execution if an attacker can write to the cache directory), and no fixed
  release exists. See [Optional extras](README.md#optional-extras) in the README.
- `uv.lock` pins the resolved dependency graph, including versions and hashes.
  The CI step `Verify lockfile reproducibility` (`uv lock --check`) fails if the
  lock falls out of sync with `pyproject.toml`, and `pip-audit` checks the
  installed dependency closure.

### Supply chain: CI and releases

- Every third-party GitHub Action is pinned to a full commit SHA (with the tag in
  a comment). Dependabot proposes updates after a seven-day cooldown.
  `GITHUB_TOKEN` is read-only by default in both workflows. `release.yml` grants
  `contents: write` only to the job that creates the GitHub Release and
  `id-token: write` only to the publish job. Checkouts do not persist
  credentials, and no workflow uses `pull_request_target`, so code from a fork
  does not run with secrets. The release workflow does not use a pip cache, has a
  per-tag concurrency group that never cancels a publish, and passes tag-derived
  values to scripts through `env:` rather than expanding them into script text.
- The `gitleaks` binary used in CI is verified against a pinned SHA-256 before it
  is run.
- Release artifacts are built with Hatchling in reproducible mode
  (`SOURCE_DATE_EPOCH`), checked with `twine check --strict`, and verified by
  `scripts/release_artifacts.py` before publication.

### Static analysis (Bandit)

Every pull request is gated on
`bandit -c pyproject.toml -r velune/ --severity-level medium --confidence-level medium`;
any finding of medium severity and confidence or higher fails the build. A full
report that includes low-severity findings also runs, and a separate regression
check fails the build if the low-severity count grows past a checked-in baseline
(184 at the time of writing). The existing low-severity findings are mostly broad
`try`/`except`/`pass` blocks used deliberately so that best-effort background
work (a probe, a cache write, a telemetry span) cannot crash an interactive
session. They were left in place rather than rewritten in one change, and the
baseline ensures the count does not grow unnoticed. Raising it is valid only in
the pull request that introduces and justifies the new finding. See the
`security` job in `.github/workflows/ci.yml` for the exact commands.

---

## Known limitations and accepted risks

These are documented deliberately. Reports that restate them are welcome but are
not treated as new vulnerabilities.

**Execution**

- The SQL classifier is structural but not a full parser of either dialect; anything it cannot confirm is not a read
  and the engine's read-only transaction is the backstop. A database role with broad privileges can still be misused
  by an approved statement: approvals show the statement, and database-native permissions are the real boundary.
- The managed runner is not an OS-level sandbox. Allowlisted interpreters and
  build tools can run any file in the workspace with your privileges. Use the
  optional Docker sandbox for isolation.
- Hooks and TOML custom slash commands run through a shell by design, as
  described in [Filesystem and execution protections](#filesystem-and-execution-protections).
- In Auto mode, changes and commands inside the project run without a per-change
  prompt.
- The protected-path rule for `.git/` and Velune's hook configuration applies to
  the file tools. A shell command run through `execute_command` is governed by
  the command policy, not by that rule.
- Conflict detection covers files the agent has read or written in the session;
  a file it never looked at is changed from its current content without a
  version check. `/rewind` keeps its record in memory for the last turn only and
  does not keep files or folder trees larger than 20 MB.

**Same-user attackers and filesystem races.** Nothing in this document protects
against an attacker running as you. Someone who can already modify Velune's data
directories, the directories on your `PATH`, or the `uv` executable can create
races:

- `velune purge-data` re-validates the whole path immediately before each
  deletion, which narrows the check-then-delete window but is not guaranteed to
  be free of races.
- The `uv` executable is resolved first and run afterwards; replacing it between
  the two steps is not detected.
- `velune purge-data --execute --confirm-phrase "delete my velune data"` and
  `velune uninstall --yes` run without a person at a prompt. The phrase is
  public, so this is not equivalent to interactive confirmation: any script or AI
  agent that can run shell commands can use it to delete data (credentials and
  memory included) or the program. Do not use these flags in unattended
  automation or let an agent run them unreviewed.
- Two lifecycle commands started at the same time are refused by an exclusive
  journal (exit code 8) rather than serialised. This guards against accidents,
  not against a same-user attacker.

**Credentials**

- The machine-derived fallback key comes from the network adapter's MAC address
  and a constant. It is obfuscation against casual reading, not a secret, and
  anyone who can read the file can derive it. On Windows the persisted passphrase
  is protected by DPAPI instead; elsewhere, without an OS keyring, the file mode
  is the only real protection. Prefer a keyring or `VELUNE_MASTER_PASSPHRASE`.
- The passphrase-derived key uses PBKDF2-HMAC-SHA256 at 390,000 iterations with a
  fixed, versioned salt, so two users who choose the same passphrase derive the
  same key. Raising the iteration count or randomising the salt would make every
  existing store undecryptable and needs a versioned migration, which has not
  been done. Use a long random passphrase.
- Redaction matches known key formats, common credential patterns and the credentials
  connectors have loaded in the current session. A secret that Velune never loaded and that has
  no recognizable shape is not masked.

**Docker integration**

- Ownership records are stored unencrypted in Velune's data directory, per workspace and daemon; anyone who
  can write there can forge one. Records are reconciled against the daemon on connect, not continuously.
- Docker access is host-level privilege: an approved privileged container, Docker-socket mount or host-network
  container can control the host. The approval prompt is the control.
- The Compose file scan is a conservative line classifier (PyYAML is not a dependency); a construct it does
  not recognize is not flagged, and `include` / `extends` are reported as unverified rather than followed.
- `compose` and `build` use the `docker` CLI; registry credentials come from your own `docker login` and are
  never read by Velune.

**HTTP integration**

- A request's tier comes from its method and URL text. A server can change state on a `GET`, and
  method-override conventions such as `?_method=DELETE` cannot all be recognized; the header forms
  (`X-HTTP-Method-Override` and friends) are refused. GraphQL mutations are `POST` requests and are
  treated as writes.
- Loopback is reachable without being listed (confirmed in manual mode): another local service on a
  port Velune does not know is sensitive can be spoken to once the user approves it.
- A response body is untrusted text. The model sees it as data, but a model can still be
  influenced by what an API returns; approval prompts are the control, not the response content.
- OAuth2 authorization-code and device flows, mutual TLS, cookie jars and proxies are not
  implemented. Behind a mandatory proxy, `web_fetch` falls back to URL-level validation.
- A compressed response chunk is inflated in memory up to the response cap before an oversize
  response is rejected.

**Data sent to providers.** Prompts and the repository or memory context included
with them are sent to the provider you select. Velune does not control what the
provider retains or trains on.

**Backups.** Backup archives are not size-limited on extraction, and restoring an
untrusted archive can add trusted directories.

**Supply chain**

- The PyPI upload authenticates with the `PYPI_TOKEN` secret. Switching to
  Trusted Publishing (OpenID Connect, no stored token) needs a PyPI-side setting
  and is recommended.
- Release tooling (`build`, `twine`) is pinned by version, not by hash.
- The one-line installers are fetched from the mutable `main` branch, and they
  pipe uv's own installer from `astral.sh` into the shell, as uv documents.
  Inspect the script first if that trust is not acceptable.
- Bandit rules B404, B603 and B607 (subprocess usage) are skipped in the
  configuration. Every call site was reviewed, none uses `shell=True`, and the
  reviewed `create_subprocess_shell` call sites are enumerated by a CI check.

---

## Security acknowledgements

Security reporters are acknowledged in release notes and in the published
advisory, unless they ask not to be. No security researchers are listed yet.
Monetary bounties are not offered; if a bounty program is introduced it will be
announced separately.

---

Apache License 2.0. Copyright 2026 Surya HA.
