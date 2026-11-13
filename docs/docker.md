# Docker integration

Velune can inspect and manage containers, images, volumes, networks and Docker Compose projects through the
same integration runtime as the database and HTTP integrations. It uses the **official Docker SDK for Python**
for the Engine API; only Compose (which the SDK does not implement) and `build` still use the `docker` CLI.

Docker daemon access is host-level privilege — anyone who can start a container can mount the host's disk.
The integration is built around that: every operation passes through the permission gate, and **ownership of
what it manages is proven, never guessed from a name**.

## Install and connect

```text
pip install "velune-cli[docker]"      # the Docker SDK (pywin32 is pulled in on Windows) and PyYAML
/resource connect docker              # or let the agent enable it
```

The daemon must be **reachable**: the `docker` CLI being installed is not enough. A stopped daemon reports
*"The Docker daemon is not running or not reachable"* with a hint, and discovery shows the integration as
unavailable (without affecting any other integration). The Engine API must be 1.41 or newer (Docker Engine 20.10).

### Which daemon

Endpoints are tried in this order; an explicit choice is never silently replaced by a default:

1. `resources.docker.host` (only honoured in a trusted workspace — a repository's `velune.toml` cannot redirect Docker)
2. `DOCKER_HOST`
3. the active `docker context`
4. the platform default — Windows: `npipe:////./pipe/docker_engine`, then `…/dockerDesktopLinuxEngine`; Linux:
   `/var/run/docker.sock`, then rootless `$XDG_RUNTIME_DIR/docker.sock`; macOS: `/var/run/docker.sock`, then Docker
   Desktop's `~/.docker/run/docker.sock`

**Remote daemons.** An unauthenticated `tcp://` socket is **refused** (even `127.0.0.1`): it gives root on that
host. A `tcp://` endpoint needs TLS with a CA and a client certificate — `tls_ca`, `tls_cert`, `tls_key` (paths
only), or the standard `DOCKER_TLS_VERIFY` + `DOCKER_CERT_PATH` — and certificate verification is never turned
off. `ssh://` uses your system `ssh` client and its host-key checking. Every change to a remote daemon is
confirmed by a person, in every mode.

The client is kept and reused, closed on disconnect, and dropped when the connection fails. After a daemon
restart, reads reconnect and retry once; **changes are never retried automatically** (they might have run).

## Operations

36 operations in six categories — enable only what a task needs (`integrations_enable … categories=["inspect"]`);
the rest stay reachable through `integration_call`.

| Category | Operations |
| --- | --- |
| `inspect` | `info`, `ps`, `images`, `inspect`, `image_inspect`, `logs`, `health`, `stats`, `network_ls`, `network_inspect`, `volume_ls`, `volume_inspect` |
| `containers` | `container_create`, `start`, `stop`, `restart`, `pause`, `unpause`, `container_remove`, `wait_ready` |
| `images` | `image_pull`, `image_tag`, `image_remove`, `build` (CLI) |
| `volumes` | `volume_create`, `volume_remove` |
| `networks` | `network_create`, `network_connect`, `network_disconnect`, `network_remove` |
| `compose` | `compose_config`, `compose_ps`, `compose_logs`, `compose_up`, `compose_stop`, `compose_down` (CLI) |

There is no generic "call any SDK method" operation and no `exec`. Each operation validates its arguments,
calls one fixed SDK method, and returns a curated, bounded view:

- **Environment variables appear by name only**; values are never shown. Logs and errors are redacted.
- Logs return the *recent end*, at most `max_log_lines` lines and `max_output_chars` characters; listings are
  capped at 500 rows.
- Container and image references are names or ids (never starting with `-`, never containing `/`, spaces or
  shell characters); image references are syntactically validated; commands are split like a shell would but
  **never run through one**; ports bind to `127.0.0.1` unless an address is given; labels under `dev.velune.*` and
  `com.docker.compose.*` are reserved.
- `container_create` creates (does not start) a container from a **local** image — pull it first. It adds
  `no-new-privileges`. Options: image, name, command, env, ports, volumes, network, restart policy, labels, memory,
  cpus; and — each needing a person — `privileged`, `host_network`, `devices`, `cap_add`.

## Ownership

Velune manages unattended only what it **created and can prove it created**.

- When Velune creates a container, volume, network or (newly pulled) image it writes a record to its own
  application-data directory (outside the workspace, so a repository cannot plant one), keyed by the daemon's
  **stable id** and a fingerprint (the creation timestamp), and adds `dev.velune.managed`, `.session` and
  `.workspace` labels.
- **Labels are claims, not proof**: an image can bake `dev.velune.managed=true` into every container made from it,
  so a label without a record is classified `external`. A record whose object lost or changed its labels, or whose
  fingerprint changed (a name was reused), is treated as inconsistent and not trusted.
- Operations resolve a name to the id first and then act on **that id**; if the name is re-pointed between the
  approval and the action, the call is refused.

| Class | Meaning | AUTO |
| --- | --- | --- |
| `velune` | created by this session; record, labels and fingerprint agree | may manage |
| `velune_previous` | created by Velune in an earlier session (records survive restarts, per daemon) | asks |
| `preexisting` | present when the session first connected | asks |
| `external` | carries Velune/Compose labels but no record | asks |
| `unknown` | anything else, and anything that could not be looked up | asks |

- A **pre-existing image tag is never owned**: pulling a tag that already exists records nothing; `image_tag`
  refuses to move an existing tag; only an image id that did not exist before is recorded.
- Name collisions never adopt: creating a container, volume or network whose name exists fails with a conflict and
  records nothing.
- On connect, records are **reconciled** against the daemon: vanished, re-created or relabelled objects are dropped.
- **The record file is authenticated** (VAIR A5). Each daemon's records carry an HMAC-SHA256 computed with a random key kept in
  the encrypted keystore (outside the workspace and outside the record directory) and bound to the daemon and the workspace.
  A file that was edited, forged, copied from another workspace or daemon, written by an older format, or sealed with another
  key fails verification and **nothing in it is trusted** (it is set aside as `*.tampered-*`, `*.corrupt-*` or
  `*.unauthenticated-*`, never read). Encryption alone would not prove ownership; a MAC proves "Velune wrote this". If the
  keystore is unavailable, records live in memory for the session only and nothing is read from or written to disk.
  Writes are atomic (unique temp file, flush, fsync, replace), serialized across Velune processes by a lock, merged with what
  other processes recorded, and owner-only on POSIX (on Windows the user-profile ACL applies). A replayed older file can
  only restore records Velune genuinely wrote, and each is still checked against the live daemon (id, fingerprint, labels).
- Compose: after `compose_up`, only resources that did **not** exist before the call (new ids) are recorded, so a
  project never takes ownership of existing or external resources. `compose_down` removes only a project whose
  containers are all proven Velune's; otherwise it refuses and each container can be removed separately (each with
  its own approval).

