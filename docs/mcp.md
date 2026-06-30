# Velune MCP Integration Guide

Velune implements the [Model Context Protocol (MCP)](https://modelcontextprotocol.io) as both a **server** (exposing its council, memory, and code analysis to MCP clients) and a **client** (consuming external MCP servers from within the REPL).

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Start — Claude Desktop](#quick-start--claude-desktop)
3. [Configuring MCP Clients (`.mcp.json`)](#configuring-mcp-clients-mcpjson)
4. [velune.toml `[mcp.servers]` Syntax](#velunetoml-mcpservers-syntax)
5. [Environment Variable Discovery (`MCP_SERVERS_JSON`)](#environment-variable-discovery-mcp_servers_json)
6. [`/mcp` REPL Commands](#mcp-repl-commands)
7. [Environment Variables](#environment-variables)
8. [Plugin MCP Shipping](#plugin-mcp-shipping)
9. [Available Prompts](#available-prompts)
10. [Sampling — Council Generation via MCP](#sampling--council-generation-via-mcp)

---

## Overview

| Role | What Velune does |
|------|-----------------|
| **MCP Server** | Exposes `velune_ask`, `velune_search_memory`, `velune_get_symbols`, and `velune_estimate_blast_radius` tools, plus built-in prompts, over stdio or HTTP/SSE |
| **MCP Client** | Connects to external MCP servers (filesystem, browser, database, etc.) and makes their tools available to the Velune council during your REPL session |

---

## Quick Start — Claude Desktop

Run Velune as an MCP server so Claude Desktop can call its tools:

```json
{
  "mcpServers": {
    "velune": {
      "command": "velune",
      "args": ["mcp", "serve"],
      "env": {
        "VELUNE_MCP_ALLOW_MUTATIONS": "0"
      }
    }
  }
}
```

Save this to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows), then restart Claude Desktop.

---

## Configuring MCP Clients (`.mcp.json`)

Place `.mcp.json` in your project root (or `~/.mcp.json` for user-global servers). Velune loads project-level configs only in trusted workspaces (`velune trust`).

### stdio transport (local subprocess)

```json
{
  "filesystem": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    "env": {}
  },
  "uvx-server": {
    "command": "uvx",
    "args": ["mcp-server-git", "--repository", "."]
  }
}
```

### SSE transport (remote / hosted)

```json
{
  "my-remote": {
    "url": "https://mcp.example.com/sse",
    "headers": {
      "Authorization": "Bearer your-token"
    }
  }
}
```

### HTTP transport (streamable HTTP / JSON-RPC)

```json
{
  "my-http": {
    "url": "https://mcp.example.com/rpc",
    "type": "http",
    "headers": {
      "X-Api-Key": "secret"
    }
  }
}
```

### WebSocket transport

```json
{
  "my-ws": {
    "url": "wss://mcp.example.com/ws"
  }
}
```

---

## `velune.toml` `[mcp.servers]` Syntax

```toml
[mcp.servers]
# URL shorthand (SSE by default)
my-remote = "https://mcp.example.com/sse"

# Full config
[mcp.servers.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
```

---

## Environment Variable Discovery (`MCP_SERVERS_JSON`)

Set `MCP_SERVERS_JSON` to a JSON object to register servers without writing a file — useful for ephemeral environments or CI:

```bash
export MCP_SERVERS_JSON='{"filesystem": {"command": "uvx", "args": ["mcp-server-filesystem", "/tmp"]}}'
velune
```

Multiple servers:

```bash
export MCP_SERVERS_JSON='{
  "fs":  {"command": "uvx", "args": ["mcp-server-filesystem", "/"]},
  "git": {"command": "uvx", "args": ["mcp-server-git", "--repository", "."]}
}'
```

The format is identical to `.mcp.json`: each key is the server name and the value is a config dict. Environment-sourced servers are always trusted and are loaded at startup alongside file-based configs.

---

## `/mcp` REPL Commands

| Command | Description |
|---------|-------------|
| `/mcp servers` | List all configured MCP servers with their state, transport, and tool counts |
| `/mcp tools [server]` | List all tools (optionally filtered to one server) |
| `/mcp resources [server]` | List all resources offered by connected servers |
| `/mcp connect <name>` | Connect a server that is currently disconnected |
| `/mcp disconnect <name>` | Disconnect a server |
| `/mcp refresh <name>` | Re-fetch the tool list from a connected server |

**Hot-reload:** Velune watches `.mcp.json` and `velune.toml` in the background (every 30 seconds). When a change is detected, new servers are connected and removed servers are disconnected automatically — no REPL restart needed.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `VELUNE_MCP_ALLOW_MUTATIONS` | `0` | Set to `1` to allow external MCP clients to invoke write/execute tools (filesystem write, git write, terminal). Off by default. |
| `VELUNE_MCP_AUTH_TOKEN` | auto-generated | Bearer token required by the HTTP/SSE transport. Auto-generated per process if not set; shown in the server log on startup. |
| `MCP_SERVERS_JSON` | _(unset)_ | JSON object of MCP server configs loaded at startup alongside `.mcp.json`. |

---

## Plugin MCP Shipping

A Velune plugin can bundle its own MCP server by placing a `.mcp.json` file at the plugin root:

```
~/.velune/plugins/my-plugin/
  plugin.json
  .mcp.json          ← server configs shipped with this plugin
  commands/
  skills/
```

Plugin servers are namespaced as `{plugin-name}:{server-name}` (e.g. `my-plugin:filesystem`) to prevent collisions. They are automatically wired into the registry when the plugin loads.

---

## Available Prompts

When Velune runs as an MCP server, it exposes three built-in prompt templates that MCP clients (e.g. Claude Desktop) can request via `prompts/list` and `prompts/get`:

### `session_context`

Returns the current workspace path and active model configuration.

**Arguments:** `workspace_path` (optional override)

**Example response:**
```
Workspace: /home/user/my-repo
Velune version: 0.9.4
Active model: claude-opus-4-8
```

### `memory_recall`

Returns recent relevant interactions from Velune's memory store.

**Arguments:** `query` (optional), `limit` (optional, default 5)

**Example response:**
```
- [working] Refactored the auth module last Tuesday
- [semantic] Found 3 usages of deprecated login() function
```

### `repository_summary`

Returns the repository file count and language breakdown.

**Arguments:** `workspace_path` (optional override)

**Example response:**
```
Repository: /home/user/my-repo
Total files: 142
  python: 89 files
  markdown: 21 files
  yaml: 18 files
  json: 9 files
  toml: 5 files
```

---

## Sampling — Council Generation via MCP

Velune's MCP server supports `sampling/createMessage` requests, routing them through Velune's multi-model council to generate a response.

This allows MCP clients to delegate analysis tasks to Velune's council. Send a standard JSON-RPC request:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "sampling/createMessage",
  "params": {
    "messages": [
      {
        "role": "user",
        "content": {"type": "text", "text": "Summarise the blast radius of changing velune/mcp/registry.py"}
      }
    ],
    "maxTokens": 1024
  }
}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "role": "assistant",
    "content": {"type": "text", "text": "Changing registry.py affects ..."},
    "model": "velune-council",
    "stopReason": "endTurn"
  }
}
```

The council runs with a 30-second wall-clock budget and one review cycle to keep latency low.
