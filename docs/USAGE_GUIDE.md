# Usage Guide

*Practical guidance for getting good results out of Velune CLI day-to-day.*

For what each command does, see [Slash Commands](SLASH_COMMANDS.md); for how
the system works internally, see [Architecture](ARCHITECTURE.md). Back to
[README](../README.md).

---

## Contents

- [1. First session on a new project](#1-first-session-on-a-new-project)
- [2. Picking a model](#2-picking-a-model)
- [3. Choosing how a task runs](#3-choosing-how-a-task-runs)
- [4. Reviewing what changed](#4-reviewing-what-changed)
- [5. Making Velune CLI remember your codebase](#5-making-velune-cli-remember-your-codebase)
- [6. Working across multiple projects](#6-working-across-multiple-projects)
- [7. Extending Velune CLI for your team](#7-extending-velune-cli-for-your-team)
- [8. Connecting external tools](#8-connecting-external-tools)
- [9. Diagnosing problems](#9-diagnosing-problems)
- [10. Recovering from a bad state](#10-recovering-from-a-bad-state)

---

## 1. First session on a new project

```bash
cd your-project
velune init      # one-time: registers the workspace, trust prompt if new
velune           # opens the REPL
```

Velune CLI does **not** index your repository automatically — the REPL opens
instantly with no symbol index built. This is deliberate: indexing a large
repo can take real time and CPU, and most sessions (a quick question, a
one-file edit) don't need it. Build the index explicitly, sized to what
you're about to do:

| Command | Cost | Use for |
| --- | --- | --- |
| `/index quick` | Seconds | Manifest-only scan — quick orientation, "what's in this repo" |
| `/index standard` | Depends on repo size | Full symbol index — most day-to-day work |
| `/index deep` | Slowest | Symbols + import graph + relationships — refactors, blast-radius questions |

> `/index` is the primary command; `/cognition` and `/cog` still work as
> aliases for backward compatibility. `/index status` shows freshness,
> `/index rebuild` forces a full rescan.

If you skip this step, `/run` still works — the council just has less
repository context to draw on, and complex tasks may get misclassified to a
lower tier than they deserve (see below).

Once you've indexed, most of the work happens off the prompt path: a
background engine watches the filesystem and keeps the index current
incrementally, without re-scanning the whole repo on every turn. See
[ARCHITECTURE.md § Repository cognition, intelligence & the Knowledge Graph](ARCHITECTURE.md#6-repository-cognition-intelligence--the-knowledge-graph)
for how that works internally.

---

## 2. Picking a model

Run `/model discover` to find what's already available locally (Ollama, LM
Studio), or `velune setup` once to add a cloud provider key (stored in your
OS keyring, never in a config file). Then `/model use <id>` to switch, or let
Velune CLI's hardware-tier detection suggest one at startup.

**Rule of thumb:** local models are fine for `INSTANT`/`STANDARD`-tier work
(explaining code, small edits); reserve your biggest/most expensive model
for `FULL`-tier tasks (security review, cross-cutting refactors) via
`/godly` or `/council`, so you're not paying full-council cost for a
one-line question.

---

## 3. Choosing how a task runs

Every `/run` goes through automatic tier classification — it's not "always
full council," and it's not "always the fast path" either:

- Explaining, describing, or answering "what is X" style prompts → `INSTANT`.
- Ordinary edits and small features → `STANDARD`/`MINIMAL`.
- Refactors, security-sensitive changes, migrations → `FULL`.
- **Regardless of keywords**, if a file you mention has several dependents in
  the repository's import graph (three or more direct dependents trips it),
  Velune CLI escalates the tier automatically — a one-line prompt touching a
  widely-imported file gets full council treatment even if it doesn't sound
  like a "refactor."

This escalation only requires an index (`/index standard` or deeper) —
without one, Velune CLI has no fan-in data to escalate on, so it's worth
indexing before a task that touches a central file.

Override the classifier directly when you know better:

- `/council <task>` — force full council regardless of what the classifier
  would pick.
- `/optimus` — pin everything to instant tier / smallest model / 4k context
  for a whole session (good for a burst of quick Q&A).
- `/godly` — pin full council / largest model / 128k context for a whole
  session (good before a big refactor pass).
- `/normal` — back to auto-classification (16k context, adapts further to
  your hardware profile).

For anything that will take a while, `/run --bg <task>` submits it to a
background job and returns your prompt immediately; `/jobs` and
`/dashboard` track progress, and the status bar shows a live `⚙ N bg`
counter.

---

## 4. Reviewing what changed

Velune CLI edits files directly rather than pasting diffs into chat. After a
run:

- `/diff` — see what actually changed on disk from the last council run.
- `/hunk` — switch to hunk-by-hunk review before accepting edits, if you
  want more granular control than "accept everything."
- `/undo` — revert the last Velune CLI-authored git commit (the changes stay
  staged, so you can inspect them before deciding what to do next).

Set `/approve` up front based on how much you trust the task: `safe` for
routine work (auto-runs reads, prompts on writes/exec), `ask` if you want
visibility into everything non-trivial, `block` for anything you want to
step through command-by-command (e.g. a task you're not fully sure about,
or one running against a sensitive repo).

---

## 5. Making Velune CLI remember your codebase

The five-tier memory system (working → episodic → semantic → graph →
lineage) means context accumulates across sessions without you re-explaining
it — but it only has something to draw on once you've used the tool for a
while:

- Episodic memory persists your conversation history per workspace.
- Semantic memory lets "fix the auth thing from yesterday" resolve to the
  actual prior conversation and files touched, once there's session history
  to search.
- Graph memory captures repository structure once indexed.

`/memory stats` shows what's actually populated; `/context` shows how much
of your active context window is being spent on retrieved memory vs. your
current conversation. If a prompt feels like it's ignoring history it
should know about, check `/memory stats` before assuming the model is at
fault — an empty or stale tier is a common cause.

`/memory clear` is destructive (drops persisted SQLite/Qdrant/LanceDB state
for the workspace, not just the current turn) and prompts for confirmation
— use it when memory has gone stale or wrong, not routinely.

> What decides *which* sources feed a given answer — and how much of each —
> is intent-aware retrieval, not a fixed mix. See
> [ARCHITECTURE.md § Memory system](ARCHITECTURE.md#5-memory-system)
> for the full mechanics (BM25 + vector + graph fusion, cross-encoder
> reranking, per-intent weighting).

From the terminal (outside the REPL), the equivalent commands are
`velune memory stats|inspect|clear|compact`.

---

## 6. Working across multiple projects

`/project list` and `/project open <path>` switch workspaces without
restarting the REPL — each workspace has its own index, memory, and MCP
config. `/project add <path>` registers a workspace you want to switch to
later without opening it immediately. Use `velune session list` /
`velune session delete <id>` from the terminal (outside the REPL) to manage
saved sessions in bulk.

---

## 7. Extending Velune CLI for your team

Three ways to add project- or team-specific behavior, roughly in order of
how much you want to share:

1. **File-based slash commands** — a TOML file in the workspace defines a
   one-off command. Fastest to write, not portable outside the repo unless
   committed alongside it.
2. **Declarative plugins** — a `plugin.json` manifest bundling commands,
   skills, hooks, and MCP servers as one unit under
   `<workspace>/.velune/plugins/` (or `~/.velune/plugins/` for personal,
   cross-project plugins). Best when you want to share a coherent bundle
   with a team, since it's one thing to enable/disable (`/plugin`) rather
   than several loose files.
3. **Hooks** — external commands run on lifecycle events
   (`PreToolUse`/`PostToolUse`/`SessionStart`/…), configured in
   `velune.toml [hooks]` or `.velune/hooks.json`. Use this for things like
   "run a linter after every file write" or "block commits to `main`" —
   policy enforcement rather than new functionality.

See [ARCHITECTURE.md § Hooks and plugins](ARCHITECTURE.md#9-hooks-and-plugins)
for the file formats.

---

## 8. Connecting external tools

- **MCP** — to use Velune CLI's tools from Claude Desktop, VS Code, or another
  MCP-capable editor, run `velune mcp serve`. To pull tools *into* Velune CLI
  from an external MCP server, declare it in `.mcp.json` and use `/mcp
  connect <name>`. See [MCP.md](MCP.md) for trust gating and transport
  details — outbound connections are trust-gated per workspace, so a repo
  you haven't explicitly trusted won't silently load its `.mcp.json`.
- **Resource connectors** — for Velune CLI to query a database or container
  directly (Docker, Postgres, MySQL, Supabase), use `/resource discover`
  then `/resource configure <id>`. Credentials are encrypted at rest;
  anything beyond a read requires an explicit approval prompt each time
  (or per-session, depending on how you answer it) — this is intentional
  friction for write/admin-tier actions against real infrastructure.
- **GitHub/GitLab** — `/push`, `/pr`, `/issue` wrap the same git/REST
  integrations `velune project` commands use; no separate setup beyond
  having `git` configured and, for PR/issue commands, a token available to
  the underlying REST client.

---

## 9. Diagnosing problems

Start with `/doctor` (in-REPL) or `velune doctor check` (terminal, more
thorough — checks providers, storage, security posture, performance, and
council role coverage in one pass, add `--fix` to auto-remediate what it
safely can). Common specific checks:

- `velune doctor providers` — connectivity to every configured provider.
- `velune doctor network` — SSRF/reachability checks relevant to MCP and
  resource connectors.
- `velune logs` / `velune logs live` — recent or streaming execution events
  read back from the trace log.
- `velune status` — index freshness, file/symbol counts, and workspace
  health, read from on-disk state.
- `velune pipeline trace "<query>"` — trace a query through the retrieval
  pipeline (lexical/vector/graph hit counts, timings, fusion) — the CLI
  entry point into what
  [ARCHITECTURE.md § Memory system](ARCHITECTURE.md#5-memory-system) describes.

Both `velune status` and `velune logs` exist specifically so you can prove
(to yourself or someone else) that indexing or a tool call really happened,
not just that a command returned success.

If Velune CLI feels like it's ignoring something you told it earlier in the
session, check `/context` (is it being truncated out of the budget?) before
`/memory stats` (was it ever persisted?).

---

## 10. Recovering from a bad state

`velune backup` / `velune restore` / `velune recover` cover the "something
went wrong" cases: `recover` is designed for the REPL-crash case (a
per-turn autosave the REPL keeps automatically), while `backup`/`restore`
are the manual, explicit path for moving or restoring the full state
(sessions, config, providers, memory, trust) as one archive — useful before
a risky operation like `/memory clear`, or before upgrading Velune CLI itself.