## Permissions

| Operation | MANUAL | PLAN | AUTO |
| --- | --- | --- | --- |
| reads (`ps`, `inspect`, `logs`, `health`, `stats`, `info`, `*_ls`, `*_inspect`, `compose_config/ps/logs`, `wait_ready`) | allow | allow | allow |
| create an ordinary non-privileged container | ask | deny | **allow** |
| `start` / `stop` / `restart` / `pause` / `unpause` of a container Velune created | ask | deny | **allow** |
| the same on any other container | ask | deny | **ask** |
| `container_remove` of an owned container | ask | deny | **allow** (not with `force` or `volumes`) |
| `container_remove` of any other container, or forced | ask | deny | **ask** |
| `image_pull` | ask (network) | deny | allow (network) |
| `image_tag`, `volume_create`, `network_create` | ask | deny | allow |
| `image_remove`, `volume_remove` | ask | deny | **ask** |
| `network_remove` of an owned, unused network | ask | deny | allow |
| `network_remove` of one in use or not owned; `network_connect/disconnect` involving a foreign resource | ask | deny | **ask** |
| `compose_up` of an ordinary file | ask | deny | allow |
| `compose_down`, `compose_stop` | ask | deny | **ask** |
| privileged, `cap_add`, host network, devices, non-loopback ports, a bind mount of the Docker socket or a secret path | **ask** | deny | **ask** |
| a bind mount outside the workspace | ask | deny | **deny** |
| anything against a remote daemon | **ask** | deny | **ask** |
| built-in networks (`bridge`, `host`, `none`) removal | deny | deny | deny |

