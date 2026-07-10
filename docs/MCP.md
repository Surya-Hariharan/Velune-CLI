# MCP Integration Guide

Velune is both an MCP (Model Context Protocol) **server** — exposing its own
tools to other MCP clients — and an MCP **client** — pulling in tools from
external MCP servers. Both directions run through
[`velune/mcp/`](../velune/mcp/). For trust-boundary details, see
[SECURITY.md § MCP trust gating](../SECURITY.md#mcp-trust-gating).

---

## 1. Running Velune as a server

Expose Velune's local tool council (council reasoning, memory queries,
symbol search, blast-radius estimation) to Claude Desktop, VS Code, or any
other MCP-capable client, without sending your code to a third party:

```bash
velune mcp serve
```

This runs `velune/mcp/server.py:VeluneMCPServer` over **stdio** — the
standard transport for editor/desktop-app integrations, where the client
launches Velune as a subprocess and talks over its stdin/stdout.

An HTTP/SSE mode is also available for network-reachable setups: it binds
to `127.0.0.1:7777` by default, requires bearer-token authentication, and
refuses to bind to a non-loopback address unless explicitly told to allow
remote access. Don't expose this port beyond localhost without
understanding the authentication story end-to-end.

**Permissions are read-only by default.** Velune's own tools are exposed to
external MCP clients without write or execute permission unless the
`VELUNE_MCP_ALLOW_MUTATIONS=1` environment variable is set — so connecting
Claude Desktop to your Velune server doesn't, by default, hand it the
ability to edit files or run commands on your machine. Enable mutations
only for clients and environments where you want that.

The server also enforces:

- A token-bucket rate limiter, so a misbehaving client can't hammer it.
- A workspace validator that rejects path-traversal attempts in tool
  arguments — a request can't use `../` tricks to reach outside the
  workspace it was granted.

---

## 2. Connecting to an external MCP server

To pull tools from someone else's MCP server into your Velune session:

```bash
velune mcp connect <server-url> <name>
```

Or, inside the REPL, declare the server once and connect by name:

```
/mcp connect <name>
```

### Declaring servers

Velune reads server definitions from, in order:

1. `.mcp.json` in the workspace root
2. `~/.mcp.json` (user-level, applies across all workspaces)
3. `velune.toml [mcp.servers]`
4. `MCP_SERVERS_JSON` environment variable

All of this is handled by `velune/mcp/registry.py:MCPServerRegistry`, the
single source of truth for multi-server management — it loads every
configured source, connects/disconnects all servers concurrently, and
routes tool calls by a qualified `{server}_{tool}` name so two servers can
expose a same-named tool without colliding.

A minimal `.mcp.json`:

```json
{
  "mcpServers": {
    "my-server": {
      "transport": "stdio",
      "command": "node",
      "args": ["./my-mcp-server.js"]
    }
  }
}
```

Config changes are picked up without a restart — the registry watches file
mtimes and hot-reloads roughly every 30 seconds.

### Transports

All four are implemented as thin connection classes under
`velune/mcp/transports/`, selected by `transport` in the server config:

| Transport | Class | Typical use |
| --- | --- | --- |
| `stdio` | `StdioConnection` | Local subprocess servers (most common) |
| `sse` | `SSEConnection` | Server-sent events over HTTP |
| `http` | `HTTPConnection` | Plain request/response HTTP servers |
| `websocket` | `WebSocketConnection` (`ws://` / `wss://`) | Bidirectional streaming servers |

---

## 3. Trust gating for outbound connections

Two independent layers protect against a malicious or misconfigured MCP
server:

### SSRF guard (network-level)

`velune/mcp/security.py:validate_mcp_url()` runs on every outbound MCP
connection URL and:

- Rejects URLs with embedded credentials (`user:pass@host`).
- Rejects non-`http(s)` schemes.
- Resolves DNS and blocks cloud-metadata endpoints (AWS/GCP/Azure/Alibaba
  IMDS) and link-local addresses — the classic SSRF-to-cloud-credential
  attack path.
- Re-resolves after any redirect to defeat DNS-rebinding.
- Supports an optional host allowlist for locked-down environments.

Loopback and LAN addresses are explicitly permitted — Velune is local-first
by design, so connecting to a server on `localhost` or your home network is
expected, not blocked.

### Workspace trust (project-config level)

When you open a workspace that has its own `.mcp.json` or `velune.toml
[mcp.servers]`, and that workspace hasn't been trusted before, the REPL
prompts you (`VeluneREPL._ensure_workspace_trust`). If you decline:

- **Project-level** MCP config (`.mcp.json`, `velune.toml [mcp.servers]`
  in that workspace) is **not** loaded.
- **User-level** config (`~/.mcp.json`) still loads regardless — servers
  you've configured for yourself aren't gated by a project's trust state.

This means cloning an unfamiliar repo and opening it in Velune does not
silently connect you to whatever MCP servers that repo's `.mcp.json`
declares — you have to explicitly trust the workspace first. Manage trust
with `velune trust` from the terminal.

---

## 4. The `/mcp` command surface

| Command | Does |
| --- | --- |
| `/mcp` or `/mcp servers` | List configured/connected servers and their status |
| `/mcp tools [server]` | List tools exposed by all servers, or one named server |
| `/mcp resources [server]` | List MCP resources (not tools) exposed |
| `/mcp connect <name>` | Connect a server already declared in config |
| `/mcp disconnect <name>` | Disconnect a server |
| `/mcp refresh <name>` | Re-fetch a server's tool/resource list |

There is intentionally no `/mcp add` — adding a server means editing
`.mcp.json` (or `velune.toml`), which keeps server definitions in a file
that's reviewable in a PR rather than mutated from inside a chat session.

---

## 5. Troubleshooting

- **A tool from an external server isn't showing up** — check `/mcp
  servers` for connection status first; a server that failed to connect
  won't have its tools listed. `/mcp refresh <name>` re-fetches after
  fixing a transient issue.
- **A workspace's MCP servers aren't loading** — check whether the
  workspace is trusted (`velune trust`); declining the trust prompt
  silently skips project-level `.mcp.json`, which is easy to forget you did.
- **Connecting to a remote MCP server fails with a validation error** —
  `validate_mcp_url()` is almost certainly doing its job (blocking a
  metadata endpoint, a redirect to an unexpected host, or a URL with
  embedded credentials). This is a security control, not a bug — fix the
  URL rather than looking for a way around the check.
- **Velune-as-server isn't editing files even though it's connected** —
  check whether `VELUNE_MCP_ALLOW_MUTATIONS` is set in the environment the
  server process runs in; by default it's read-only.
