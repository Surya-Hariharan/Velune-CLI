# Velune MCP Integration

> Use Velune's local tool council as a tool provider for Claude Desktop,
> VS Code, and any MCP-compatible client.

---

## What this enables

Velune implements the [Model Context Protocol](https://modelcontextprotocol.io/)
as both a **server** (Velune tools → external clients) and a **client**
(external MCP servers → Velune agents).

When running as a server, Claude Desktop or VS Code can call Velune's
codebase tools directly — giving cloud-based editors access to your
local filesystem, git history, and semantic code search without
sending your code to a third party for indexing.

**Tools exposed to MCP clients:**

| Category       | Tools                                                              |
|----------------|--------------------------------------------------------------------|
| Filesystem     | `read_file`, `read_directory`, `write_file`, `create_file`, `delete_file` |
| Search         | `grep_files`, `find_files`                                         |
| Code nav       | `semantic_code_search`, `symbol_search`, `go_to_definition`, `find_references` |
| Git            | `git_log`, `git_diff`, `git_blame`, `git_status`, `git_branch`, `git_commit`, `git_checkout` |
| Terminal       | `execute_command`, `terminal_history`                              |
| Web            | `web_fetch`                                                        |

---

## Start Velune as an MCP server

The server runs in stdio mode — the MCP client launches it as a subprocess.
You do not need to start it manually.

To verify the server works before wiring it into a client:

```bash
# Test in stdio mode (exits immediately with no input)
velune mcp serve
```

To start it against a specific project directory:

```bash
cd ~/projects/my-app
velune mcp serve
```

Velune loads `.velune/config.toml` from the working directory, so
running from inside your project gives the MCP client accurate
workspace context.

---

## Claude Desktop configuration

Find your Claude Desktop config file:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux:** `~/.config/Claude/claude_desktop_config.json`

Add a `velune` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "velune": {
      "command": "velune",
      "args": ["mcp", "serve"]
    }
  }
}
```

If `velune` is not on your PATH (e.g., installed in a virtualenv),
use the full path to the executable:

```json
{
  "mcpServers": {
    "velune": {
      "command": "/home/user/.venv/bin/velune",
      "args": ["mcp", "serve"],
      "env": {
        "PYTHONPATH": "/home/user/.venv/lib/python3.11/site-packages"
      }
    }
  }
}
```

To bind Velune to a specific project at startup, add the workspace flag:

```json
{
  "mcpServers": {
    "velune": {
      "command": "velune",
      "args": ["mcp", "serve", "--workspace", "/home/user/projects/my-app"]
    }
  }
}
```

Restart Claude Desktop fully after editing the config. You should see
a "velune" section with a tool count in the attachment menu.

---

## VS Code configuration

For VS Code with the
[Copilot Chat MCP extension](https://code.visualstudio.com/docs/copilot/chat/mcp-servers)
or [Continue.dev](https://continue.dev/), add to `.vscode/mcp.json`
in your project:

```json
{
  "servers": {
    "velune": {
      "type": "stdio",
      "command": "velune",
      "args": ["mcp", "serve"]
    }
  }
}
```

Or in your user `settings.json` to enable it globally:

```json
{
  "mcp.servers": {
    "velune": {
      "type": "stdio",
      "command": "velune",
      "args": ["mcp", "serve"]
    }
  }
}
```

---

## Tool reference

All tools use JSON arguments. The MCP client sends these automatically
when Claude invokes a tool — you do not write JSON manually.

### Filesystem tools

**`read_file`** — read a file by path

```json
{ "path": "src/auth/session.py" }
```

**`write_file`** — overwrite a file (creates parent dirs if needed)

```json
{ "path": "src/auth/session.py", "content": "..." }
```

**`read_directory`** — list files in a directory

```json
{ "path": "src/auth/", "recursive": false }
```

---

### Search tools

**`grep_files`** — regex search across the workspace

```json
{
  "pattern": "def authenticate",
  "path": "src/",
  "file_glob": "*.py"
}
```

**`semantic_code_search`** — vector similarity search over indexed code

```json
{ "query": "database connection pooling", "limit": 10 }
```

**`symbol_search`** — search for a class, function, or variable by name

```json
{ "symbol": "AuthManager", "kind": "class" }
```

---

### Git tools

**`git_diff`** — show uncommitted changes

```json
{ "staged": false }
```

**`git_log`** — commit history for a path

```json
{ "path": "src/auth/", "limit": 20 }
```

**`git_blame`** — line-by-line authorship

```json
{ "path": "src/auth/session.py" }
```

---

### Terminal tool

**`execute_command`** — run a shell command inside the workspace sandbox

```json
{ "command": "pytest tests/test_auth.py -v" }
```

Commands run inside a subprocess sandbox with explicit workspace
boundaries. Commands that attempt to write outside the workspace
root are rejected.

---

## Connect Velune to an external MCP server

Velune can also act as an MCP *client*, consuming tools from other
MCP servers and making them available to the Velune agent council.

Connect to an external server and list its tools:

```bash
velune mcp connect https://mcp.example.com/sse my-server
```

This connects via SSE, discovers available tools, and prints:

```text
✓ Connected to my-server successfully!
Exposed Tools (3):
  - my-server_search_docs: Search the documentation index
  - my-server_create_ticket: Create a Jira ticket
  - my-server_query_db: Run a read-only SQL query
```

Once connected inside a running Velune session, council agents can
invoke these tools the same way they invoke built-in tools.

---

## Persistent external server config

To auto-connect to external MCP servers every time Velune starts,
add them to `velune.toml` in your project root:

```toml
[mcp.servers]
my-docs = "https://mcp.example.com/sse"
jira    = "https://mcp.internal/sse"
```

Velune loads these at startup and makes their tools available to the
council without requiring a manual `velune mcp connect` each time.

---

## Troubleshooting

**Velune does not appear in the Claude Desktop tool list**

- Confirm `velune mcp serve` runs without error in a terminal first.
- Validate your JSON config with a linter — a trailing comma or missing
  quote silently prevents the entry from loading.
- Quit and relaunch Claude Desktop (Cmd+Q / Alt+F4, not just close the window).

**`command not found: velune` error in Claude Desktop logs**

Claude Desktop inherits a minimal PATH. Use the full path to the
velune binary:

```bash
which velune   # copy this path into the config
```

**Tool calls fail with "workspace not found" or permission errors**

Add the `--workspace` flag to point Velune at the correct project:

```json
"args": ["mcp", "serve", "--workspace", "/absolute/path/to/project"]
```

**`execute_command` tool is blocked**

This tool requires the `terminal.execute` permission. It is enabled
by default but can be restricted via `velune.toml`:

```toml
[tools]
allow_execute = false
```

**Slow tool responses on first call**

The first call after startup triggers workspace indexing. Subsequent
calls are fast. You can pre-index manually:

```bash
velune workspace init
```