Escalated options appear as extra actions with their own scopes (`docker.container.privileged`,
`docker.network.host`, `docker.network.exposed`, `docker.mount.host`, `docker.device.access`), so the approval
shows *why*. A session grant never covers them. Discovery (`integrations_list` / `integrations_enable`) grants
nothing.

## Compose

Compose runs through `docker compose` with a **forced project name** (`-p`, from the `project` argument or the
compose file's directory) and an argv that never carries model-controlled option text. Before `compose_up`, the
file is scanned (`compose_config` shows the same result): privileged containers, `cap_add`, `security_opt`,
host network / PID / IPC namespaces, joining another container's namespace, Docker-socket and host bind mounts
(including `driver_opts` devices), `devices`, **external volumes and networks**, `volumes_from`, and
`include` / `extends` (reported as unverified) each raise an extra high-risk action.

The scan has two passes and reports their union. A line classifier needs nothing but the text; a **structural pass** parses
the file with PyYAML's `safe_load` (never `load`; size, depth, node and document limits) and walks `services.*` and the
top-level `volumes`, `networks`, `configs`, `secrets` and `include`, so it sees what a line scan cannot: flow-style YAML
(`{privileged: true}`, `volumes: ["/:/host"]`), quoted keys, anchors and merge keys, JSON-formatted files, and bind sources
that are variables (`${HOME}:/h`). A file that does not parse, is too deep or has too many documents is *unverified*. Without
PyYAML (the `docker` extra installs it) a file using syntax the line scan cannot read is also reported unverified instead of
being waved through. The line pass is kept as a second opinion, so an environment variable spelled like a Compose key is
flagged too — a false positive costs one confirmation.

## Configuration

```toml
[resources.docker]
enabled = true
host = ""                 # explicit endpoint; empty = auto-detect (trusted workspaces only)
tls_ca = ""               # PATHS to certificates for a tcp:// endpoint
tls_cert = ""
tls_key = ""
timeout = 20.0            # seconds per request
max_log_lines = 500
max_output_chars = 16000
workspace_mounts = true   # false: even bind mounts inside the workspace need approval
compose_timeout = 600.0
```

## Development and testing

`tests/docker_fakes.py` is an in-memory Docker daemon (containers, images, volumes, networks, failure switches);
the connector takes an injected engine, so **no Docker is needed**:

```text
python -m pytest tests/test_docker_*.py tests/test_resource_authorization.py tests/test_docker_compose_*.py
VELUNE_DOCKER_LIVE=1 python -m pytest tests/test_docker_live.py   # optional, real disposable daemon
```

## Limitations

- Compose and `build` use the `docker` CLI (needs the CLI and the compose plugin); they run in the process
  working directory and go through the same gate.
- Image pulls use your existing `docker login` credentials; Velune never reads, stores or logs registry
  credentials. Very long pulls are bounded by the SDK's request timeout.
- Ownership records are per workspace and per daemon, in Velune's data directory, authenticated with a key held in the
  keystore: something that can read your keystore (your own user account) can forge one; something that can only write the
  file cannot. Records are reconciled when a daemon is connected, not continuously, and a stale process that never saw
  another process's release can write a released record back (the daemon's id and fingerprint still have to match).
- Windows/macOS/Linux connection selection is unit-tested; a real daemon was exercised only by the optional live
  test. Podman's Docker-compatible socket is untested.
- `exec`, `commit`, `export`/`import`, swarm, plugins and daemon configuration are not supported.
