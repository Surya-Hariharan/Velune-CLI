# Filesystem Tools

*How Velune's agent reads, creates, edits, moves and deletes files in your
repository, what keeps that safe, and how to extend it.*

The agent changes files through its own tool loop: the model asks for a tool
call, Velune checks it against the permission policy, runs it, and sends the
result back so the model can decide what to do next. This page covers the
filesystem part of that loop. For execution modes in day-to-day use see the
[usage guide](usage-guide.md#3-choosing-how-much-velune-may-do); for the threat
model see [SECURITY.md](../SECURITY.md).

---

## Contents

- [The tools](#the-tools)
- [How a tool call runs](#how-a-tool-call-runs)
- [What the model gets back](#what-the-model-gets-back)
- [Safety model](#safety-model)
- [Undoing a turn: /rewind](#undoing-a-turn-rewind)
- [Provider support](#provider-support)
- [Testing](#testing)
- [Adding a filesystem tool](#adding-a-filesystem-tool)

---

## The tools

All paths are relative to the workspace root (absolute paths inside it also work).

| Tool | Arguments | What it does |
| :--- | :--- | :--- |
| `read_file` | `file_path`, `start_line?`, `end_line?` | Returns the file, or numbered lines for a range or a large file (about 14 000 characters per call, with a "continue at line N" marker). |
| `read_directory` | `directory_path` | Lists a folder, hiding what the ignore rules exclude. |
| `find_files` | `pattern`, `directory?` | Finds files by name (glob). |
| `grep_files` | `pattern`, `directory?`, `file_pattern?` | Searches file contents. Secret files such as `.env` are excluded by the default ignore rules. |
| `edit_file` | `file_path`, `old_string`, `new_string`, `replace_all?`, `expected_sha256?` | Replaces an exact string. It must match exactly one place unless `replace_all` is true. This is the preferred way to change part of a file. |
| `write_file` | `file_path`, `content`, `expected_sha256?` | Writes a whole file, creating it and any missing folders. |
| `create_file` | `file_path` | Creates an empty file. Fails if it already exists. |
| `delete_file` | `file_path` | Deletes a file (not a folder). |
| `create_directory` | `path` | Creates a folder and any missing parents. |
| `delete_directory` | `path`, `recursive?` | Deletes a folder. A folder with contents needs `recursive=true`, which is high-risk. |
| `move_path` | `source`, `destination`, `overwrite?` | Moves or renames a file or folder. `overwrite` replaces an existing destination file. |

### `edit_file` in detail

- **Exact matching only.** There is no fuzzy fallback, because a fuzzy match can
  silently change the wrong lines. `old_string` must match the file
  character for character, including indentation.
- **Unique by default.** When `old_string` occurs more than once, the call is
  refused with the count and line numbers (`matches 2 places (lines 2, 6)`). The
  model then adds surrounding lines or sets `replace_all`.
- **Helpful failures.** When nothing matches, the error names the closest line,
  or says the text exists but its whitespace differs. An empty `old_string`, or
  `old_string == new_string`, is refused.
- **Whole-file view.** The tool reads the file from disk itself (up to 20 MB), so
  an edit near the end of a large file works even though `read_file` showed only
  part of it.
- An empty `new_string` deletes the matched text.

---

## How a tool call runs

```
model ──tool call──▶ ToolLoopRunner ──▶ authorize_and_execute ──▶ tool.execute ──▶ mutation layer ──▶ disk
  ▲                    (tool_loop.py)     (describe_actions →       (edit.py,         (atomic write,
  └────── result: summary + diff + sha256 ◀── PermissionGate)       write.py, dirs.py)  verify, journal)
```

1. **Registration.** `velune/tools/subsystems.py` builds the registry once per
   session. The read, write and edit tools share one `FileVersionTracker`.
2. **Schemas.** `ToolLoopRunner.build_tool_definitions()`
   (`velune/orchestration/tool_loop.py`) turns each tool's `get_schema()` into an
   OpenAI-format function definition. Provider adapters translate it for
   Anthropic, Gemini and Cohere.
3. **The loop.** The runner sends the conversation and the tools, executes the
   calls the model asks for, appends each result as a `tool` message, and repeats
   until the model answers in text. The turn limit is `max_tool_turns`
   (10 by default, at least 30 in Auto or while executing a plan). A failed call
   is returned to the model as an error result rather than aborting the turn.
   An identical call that already failed is not re-run, unless a later call on the
   same path succeeded (for example a re-read after a conflict).
4. **Authorization.** `authorize_and_execute` (`velune/tools/base/tool.py`) asks
   the tool to `describe_actions` (create, modify, delete, move, each with a
   resolved path), and the `PermissionGate` (`velune/permissions/gate.py`) decides
   ALLOW, DENY or ASK. With no gate, any change is refused.
5. **Execution.** The tool resolves its path against the same boundary and
   changes the file through `velune/tools/filesystem/mutation.py`.

---

## What the model gets back

Every change returns a stable first line, then details:

```
edit_file OK src/app.py (+1 -1) verified sha256=3f1c…
--- a/src/app.py
+++ b/src/app.py
@@ -4,3 +4,3 @@
 def sub(a, b):
-    return a + b
+    return a - b
```

- `verified` means the file was hashed after the write and matched what was
  written. For moves the destination must exist, the source must be gone and
  the content hash must be unchanged; for deletes and folders the result is
  checked with `stat`. If a check fails, the call is reported as an error.
- `sha256` is the new content's hash. The model can pass it back as
  `expected_sha256` to make sure nothing changed in between.
- The diff is capped at about 6 000 characters.

---

## Safety model

### Workspace boundary

Paths are resolved, following symlinks and junctions, against the workspace root.
Anything that resolves outside it, whether by `../`, an absolute path or a
link, is an *outside-workspace* action:

- **Manual:** the user is asked.
- **Auto:** refused.

The mutation layer re-checks the boundary immediately before replacing a file, so
a folder swapped for a link after approval is still caught.

### Execution modes

| Mode | Reads | Ordinary changes | High-risk changes | Outside the workspace |
| :--- | :--- | :--- | :--- | :--- |
| Manual | allowed | asked | asked | asked |
| Plan | allowed | refused (plan files only) | refused | refused |
| Auto | allowed | allowed | asked | changes refused |

High-risk changes include:
- recursive folder deletion and deleting the workspace root;
- secret files (`.env`, keys, `~/.ssh`), which also need approval to read;
- the **protected paths** below.

High-risk approvals are never covered by a session-wide "always allow".

The same tools are offered to external clients by Velune's MCP server, under a
gate that cannot ask anyone:
- changes are refused unless `VELUNE_MCP_ALLOW_MUTATIONS=1` is set;
- with it set, changes run as in Auto, except that anything that would ask
  (high-risk, protected or outside the workspace) is refused.

See the [MCP guide](mcp.md).

### Protected paths

Changing these is high-risk because they make other programs run code:

| Path | Why |
| :--- | :--- |
| anything under `.git/` (`hooks/`, `config`, …) | git runs hooks and honours config such as `core.hooksPath` and `core.fsmonitor` |
| `.velune/hooks.json` | Velune runs the commands in its hook configuration |
| `velune.toml` | it can define hooks |

Reading them is ordinary. `.gitignore`, `.github/` and similar names are not
protected. The protection covers the file tools only: a shell command run through
`execute_command` is governed by the command policy instead.

### Atomic writes

New content goes to a temporary file in the same folder, which is flushed,
`fsync`-ed and swapped in with `os.replace`. The target is either the old file or
the new one, never half-written, and the temporary file is removed if anything
fails. Permission bits are kept.

The folder entry is not `fsync`-ed, so after a power cut the rename itself may be
lost; that is the full guarantee. On Windows, a replace that is briefly blocked by
another program holding the file is retried a few times.

### Encoding and line endings

An existing file keeps its:
- encoding (UTF-8, UTF-16, Windows-1252),
- byte-order mark,
- line-ending style (LF, CRLF, or the dominant style of a mixed file),
- trailing-newline status.

Text from the model is converted to match, so a one-line edit to a CRLF file
changes one line, not every line ending. New files are written exactly as given.
Characters the file's encoding cannot hold are refused rather than silently
replaced.

### Conflict detection

The session's `FileVersionTracker` remembers the hash of each file as the agent
last read or wrote it. If the file has changed since (you saved it in your editor,
a formatter rewrote it), `edit_file` and `write_file` refuse with a *Conflict*
error and the agent is told to read it again. The hash is checked once more right
before the swap.

A file the agent has never read is not tracked. An `edit_file` on it still works
from its current content.

---

## Undoing a turn: /rewind

Every change in an agent turn (and every Council edit) is recorded with what was
there before. `/rewind` puts the last turn's changes back, newest first: it
restores edited and deleted files, removes created files and folders, moves files
back, and re-creates deleted folders.

- A file that changed again after the turn is **skipped and reported**, never
  overwritten.
- Files or folder trees over 20 MB are not kept, so their deletion cannot be
  rewound; the tool result says so when that happens.
- The journal lives in memory for the last turn only. `/undo` is different: it
  reverts the last Velune git commit.

---

## Provider support

Filesystem tools need native function calling.

| Provider | Tool calling | Notes |
| :--- | :--- | :--- |
| OpenAI, Groq, OpenRouter, xAI, Meta, Together, Fireworks | yes | OpenAI wire format, streamed tool calls |
| Anthropic | yes | translated to `tool_use` / `tool_result` |
| Google Gemini | yes | translated to `functionDeclarations`; filesystem schemas avoid keywords outside Gemini's schema subset |
| Cohere | yes | translated to `parameter_definitions` |
| Mistral, DeepSeek | yes | OpenAI wire format |
| Ollama | yes | depends on the model supporting tools |
| OpenAI-compatible servers (vLLM, …) | yes | depends on the server and model |
| LM Studio | yes | tools and streamed tool calls are sent and parsed; verified against a mocked server, not a live instance; the loaded model must support tool use |
| NVIDIA NIM, Hugging Face, llama.cpp | no | these adapters do not send tools; the session falls back to plain chat |

A provider that rejects the tool payload on a turn's first request is remembered
for the session, and that model continues as plain chat.

---

## Testing

| Test file | Covers |
| :--- | :--- |
| `tests/test_agent_filesystem_e2e.py` | The agent, through the production registry, gate and loop, inspecting, creating, writing, reading, editing (recovering from a rejected ambiguous edit), moving, deleting and verifying. Also Manual-mode prompts, denial, a concurrent-edit conflict and recovery, and `/rewind` of the whole turn. |
| `tests/test_edit_file_tool.py` | Unique, ambiguous and `replace_all` edits; missing targets; whitespace hints; large files; binary and missing files. |
| `tests/test_fs_mutation.py` | Atomic-write failures, CRLF/mixed/BOM/UTF-16 preservation, conflict detection, verification failures, the undo journal. |
| `tests/test_fs_security.py` | Traversal, absolute paths, symlink and junction escapes, protected paths, Plan/Auto contracts, denial, fail-closed. |
| `tests/test_fs_tools_dirs.py` | Folder and move tools through the gate. |
| `tests/test_provider_tool_schemas.py` | Every production tool schema through each provider's translation. |
| `tests/test_lmstudio_tools.py` | LM Studio tool attachment and parsing, including streamed calls. |
| `tests/test_tool_loop.py`, `tests/test_tool_loop_safety.py` | The loop itself: errors fed back, bounds, retry rules, redaction. |

```bash
pytest tests/test_agent_filesystem_e2e.py tests/test_edit_file_tool.py \
       tests/test_fs_mutation.py tests/test_fs_security.py -v
pytest                       # everything
ruff check velune/ && ruff format --check velune/ && pyright velune/
```

Symlink tests skip on platforms where the user cannot create symlinks. On
Windows the folder case uses a directory junction instead.

---

## Adding a filesystem tool

1. **Write the tool** in `velune/tools/filesystem/`. Subclass `BaseTool` and
   implement:
   - `get_name()` (letters, digits, `_` and `-` only);
   - `get_description()`, written for the model;
   - `get_schema()`, a JSON Schema object. Keep to `type`, `description`,
     `properties`, `required`, `items` and `minimum`, which every provider accepts.
2. **Declare permissions and actions.**
   - `get_required_permissions()` returns `{ToolPermission.FILESYSTEM_WRITE}` for
     anything that changes files.
   - `describe_actions(args, boundary)` returns one
     `velune.permissions.boundary.path_action(...)` per path touched, with the right
     `ActionType`. That is what the gate decides on, and what flags secret,
     protected and outside-workspace paths.
   - A tool that forgets `describe_actions` is still gated, conservatively.
3. **Resolve paths** with `velune.permissions.gate.resolve_tool_path`, never
   `Path(arg)`, so the call stays inside the approved boundary.
4. **Change files only through `velune/tools/filesystem/mutation.py`.** That
   means `load_text` / `encode_text` / `atomic_write` / `verify_written` for
   content, `ensure_parent` for new folders, and `journal(...)` for every change.
   Return a `MutationResult(...).render()`.
5. **Register it** in `_create_tool_registry` (`velune/tools/subsystems.py`),
   passing the shared `tracker` if it reads or writes file content.
   - Add a verb in `_TOOL_VERBS` in `velune/cli/handlers/tool_chat.py`, and add it
     to `_FILE_MUTATORS` if the approval prompt should show a diff.
   - Add it to `EXCLUDED_TOOLS` in `velune/orchestration/engine/toolpolicy.py`
     if it mutates; the native engine is read-only.
6. **Test it through the loop.** Build the registry with
   `tests/_fs_registry.py::production_registry`. Drive it with a scripted model and
   `ToolLoopRunner`, as `tests/test_agent_filesystem_e2e.py` does, and assert on
   the files on disk. Add it to `FILESYSTEM_TOOLS` in
   `tests/test_provider_tool_schemas.py`.
