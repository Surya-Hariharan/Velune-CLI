# MCP Integration

*How Velune CLI speaks the Model Context Protocol — as a server, as a client, and how outbound connections are trust-gated.*

Velune CLI is both an MCP **server** — exposing its own tools to other MCP
clients — and an MCP **client** — pulling tools in from external MCP
servers. Both directions live under [`velune/mcp/`](../velune/mcp/). For the
security-policy framing of the trust boundary described in [§4](#4-trust-gating-for-outbound-connections),
see [SECURITY.md § MCP trust gating](../SECURITY.md#mcp-trust-gating). See
also [README.md](../README.md) for the rest of the project.

---

## Contents

- [1. Running Velune CLI as a server](#1-running-velune-cli-as-a-server)
- [2. Connecting to an external MCP server](#2-connecting-to-an-external-mcp-server)
- [3. Declaring servers in `.mcp.json`](#3-declaring-servers-in-mcpjson)
- [4. Trust gating for outbound connections](#4-trust-gating-for-outbound-connections)
- [5. The `/mcp` command surface](#5-the-mcp-command-surface)
- [6. Troubleshooting](#6-troubleshooting)

---

## 1. Running Velune CLI as a server

Expose Velune CLI's local tools (council reasoning, memory search, symbol
lookup, blast-radius estimation, plus anything registered in the tool
registry) to Claude Desktop, VS Code, or any other MCP-capable client,
without sending your code to a third party:

```bash
velune mcp serve
```

This runs `velune/mcp/server.py:VeluneMCPServer` over **stdio**, using the
`mcp` SDK's own `mcp.server.stdio.stdio_server()` transport — the standard
transport for editor/desktop-app integrations, where the client launches
Velune CLI as a subprocess and talks over its stdin/stdout. This is the only
transport `velune mcp serve` currently starts.

> **Not yet wired to the CLI.** `VeluneMCPServer` also implements
> `run_http()` — a hand-rolled `aiohttp`-based HTTP/SSE listener (not the
> `mcp` SDK's own HTTP transport) that binds to `127.0.0.1:7777` by default,
> refuses to bind to a non-loopback host unless `allow_remote=True` is
> passed, and requires a bearer token (`Authorization: Bearer <token>` or
> `X-Velune CLI-Token`, checked with `secrets.compare_digest`) on every request
> to its `/sse` and `/tools` routes. Nothing in `velune mcp serve` or the
> rest of the CLI currently calls `run_http()` — it exists as a method on
> the class, callable programmatically, but there's no `--http` /
> `--port` / `--allow-remote` flag yet. Don't rely on it being reachable
> from the command line.

**Permissions are read-only by default.** Tools pulled from Velune CLI's own
tool registry are exposed to external MCP clients with
`FILESYSTEM_READ` / `GIT_READ` / `NETWORK_ACCESS` only, unless the
`VELUNE_MCP_ALLOW_MUTATIONS=1` environment variable is set (or
`allow_mutations=True` is passed to `VeluneMCPServer`) — so connecting
Claude Desktop to your Velune CLI server doesn't, by default, hand it the
ability to write files or run commands on your machine. The four
Velune CLI-native tools (`velune_ask`, `velune_search_memory`,
`velune_get_symbols`, `velune_estimate_blast_radius`) are always available
regardless of this flag; they're read-only by construction.

The server also enforces:

- A **token-bucket rate limiter** (`RateLimiter`, 60 calls/minute by
  default), so a misbehaving client can't hammer it.
- A **workspace validator** (`WorkspaceValidator`) that rejects any
  `workspace_path` argument resolving outside the allowed workspace list —
  a tool call can't use `../` tricks to reach outside the workspace it was
  granted.

---

## 2. Connecting to an external MCP server

Two different entry points, for two different situations:

```bash
velune mcp connect <server-url> <name>
```

`velune mcp connect` is a one-off connect-and-list: it always treats
`<server-url>` as an **SSE** endpoint (`VeluneMCPClient` hard-codes
`transport=SSE` for the raw-URL form) and does not persist anything to
config. Use it to try a server or check what tools it exposes.

For `stdio`, `http`, or `ws`/`wss` servers — or anything you want to
reconnect to across sessions — declare it once in `.mcp.json` (see
[§3](#3-declaring-servers-in-mcpjson)) and connect by name, either from the
REPL:

```
/mcp connect <name>
```

or by letting it auto-connect on session start (the REPL connects every
configured, trusted server automatically when it starts up).

---

## 3. Declaring servers in `.mcp.json`

`velune/mcp/registry.py:MCPServerRegistry` is the single source of truth
for multi-server management. It loads every configured source, connects or
disconnects all servers concurrently, and routes tool calls by a qualified
`{server}_{tool}` name so two servers can expose a same-named tool without
colliding.

### Sources, in load order

1. `<workspace>/.mcp.json` — only loaded if the workspace is **trusted**.
2. `~/.mcp.json` — user-level, always loaded regardless of workspace trust.
3. `velune.toml [mcp.servers]` — only loaded if the workspace is trusted.
4. `MCP_SERVERS_JSON` environment variable.

Sources 1–3 are merged with **later sources overwriting a same-named entry
from an earlier one** (so a `velune.toml` entry wins over a same-named
`.mcp.json` entry). The environment variable is lowest priority: it only
fills in server names that aren't already defined by a file-based source.

Config changes are picked up without a restart — the registry watches
`.mcp.json` and `velune.toml` mtimes and hot-reloads roughly every 30
seconds.

### `.mcp.json` shape

The file is a **flat object** mapping server name → server entry directly
at the top level — there is no `mcpServers` wrapper key:

```json
{
  "my-server": {
    "command": "node",
    "args": ["./my-mcp-server.js"]
  }
}
```

Transport selection per entry:

- If the entry has a `command` field, it's treated as **stdio** — no
  `type` needed.
- Otherwise the `type` field picks the transport: `sse` (the default when
  `type` is omitted entirely), `http`, or `ws` / `websocket` (both
  normalize to `ws`).

<details>
<summary><strong>One example per transport</strong></summary>

```json
{
  "filesystem": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    "env": { "LOG_LEVEL": "debug" }
  },
  "github": {
    "type": "sse",
    "url": "https://mcp.github.com/sse"
  },
  "my-api": {
    "type": "http",
    "url": "https://api.example.com/mcp",
    "headers": { "Authorization": "Bearer ${API_TOKEN}" }
  },
  "my-ws-server": {
    "type": "ws",
    "url": "ws://localhost:8765/mcp",
    "headers": { "Authorization": "Bearer ${API_TOKEN}" }
  }
}
```

`velune.toml` accepts the same shape under `[mcp.servers]`, plus a string
shorthand for quick SSE entries:

```toml
[mcp.servers]
myserver = "https://mcp.example.com/sse"   # shorthand — always SSE

[mcp.servers.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
```

</details>

<details>
<summary><strong>Transport → connection class</strong></summary>

All four are thin connection classes under `velune/mcp/transports/`,
instantiated by `velune/mcp/transports/factory.py:make_connection()`:

| `type` | Class | Notes |
| --- | --- | --- |
| *(inferred from `command`)* | `StdioConnection` | Local subprocess servers (most common); uses the `mcp` SDK's `stdio_client`. |
| `sse` *(default)* | `SSEConnection` | Server-sent events, via the `mcp` SDK's `sse_client`. |
| `http` | `HTTPConnection` | Tries the `mcp` SDK's `streamablehttp_client` first, falls back to a plain `httpx` JSON-RPC client if unavailable. |
| `ws` / `websocket` | `WebSocketConnection` | Hand-rolled over the raw `websockets` library (not an `mcp`-SDK transport) — a pure outbound `ws://` / `wss://` JSON-RPC 2.0 client. |

</details>

---

## 4. Trust gating for outbound connections

Two independent layers protect against a malicious or misconfigured MCP
server.

### SSRF guard (network-level)

`velune/mcp/security.py:validate_mcp_url()` runs on every outbound MCP
connection URL (WebSocket URLs are normalized `ws://`→`http://` /
`wss://`→`https://` before the same check runs) and:

- Rejects URLs with embedded credentials (`user:pass@host`).
- Rejects non-`http(s)` schemes.
- **Always** blocks cloud-metadata hosts (AWS/Azure/GCP IMDS, ECS task
  metadata, Alibaba Cloud metadata) and link-local addresses — the classic
  SSRF-to-cloud-credential attack path — checked both against the literal
  hostname and against every address the hostname resolves to via
  `getaddrinfo`, so a hostname that resolves to more than one IP can't
  smuggle a blocked one past the check.
- Supports an optional host **allowlist** (`[mcp] allowed_hosts` in
  `velune.toml`) for locked-down environments — when set, only listed
  hosts may be connected to.

Loopback and LAN addresses are explicitly permitted — Velune CLI is local-first
by design, so connecting to a server on `localhost` or your home network is
expected, not blocked.

> This check runs once, at connect time, against the URL as configured. It
> does not re-validate on HTTP redirects mid-connection — treat it as a
> pre-connection gate, not a persistent network firewall.

### Workspace trust (project-config level)

When you open a workspace that has its own `.mcp.json` or
`velune.toml [mcp.servers]`, and that workspace hasn't been trusted before,
the REPL prompts you (`VeluneREPL._ensure_workspace_trust`). If you
decline:

- **Project-level** MCP config (`.mcp.json`, `velune.toml [mcp.servers]`
  in that workspace) is **not** loaded.
- **User-level** config (`~/.mcp.json`) still loads regardless — servers
  you've configured for yourself aren't gated by a project's trust state.

This means cloning an unfamiliar repo and opening it in Velune CLI does not
silently connect you to whatever MCP servers that repo's `.mcp.json`
declares — you have to explicitly trust the workspace first. Manage trust
with `velune trust add|list|forget` from the terminal.

---

## 5. The `/mcp` command surface

| Command | Does |
| --- | --- |
| `/mcp` or `/mcp servers` | List configured/connected servers and their status |
| `/mcp tools [server]` | List tools exposed by all servers, or one named server |
| `/mcp resources [server]` | List MCP resources (not tools) exposed |
| `/mcp connect <name>` | Connect a server already declared in config |
| `/mcp disconnect <name>` | Disconnect a server |
| `/mcp refresh <name>` | Re-fetch a server's tool/resource list |

> There is intentionally no `/mcp add`. Adding a server means editing
> `.mcp.json` (or `velune.toml`), which keeps server definitions in a file
> that's reviewable in a PR rather than mutated from inside a chat session.

Note that `/mcp connect` takes a server **name** already present in config
— it is not the same as `velune mcp connect <url> <name>` on the CLI,
which takes a raw SSE URL and doesn't touch config at all (see
[§2](#2-connecting-to-an-external-mcp-server)).

---

## 6. Troubleshooting

- **A tool from an external server isn't showing up** — check `/mcp
  servers` for connection status first; a server that failed to connect
  won't have its tools listed. `/mcp refresh <name>` re-fetches after
  fixing a transient issue.
- **A workspace's MCP servers aren't loading** — check whether the
  workspace is trusted (`velune trust list`); declining the trust prompt
  silently skips project-level `.mcp.json`, which is easy to forget you did.
- **Connecting to a remote MCP server fails with a validation error** —
  `validate_mcp_url()` is almost certainly doing its job (blocking a
  metadata endpoint or a URL with embedded credentials, or the host isn't
  on your configured allowlist). This is a security control, not a bug —
  fix the URL rather than looking for a way around the check.
- **Velune CLI-as-server isn't editing files even though it's connected** —
  check whether `VELUNE_MCP_ALLOW_MUTATIONS` is set in the environment the
  server process runs in; by default it's read-only.
- **`velune mcp serve` only seems to work over stdio** — that's current
  behavior, not a bug; see the callout in [§1](#1-running-velune-cli-as-a-server).
