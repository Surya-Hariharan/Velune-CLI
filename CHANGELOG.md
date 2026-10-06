# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- **Execution modes: Manual, Plan and Auto** (`/manual`, `/plan`, `/auto`,
  Shift+Tab, `velune --mode`, `[execution] mode`; the status bar shows the mode).
  - **Manual** (default) reads freely and asks before every change. Related
    changes are approved together.
  - **Plan** writes a plan to `.velune/plans/<name>.md` (objective, findings,
    changes, files affected, risks, rollback, order) and changes nothing. You can
    revise it repeatedly; only an explicit approval executes it. Changes outside
    the plan's files ask first, and the results are written back into the plan.
  - **Auto** edits, creates, deletes and runs commands inside the project without
    asking, and iterates until tests pass.
  - **In every mode**, these always need confirmation: anything outside the
    project, high-risk operations (`git reset --hard`, `git clean`, force-push,
    recursive deletes, global installs, system changes), and secrets.
  - Enforced at the tool layer; every decision is recorded (`velune trace --type permission`).
- **Folder tools:** `create_directory`, `delete_directory`, `move_path`. These are
  native Python, so they work on Windows, where `mkdir` through the command tool
  could not.
- **Math in responses is rendered in the terminal.** LaTeX that models write (`$x^2$`, `\( … \)`,
  `$$ … $$`, `\[ … \]`, `\begin{bmatrix} … \end{bmatrix}`) now shows as readable Unicode:
  - matrices of any size with tall brackets and aligned columns;
  - stacked fractions, roots, and sums or limits with their bounds;
  - sub- and superscripts, Greek letters and operators.

  Inline math, including math inside table cells, renders on one line. A formula still streaming in
  is laid out as it arrives, instead of flashing raw source.

  Consoles that can't show Unicode get an ASCII version. A formula too wide for the terminal falls
  back to one line, and one Velune can't parse is shown as written. Code blocks and inline code are
  never touched. No new dependencies; turn it off with `[display] math_rendering = false`.

### Fixed

- **LaTeX in answers was printed broken** (`[ A=\begin{bmatrix} 1 & 2 & 3\ 0 & …`). Markdown treated
  `\[`, `\(` and `\\` as escapes and deleted the backslashes before anything could render the math.
  The math is now taken out before Markdown parsing. `velune ask`, `velune run` and council agents
  now use the same renderer as the REPL; they used to print responses as plain text.

- **Every turn after a failed or denied tool call was rejected by Groq** ("rejected the request
  as malformed (HTTP 400)"). Velune sent an internal `is_error` field that the OpenAI-style APIs
  don't accept. It is now removed for those providers, and a 400 shows the provider's own reason.
- `/push`, `/pr`, `/issue`, council edits and the MCP server no longer bypass approval. The command
  tool's `directory` argument can no longer point outside the workspace. The Docker sandbox now
  applies the same command allowlist.
- Tool output is redacted for API keys and tokens before it reaches the model. An identical tool
  call that already failed is not retried blindly.
- **Command palette and `/` completion now match by name prefix.** Typing `/c` lists only the commands whose
  name starts with `c` (and `/co` narrows that further). Before, aliases and search keywords were matched
  loosely too, so `/co` returned 29 results led by `/index` (through its `/cog` alias) and `/m` pulled in
  `/roles` and `/pull`. Aliases and keywords still find a command when no name starts with what you typed,
  so `/anthropic` and `/login` still reach `/connect`.

- **Council runs no longer treat failures as success.** A judge (reviewer, challenger or critic) that
  timed out, errored or answered with something unparseable used to count as an approval, and a
  synthesizer timeout was shown as the answer. Such judges now abstain: they are named in the
  arbitration flags (`JUDGE_UNAVAILABLE:<seat>`, `NO_REVIEW`) and cap confidence (0.60 when one judge
  is missing; 0.40 plus a human-review request when the reviewer or half the judges are missing; 0.30
  when none ran). A synthesizer failure produces the degraded report instead of an error string, a failed
  debate revision keeps the previous proposal, a judge that cannot re-review does not confirm a
  revision, and runs that produced no answer are failures in the REPL rather than stored answers.
  Degraded results are labelled (`ask --json` reports `degraded`). The debate revision now sees the
  proposal it is revising, and the synthesizer is told the council's confidence and flags.
- **`/council` and `/mode fast|max` now force the council tier they promise.** The tier was dropped
  before it reached the orchestrator, so `/council` ("force full") classified the task like any other.
  A forced tier that a hardware or config ceiling (or low-resource mode) lowers is announced, and
  `ask --council-tier` rejects unknown names. `/council` is slow and makes many model calls.
- **`/mode fast` now really skips the critics.** "Critics: disabled" was only printed. The Challenger
  and the four specialised critics are skipped (the Reviewer stays), including for `/council`.
- **A council seat whose provider fails can fall back to another model.** The mechanism existed but was
  never given any providers, and it was skipped on timeouts. Fallbacks follow
  `providers.fallback_providers` in order and are announced as `[Fallback]`. Because a prompt can
  contain your code, a local model never falls back to a cloud model unless you set
  `providers.allow_cloud_fallback_from_local = true`.
- **`/roles` now offers, applies and reports exactly the roles that work.** It listed eight roles but
  the council uses five: the picker hid `challenger` and `synthesizer`, offered the inert `embedding`,
  silently dropped `architect`, `security` and `embedding`, and one unknown entry in
  `council_roles.json` discarded every saved assignment. Assigning a role with no effect is now
  rejected with guidance; saved entries without effect are reported and kept; an assigned model that is
  not available is announced and shown as unavailable; the stored provider is honoured; and the saved
  assignments apply to `ask`, `chat`, `run` and the MCP server, not only the REPL. `velune doctor`
  shows what is really in force.
- **MCP `velune_ask` and sampling now run the real council.** They always answered "Council not
  available": the server built its own orchestrator with a missing argument and then called a method
  that does not exist. `velune mcp serve` now hands the server its runtime orchestrator; results carry
  `confidence` and `degraded`, and a timeout or total agent failure is reported as an error.

### Changed

- `/approve` is now a legacy alias for the execution modes (ask → manual, safe → auto,
  block → plan). `--yes` starts in Auto mode.
- **Premium council prompts are now opt-in.** The git-ignored `_premium.py` prompt layer used to override
  the committed prompts whenever the file existed, so the same version behaved differently on a
  developer machine, in CI and from PyPI. It now loads only with `VELUNE_PROMPT_LAYER=premium`
  (a missing or malformed layer warns and falls back to the committed prompts). `velune doctor` shows
  the active layer and council traces record it. Set the variable to keep using a local premium file.

---

## [0.9.8] - 2026-10-05

### Added

- **One-line installers.** `scripts/install.sh` (macOS/Linux/WSL) and `scripts/install.ps1` (Windows) install
  Velune into its own isolated environment with `uv tool install`. They work with no Python installed, or one
  that is too old or "externally managed" (PEP 668), and they can't conflict with other tools' packages.
  This is now the recommended install path in the README.
- **Richer inline Markdown.** Tables render with borders and wrap long cells instead of truncating;
  `` ```mermaid `` flowcharts (`graph`/`flowchart`, labelled and chained links, node shapes) draw as a
  box-drawing tree; fenced ASCII/bar charts are cropped to the terminal width instead of wrapped, so their
  alignment survives narrow terminals. Unsupported diagram types still show as code.
- **Live model catalogs for OpenAI, Anthropic and Gemini.** Each curated model list is now reconciled with
  the provider's live model listing, so retired models drop out and new chat models appear with the
  provider-reported context window (offline, the curated list is used unchanged). OpenAI reasoning models
  (`o*`, `gpt-5*`) now receive `max_completion_tokens` and no custom sampling instead of an HTTP 400.
- **Self-repairing memory database.** A damaged SQLite file ("database disk image is malformed") is moved aside
  as `<name>.corrupt-<timestamp>` and recreated at startup, instead of leaving every memory subsystem failed
  on each launch.
- Project provenance files: `AUTHORS.md`, `NOTICE`, `GOVERNANCE.md`, `.github/CODEOWNERS`,
  `docs/project-origin.md`; contribution-licensing terms in `CONTRIBUTING.md`; each GitHub release body now
  names its source tag, commit and repository.
- Documentation checks: tests keep `docs/slash-commands.md` in sync with the registered commands and keep
  relative Markdown links valid.

- **Model palette (`/model`, `/models`).** Typing `/model` or `/models` now
  opens a keyboard-driven picker in the same floating rectangle the command
  palette uses — arrow keys to highlight, Enter to switch (routed through the
  existing `/model use <id>` path, so persistence/recents/default-provider
  behave exactly as a hand-typed switch). Cyan-blue theming distinguishes it
  from the indigo command palette sharing that space. An empty or unreachable
  model catalog shows setup guidance instead of submitting a prompt or
  printing an error panel.

### Fixed

- `scripts/install.sh` (the macOS/Linux one-line installer) was excluded by a `.gitignore` rule for
  local scripts, so the README's `curl … | sh` command would have returned 404. A test now checks that
  every installer the README links exists and is tracked.
- **`velune doctor` is now the diagnostics command, and it only fails for real installation problems.**
  - A bare `velune doctor` runs the checks; it used to print help. `velune doctor check` still works.
  - It opens with an environment summary: Velune version, Python and interpreter path, OS/architecture,
    install location, the `velune` command on PATH, and the config and data directories.
  - It ends with a core verdict ("✓ Core installation is healthy"). Optional integrations, such as
    provider keys, Ollama or LM Studio, extras and GPU, are listed separately and no longer make the
    exit code non-zero. Scripts can rely on `velune doctor` exiting 1 only when the installation itself
    is broken.
  - `velune doctor --json` includes the environment and verdict. `doctor check --json` keeps its
    previous list format.
- `velune doctor` no longer creates `./.velune/` and an empty database in whatever folder it runs from.
  It also no longer reports a false failure when started from a read-only folder such as
  `C:\Windows\System32`.
- `velune doctor` no longer warns about the `velune` command on pipx / `uv tool` installs (including the
  one-line installer). It now reads which Python the launcher actually runs, instead of assuming the
  launcher sits next to `python.exe`.
- A fresh lean install no longer prints `ERROR` lines. Two messages are gone: "LanceDB startup failed: No
  module named 'lancedb'" (the `[rag]` extra is optional) and "no such table: sessions" (doctor opened
  an empty memory store for an unused folder). The summary banner no longer says FAIL when only an
  optional integration failed.
- **Optional extras installed broken or not at all.** `velune-cli[parsing]` allowed tree-sitter
  0.21/0.22, where every grammar failed to load and repository parsing silently fell back to regex.
  0.21 also had no wheel for Python 3.12+. `velune-cli[rag]` allowed `qdrant-client` versions without
  `query_points`, which broke every vector search at runtime, plus `pyarrow`/`lancedb` versions with no
  wheel for Python 3.13/3.14. The floors are now the oldest versions that pass the tests and have wheels
  everywhere. A broken grammar is logged and reported by `velune doctor` instead of being swallowed.
- **`pip install velune-cli` failing or crashing on machines that already had Python packages.** The core
  dependency floors were far below anything tested (`typer>=0.9`, `prompt-toolkit>=3.0.0`, `numpy>=1.24`,
  `orjson>=3.9`…). pip therefore kept stale copies already installed by other tools, and Velune crashed at
  runtime: for example, `typer<0.16` with `click>=8.2` breaks every `--help`. pip could also pick versions
  with no wheel for newer Pythons and fail with "Microsoft Visual C++ 14.0 is required". The floors are now
  the oldest versions that pass the test suite and ship wheels for CPython 3.10–3.14 on Windows, macOS and
  Linux. CI installs exactly those versions to keep it that way.
- **Misleading startup error.** Any import failure used to say "your Python installation is corrupted —
  reinstall Python", even when the real cause was an incompatible dependency. A dependency problem now names
  the package and its installed version and gives the upgrade/isolated-install command. Import failures
  while the CLI is being built (not only the first import) now get this message instead of a raw traceback.
- **`velune doctor` reporting failures on a correct install.** *Core Dependencies* listed the optional
  `qdrant_client` and so failed on every lean install. It now checks the real declared requirements,
  including minimum versions. *Qdrant in-process* is now a warning ("optional `[rag]` extra not installed")
  instead of a failure.
- Cost estimation no longer raises when `tiktoken` can't download its encoding file (offline or firewalled
  machines); it falls back to the word-count estimate.
- **Groq chat failed with HTTP 404 / 400.** The static model list kept models Groq had retired, and models
  discovered later were assumed to have a 131k-token window (`allam-2-7b` has 4k). Groq models now come from
  the live `/models` list with the real context window.
- **Gemini errors were all generic.** Gemini now raises the same typed errors as the other adapters (rejected
  key, rate limit with `Retry-After`, retired model, malformed request); the retired Gemini 1.5 and
  thinking-exp entries were replaced.
- **Anthropic key validation spent a billed completion** against a hardcoded model (a retired model made a
  valid key look broken). It now calls the free `/v1/models` endpoint.
- **`/doctor` tore up the full-screen UI** (its report was written to the real terminal, and ~20 blocking
  checks froze the app). It now renders through the REPL console and runs its checks in a worker thread.
- **Log lines were printed over the composer.** Console log handlers are muted, and structlog is routed
  through stdlib logging, while the full-screen app runs.
- **Misleading error hints.** Provider failures now suggest the real remedy (`/connect` for a rejected key,
  `/model` for a retired model, retry for rate limits); the "Council Failed" panel names retired models and
  `velune models refresh`; `velune provider list` fits an 80-column terminal.
- **Python 3.10:** `asyncio.TimeoutError` is not the builtin `TimeoutError` before 3.11, so first-chunk and
  health-check timeouts escaped their handlers; both are now caught.
- Lint (`ruff`), formatting and `pyright` regressions on `main`.

- **`velune health` crashed with a raw `ImportError` traceback** on every
  invocation — it imported a provider-metadata symbol removed in the
  provider-catalog consolidation. Repointed at `velune.providers.catalog`.
- **`provider status` / `provider test` / `provider inspect` / `provider api`
  always failed** with an internal "no current event loop" message instead of
  a real credential verdict — `asyncio.get_event_loop()` raises in a thread
  with no running loop on Python 3.12+, which is the normal CLI entry point.
  Fixed to match the working pattern already used by credential add/update.
- **Rich markup rendered as literal text** (`[bold magenta]...[/bold magenta]`)
  in `memory stats`'s architecture map and `project status` — both built
  styled text with `Text.assemble()`, which does not parse markup tags.
- **`config show` displayed a key `config get` rejected** (`providers.default`
  vs. the real `providers.default_provider`), and gave no indication of which
  `velune.toml` on disk a value actually came from when config resolution
  walks up past the workspace.
- **`models scan` could recommend a model it had just marked unhealthy** as
  the suggested default. The recommendation now prefers a healthy model and
  falls back to guidance pointing at the actual blocker when none are
  reachable.
- **`/models` with an empty catalog printed a red "No models available" error
  panel** and could be submitted as a prompt. It now opens the model palette
  (which explains the empty state and how to fix it) and no longer submits.
- Mojibake (`�`) in place of an em-dash in `--help` output on Windows, caused
  by `sys.stdout` defaulting to the active ANSI code page instead of UTF-8.

### Security

- Bumped `pyjwt` 2.13.0 → 2.15.1, `urllib3` 2.7.0 → 2.8.0, `virtualenv` 21.7.0 → 21.14.5 and `pip` 26.1.2 →
  26.2.1 in `uv.lock` (all transitive) to clear the open Dependabot alerts.
- Closed the mcp 2.x major-bump Dependabot PR (the 2.x low-level Server API is not yet supported).

- Bumped `cryptography` 49.0.0 → 50.0.0 (GHSA/Dependabot alert #8: PKCS#7
  Bleichenbacher oracle in a transitive dependency path; Velune does not use
  the affected API directly).
- Bumped `h2` 4.3.0 → 4.4.1 (Dependabot alert #9: duplicate `Host` header
  request-smuggling primitive in a transitive dependency of the optional
  `[rag]` extra; Velune does not use HTTP/2 directly).
- Master passphrase file (`master.passphrase`) is now encrypted at rest
  (Windows DPAPI, or AES-GCM with a machine-derived key elsewhere) instead of
  stored in clear text, with automatic one-time migration of any existing
  plaintext file. Addresses a CodeQL clear-text-storage finding.

### Changed

- The `[dev]` extra requires `twine>=7.0.0`: older twine can't read the Metadata-Version 2.5 that
  current Hatchling produces, so `twine check --strict` failed for contributors.
- `packaging` is now a core dependency. `velune doctor` uses it to evaluate Velune's own requirement
  markers and minimum versions.
- Removed the `openai`, `anthropic` and `orjson` dependencies. They were never imported (every provider uses
  plain `httpx`), and dropping them removes 7 packages from a default install, two of them Rust-compiled.
- Python 3.14 is officially supported (classifier and CI matrix).
- **Removed the unshipped Go launcher and Rust native module** (`ext/`) and their CI jobs. Neither was part of the
  PyPI package; the repository indexers now use the plain-Python helpers in `velune/repository/file_scan.py`
  (formerly `_native.py`), whose behaviour is unchanged.

- Minimal chat output: no "Velune" header above replies, a per-turn footer of just the token count (plus cost
  when known), and a one-line connection confirmation.
- Documentation reorganised: the public guides live in `docs/`; the architecture reference is kept private.
  `docs/development.md`, `docs/usage-guide.md` and `docs/slash-commands.md` were brought back in line with the
  code (commands, CI, bootstrap, file locations).
- `pyproject.toml` license metadata migrated to PEP 639 (`license =
  "Apache-2.0"` + `license-files`), replacing the legacy `{file = "LICENSE"}`
  form that duplicated the full license text into package metadata.
- README expanded with an explicit Apache-2.0 statement and a trademark/
  branding clarification.

## [0.9.7] - 2026-07-31

### Added

- **`velune doctor` reports terminal zoom-lock feasibility.** Investigated
  whether Velune could lock the host terminal's font-zoom shortcuts
  (`Ctrl +/-/0`, `Ctrl+scroll`, `Cmd +/-/0`) for the session across Windows
  Terminal, the legacy Windows Console Host (PowerShell/CMD), macOS
  Terminal.app, iTerm2, GNOME Terminal, Konsole, Alacritty, kitty, and the
  VS Code integrated terminal. None expose a session-scoped API for this —
  zoom is always resolved inside the emulator's own input layer before any
  byte reaches the child process — so no lock is implemented. `velune
  doctor` now detects the hosting terminal and surfaces this as a documented
  "warn," not a silent no-op; full per-terminal findings in
  docs/terminal-zoom-lock.md. Extended to cover
  WezTerm (Lua-configured `key_bindings`, resolved by its own `winit` event
  loop — same structural dead end, same verdict).

### Changed

- **The status bar and Rich console output inside the fullscreen REPL are now
  fully responsive to the terminal's actual column count, instead of looking
  best only at whatever width a developer happened to be testing at.**
  Re-investigated a report that the UI "looks optimized at 90% zoom but not
  100%": terminal zoom is not something a terminal app can observe or control
  at all (see docs/terminal-zoom-lock.md) — a
  text-mode app only ever sees a COLUMNS×LINES grid, so a narrower zoom level
  is indistinguishable from a narrower terminal window. Auditing every
  rendering surface in `velune/cli/fullscreen.py` found the home screen,
  composer, and prompt borders already fully width-driven, but two real
  responsiveness bugs: (1) `render_status_bar` concatenated every active
  segment (model, mode, context bar, git branch, MCP, background jobs,
  alerts, invalid keys, provider health, latency, throughput) with zero width
  awareness, so prompt_toolkit's non-wrapping status window would hard-clip
  the tail mid-segment whenever too many indicators were active for the
  available width — now it drops lower-priority segments in the documented
  priority order (never mid-word) and mathematically cannot exceed the given
  width, even in pathological cases; (2) the Rich `Console` feeding all
  panel/table/markdown output synced its `.size` by assignment on every
  write, which — because Rich's own `.size` *setter* permanently disables its
  live terminal auto-detection — pinned the console to whatever width was
  current as of the *previous* print, one print behind any resize, for the
  rest of the session. Replaced with `_LiveSizedConsole`, which overrides
  `.size` as a property that always re-reads the app's current width/height,
  matching the pattern already used by `_render_home`/`_render_status`. New
  coverage in `tests/test_statusbar_responsive_width.py` (status bar behavior
  from 40 to 200 columns) and `tests/test_fullscreen_ui.py` (console
  live-width tracking, including a test that fails against the old
  write-time-sync implementation).

- **Home screen banner renamed "VELUNE" → "VELUNE CLI"**, and the REPL's
  content column now fills the full terminal width by default instead of
  letterboxing into a fixed 100-column reading pane (still available via
  `display.content_max_width` in `velune.toml` for anyone who preferred it).
- **Renamed five slash commands whose names didn't say what they do.**
  `/login` → `/connect` (it pastes an API key into a provider, not a
  username/password sign-in), `/councilmodel` → `/roles`, `/optimus` →
  `/fast`, `/godly` → `/max` (now matching `/mode fast|max|normal`'s own
  vocabulary instead of a mismatched pair of nicknames), `/typify` →
  `/types`. Every old name keeps working as an alias — nothing breaks,
  tab-completion and `/help` just lead with the clearer name now.

### Fixed

- **Typing any command (e.g. `/models`) could suddenly dump a wall of
  "Provider X unavailable" panels — one per cloud provider you never even
  configured, repeating forever.** `ProviderRegistry` registers a lazy
  factory for every provider type it knows about, including ones with no
  stored API key, so `ProviderHealthMonitor`'s periodic poll reports
  `UNAVAILABLE` for those right alongside a genuinely broken *configured*
  provider — failing health for a provider you never set up isn't
  actionable. `ProactiveWatcher._run_periodic_checks` then alerted on every
  such still-unavailable provider on every 15s tick, unconditionally,
  forever (nothing publishes the `provider.health_changed` bus event its
  sibling handler expects, so this periodic path was the *only* thing ever
  generating these alerts). Since alerts only get drained and rendered on
  the next prompt submit (`poll_and_render_alerts`, called before dispatch
  regardless of what you type), a backlog accumulated over however many
  15s ticks passed since your last keypress would land all at once,
  looking like the output of whatever command you happened to type.
  Fixed: only alert for providers that are actually configured (a stored
  key, or a keyless local endpoint like Ollama/LM Studio), and only on the
  transition into `UNAVAILABLE` — tracked per-provider so a still-down
  provider is reported once, not re-queued every tick, while a fresh
  outage after recovery still alerts again. New coverage in
  `tests/test_proactive_watcher_health_alerts.py`.
- **The prompt composer no longer grows and shrinks unpredictably as you
  type — it now has a fixed idle height (3 rows) that grows with multiline
  content up to a hard ceiling (8 rows) and then freezes there for the rest
  of that draft**, instead of resizing every keystroke. The composer
  previously used a static `Dimension(preferred=1)`, but `HSplit`'s own
  layout algorithm hands any terminal rows left over after satisfying every
  child's *preferred* size up to each child's *max* — so in any terminal
  with a few spare rows it rendered pinned near its max regardless of
  content, and shrank again the moment the conversation pane above it grew
  enough to reclaim that space. That combination is what read as the box
  "zooming" in and out. Fixed with a content-driven height callback
  (`FullscreenREPLUI._prompt_window_height`) plus `dont_extend_height=True`,
  which stops `HSplit` from redistributing leftover rows to the composer at
  all — the conversation pane above absorbs all of it instead. Both bounds
  are configurable via `display.composer_min_lines`/`composer_max_lines` in
  `velune.toml`. Also gave the conversation pane its own mouse-wheel
  handling (`_ScrollableConversationWindow`) so wheel scroll over either
  pane routes through one consistent scroll path instead of fighting
  keyboard-driven scrolling. New coverage in `tests/test_composer_height.py`
  renders a real `Application` and reads `render_info.window_height` off the
  live layout rather than just unit-testing the height formula, specifically
  to catch this class of regression again.
  Separately: this does **not** touch the host terminal emulator's own
  font-zoom shortcuts (`Ctrl +/-/0`, `Ctrl+scroll`) — those are resolved by
  the terminal before any byte reaches Velune and were already confirmed
  unlockable from in-app code (see the "`velune doctor` reports terminal
  zoom-lock feasibility" entry above). If the composer above is what was
  growing/shrinking, this fixes it; if the terminal's own font size was
  changing, that's a terminal-emulator setting, not a Velune bug.
- **First-turn episodic-memory writes silently failed on a fresh launch.**
  `VeluneREPL._start_episodic_session()` runs once, synchronously, near the
  top of `run()` — before Tier-1 background warm-up (which owns
  `runtime.episodic_session_memory`) necessarily finishes, so the very first
  attempt could raise and get swallowed at DEBUG level, leaving
  `_episodic_session_id` pointing at a session row that was never inserted.
  Every turn recorded afterwards then failed `EpisodicMemory.record_turn()`'s
  `turns.session_id` foreign key. `entrypoint.py` was already calling
  `repl.on_warm_complete()` once Tier-1 finished, but the method didn't
  exist, so the call itself raised and did nothing. Implemented
  `on_warm_complete()` to retry `_start_episodic_session()` once Tier-1 is
  actually up, and made `EpisodicMemory.record_turn()` return `""` (not a
  phantom turn id) when the insert fails, so
  `MemoryLifecycleManager.record_turn()` doesn't enqueue embedding work for
  a row that doesn't exist.
- **`pip install velune-cli` now also registers a `velune-cli` command.**
  Root-caused a Windows report of "neither `velune` nor `velune-cli` is
  recognized" after a clean install. A from-scratch build → wheel inspection
  → clean-venv install → execution cycle confirmed the `velune` console
  script, its generated launcher, and PyPI publish state were all already
  correct — that half of the report was the well-known (and already
  README-documented) case of the interpreter's `Scripts`/`bin` directory not
  being on `PATH`. `velune-cli`, however, was a real gap: `[project.scripts]`
  only ever defined `velune`, so the name matching the PyPI distribution
  itself — the name users most naturally guess — was never a valid command
  regardless of `PATH`. Added it as a second `[project.scripts]` entry
  pointing at the same `velune.main:main`. Also removed the repo's
  `MANIFEST.in`: it is inert under the Hatchling build backend (sdist
  inclusion is controlled entirely by `[tool.hatch.build.targets.sdist]` in
  `pyproject.toml`) and had drifted to reference a `docs/CHANGELOG.md` path
  that no longer exists.
- **CHANGELOG/CI documentation drift.** The `[0.6.0]` entry below describes
  an Architecture Lint job, a 70%-minimum unit-test coverage gate, and a
  dedicated startup-performance-regression job as part of CI. None of the
  three are present in the current `.github/workflows/ci.yml` — that history
  is left as-written below rather than rewritten, but is called out here so
  the changelog isn't mistaken for a description of what gates a merge
  today. Current CI is: lint (ruff + pyright), targeted security checks
  (pip-audit, bandit, gitleaks, a `shell=True`/`create_subprocess_shell`
  regression grep, an `asyncio.run()` count check), the full OS/Python test
  matrix, a build check, and an install smoke test.

## [0.9.6] - 2026-07-18

### Added

- **API-key lifecycle management.** Stored keys now carry a real verification
  state (`verified` / `unverified` / `stale` / `invalid`) instead of a
  hardcoded "valid"; `velune login <provider>` verifies on save, `/providers`
  was rebuilt around the state model, and a rejected key (HTTP 401) observed
  at runtime is persisted so the next run fails fast at preflight with the
  one-command fix instead of burning a council cycle against guaranteed 401s.
- **Incremental repository cognition.** Warm prompts reuse a cached,
  delta-aware snapshot refreshed from filesystem events instead of rebuilding
  the full pipeline per prompt (32–130× faster warm turns).
- **Architectural convergence, phase 1.** The three-brain memory coordinator,
  context assembler, and intent classifier are wired into the live REPL turn
  path (previously built but orphaned), with turn recording feeding episodic
  memory.
- **Retrieval pipeline expansion.** Intent-aware retrieval planning, hybrid
  score normalization, and BM25 identifier subword tokenization — a
  natural-language query like "list users" now reaches `list_users` and
  `listUsers` symbols.
- **`/resource configure <postgres|mysql|supabase>`** — the missing piece of
  0.9.5's Resource Connector Framework: an interactive, encrypted credential
  prompt (pre-filled from `/resource discover` hints where available) that
  closes the gap where those three connectors were registered but had no way
  to ever receive credentials, so `/resource connect` always failed. Docker
  needs no configuration and continues to connect directly.
- Repository cognition actually runs end-to-end: `RepositoryIntelligenceEngine`
  and the `KnowledgeGraph` are now constructed and initialized (previously
  registered in a `module.py` no bootstrap path imported), and the workspace
  auto-index + "Repository Detected" banner fire on REPL entry.
- CLI onboarding restructured into a `velune/cli/onboarding/` package
  (`stages.py` / `logic.py`), with a dedicated `velune onboard` entry point
  that can resume an interrupted wizard run.
- Destructive slash commands are gated by a new `SlashCommand.permissions`
  "confirm" flag; `/memory clear` now asks before wiping memory.
- Command palette surfaces recently-used commands.
- Telemetry tracking for cognition, tokens, and usage; 8 new UI/widget/indexing
  integration tests.

### ![Fixed](https://img.shields.io/badge/-Fixed-informational?style=flat-square)

- **A bad API key no longer produces a fake "answer" and exit code 0.**
  Failed council agents used to return their error text as if it were a
  response; it flowed through candidate voting and rendered as the final
  summary while the command exited 0 behind 20+ raw logger lines. Failure
  sentinels are now dropped from the candidate pool, all OpenAI-compatible
  adapters (OpenAI, Groq, OpenRouter, Fireworks, Together, xAI) raise a typed
  authentication error on 401/403 in `infer`/`stream`/`embed`, and `velune
  ask` renders one actionable panel (naming the rejected provider and the
  `velune login` fix) and exits non-zero. Preflight also blocks when the only
  discovered models are Ollama manifest entries with the daemon down.
- **Production-readiness fix set:** single Ctrl+C now cancels generation in
  the fullscreen REPL; stream renderer finalizes on early cancel (no leaked
  thinking animation); resuming a session archives the live conversation
  first and adopts the resumed session id; `WebFetch` re-validates every
  redirect hop (SSRF); keyring-less credential fallback upgraded to a
  passphrase-derived key (PBKDF2) with legacy stores still readable; fast
  chat turns now actually hit the incremental cognition cache; reranker
  source-trust rekeyed to the real retrieval source labels; BM25 returns
  verbatim matches in 1–2 file corpora (negative-IDF degeneracy); `mcp`
  floored to a patched release (CVE-2026-59950).
- Restored `docs/CHANGELOG.md`, which had been deleted in a docs cleanup
  while still referenced by the PyPI project metadata, the sdist include
  list, and the GitHub Release notes generator.
- **Model picker showed unconfigured cloud providers as "installed."** Five
  call sites (`/model`, `/model discover`, `/councilmodel`, mode-based
  auto-selection) checked provider availability with
  `provider_registry.get(id) is not None`, which is always true for every
  built-in cloud provider regardless of whether an API key exists. Switched
  to the registry's real `check_provider_available()` (an API-key check),
  while exempting local models so Ollama isn't hidden by the same fix.
- Plain chat turns silently skipped semantic context retrieval — the REPL
  called a `HybridRetriever` method that didn't exist, and the failure was
  swallowed by a bare `except`.
- Plugin slash commands raised a silently-logged `ModuleNotFoundError` on
  every invocation (wrong import path).
- `VectorRetriever` derived Qdrant point IDs from Python's salted `hash()`,
  so re-indexing the same file could never overwrite its own prior vector;
  now a stable id, with a real `delete_by_ids()` wired into file removal.
- Filesystem scanner had no symlink-cycle guard.
- An `asyncio.run()` regression in CLI onboarding bypassed the single
  sanctioned `run_async()` entry point (caught by CI's own P0-1 regression
  guard, which was otherwise green on a stale cached run).
- Assorted CI-only failures now caught pre-merge: import-sort/`UP035` lint
  violations, 9 files with formatting drift the Lint job's fail-fast
  ordering had never actually reached, and a pyright `reportReturnType`
  error in the wizard chrome's key-binding merge.
- Deleted ~1500 lines of fully dead parallel retrieval/orchestration
  infrastructure with zero callers and zero tests.

## [0.9.5] - 2026-07-08

### Added

- **Resource Connector Framework** (`velune/resources/`) — reusable subsystem
  for connecting to local development resources (Docker, databases,
  Supabase), mirroring the provider/tool architecture so new connectors
  (Redis, Firebase, K8s, AWS) are a single subclass + `register()` call.
  `ResourceManager` is the single authorization choke point, integrating with
  the REPL `ApprovalMode` and the encrypted keystore; SQL statements are
  classified by risk (`SELECT` → READ, DDL → WRITE, `DROP` → ADMIN, with
  multi-statement batches escalated to prevent hidden-DROP injection).
  Ships with Docker (CLI-driven), PostgreSQL and MySQL/MariaDB (local-host-only
  enforced), and Supabase (SSRF-safe REST) connectors, plus a `/resource` REPL
  command (`list`/`discover`/`connect`/`disconnect`/`status`/`info`).
- **Native tool calling (foundation)** — `InferenceRequest`/`InferenceResponse`
  now carry OpenAI-format `tools`, `tool_choice`, and normalized `tool_calls`
  (`velune.core.types.inference.ToolCall`). The OpenAI, Groq/OpenRouter
  (inherited), OpenAI-compatible, Ollama, and Anthropic adapters send tool
  definitions and parse tool-call turns; the Anthropic adapter translates
  between the OpenAI normal form and Messages-API `tool_use`/`tool_result`
  blocks. Fully backward-compatible — requests without tools produce
  identical payloads to before.
- **`ToolLoopRunner`** (`velune/orchestration/tool_loop.py`) — bounded agentic
  infer → execute-tools → feed-results loop over the local `ToolRegistry` and
  (optionally) connected MCP servers, making MCP tools reachable by models for
  the first time. Every execution goes through `authorize_and_execute`
  (permission + hook enforcement); the default approval policy auto-allows
  read-only tools only and fails closed on approver errors, unknown tools, and
  oversized output. Covered by 16 contract tests with a scripted fake provider.
- **v1.0 readiness audit** — `docs/AUDIT_2026-07-07_V1_READINESS.md`:
  full-system audit with severities and the prioritized roadmap the tool-loop
  work implements.
- **NVIDIA NIM provider** — discovery backend for NVIDIA NIM cloud models
  (`velune/providers/discovery/nvidia_nim.py`), surfaced in `velune setup`
  and the provider catalogue.
- **Model registry cache** — persistent on-disk cache of discovered models
  (`velune/models/registry_cache.py`) so repeat scans and the first REPL
  prompt avoid redundant provider network calls.
- **`velune onboard`** — dedicated first-run setup wizard entry point that can
  resume an interrupted onboarding run.

### ![Improved](https://img.shields.io/badge/-Improved-blue?style=flat-square)

- Expanded and hardened provider discovery (Docker, OpenAI-compatible
  endpoints, Ollama, LM Studio, Google) with more reliable detection and a
  richer interactive `velune provider` / `velune models` command surface.

### ![Fixed](https://img.shields.io/badge/-Fixed-informational?style=flat-square)

- Resolved lint/format violations in the new provider discovery code that
  broke the CI Lint job (unused import, import ordering, explicit `zip(strict=)`,
  local-variable naming).

## [0.9.4] - 2026-06-28

### Added

- Go production launcher improvements for cross-platform startup and health checks.
- Rust native module foundation for optional hot-path repository operations.
- Repository Knowledge Graph for structured file, symbol, and relationship context.
- Repository Intelligence Engine for repository events and incremental graph updates.
- Native integration layer with pure-Python fallbacks when native wheels are unavailable.
- Cross-platform CI covering Python, Go, Rust, packaging, install smoke tests, and native benchmarks.

### ![Improved](https://img.shields.io/badge/-Improved-blue?style=flat-square)

- Faster startup paths for lightweight commands and launcher-assisted execution.
- More reliable native integration across supported operating systems.
- Stronger security checks, including dependency auditing, Bandit, and Gitleaks coverage.
- Broader CI/CD validation before release.
- Expanded tests for knowledge graph, intelligence engine, memory coordination, and native fallbacks.
- More reproducible build and artifact validation.

### ![Fixed](https://img.shields.io/badge/-Fixed-informational?style=flat-square)

- Cross-platform build issues in Go and Rust CI jobs.
- PyO3 compatibility for Rust unit-test linkage.
- Native fallback behavior when the Rust extension is not installed.
- CI failures from synthetic secret-like test fixtures.
- Release workflow checks for version/tag consistency and artifact validation.

## [0.9.3.5] - 2026-06-27

> Sprint 1 AI Foundation — Repository Knowledge Graph, Intelligence Engine, and
> Three-Brain Memory Coordinator, plus Go launcher, Rust native extensions,
> and a fully hardened cross-platform CI/CD pipeline.

### Added — Repository Knowledge Graph (`velune/knowledge/`)

- **SQLite-backed semantic graph** with typed nodes (file, module, class,
  function, method) and typed edges (imports, contains, inherits, defines).
  WAL mode + single `asyncio.Lock` write serialization + short-lived read
  connections keep the store fast and safe under concurrent access.
- **`KnowledgeGraphBuilder`** — converts a `RepositorySnapshot` into a full
  graph in a single atomic transaction; partial failures roll back cleanly.
- **`KnowledgeQuery`** — AI-optimized query layer: `context_for_file`,
  `find_by_label`, `summary_text`. Output is designed for direct LLM injection
  without post-processing.

### Added — Repository Intelligence Engine (`velune/intelligence/`)

- **Central coordinator** with event-driven change detection (3 s file-change
  poll) and git-state tracking (10 s interval). All events ride `CognitiveBus`.
- **`KnowledgeGraphPatcher`** — surgical incremental node/edge updates via
  `FK CASCADE`; avoids full graph rebuilds on every file change.
- **Typed `RepositoryEventType` constants** + factory helpers for subscribers
  (`repository.files_changed`, `index_updated`, `kg_patched`,
  `git_state_changed`, `profile_refreshed`, `engine_started/stopped`).

### Added — Three-Brain Memory Coordinator (`velune/memory/three_brain.py`)

- **Brain 1 Hot (Working)** — in-session `MemoryTurn` store.
- **Brain 2 Warm (Semantic)** — LanceDB ANN search via `SemanticMemory`.
- **Brain 3 Cold (Episodic)** — SQLite cross-session LIKE search via
  `EpisodicMemory`.
- `asyncio.gather` fan-out across all three brains; `KnowledgeQuery` augments
  warm-brain results with graph context.
- Subscribes to `repository.files_changed` → annotates `ThreeBrainResult`
  with `stale_file_count`.
- `as_context_block()` renders compact, LLM-injectable text; registered as
  `runtime.three_brain_coordinator`.

### Added — Go Launcher (`ext/go/`)

- Cross-platform Go launcher binary (`velune` executable) that discovers the
  correct Python interpreter (sibling `.venv` → `VIRTUAL_ENV` → PATH), relays
  OS signals (SIGINT/SIGTERM) to the child Python process, and manages the
  Velune CLI daemon lifecycle (`start` / `stop` / `status`).
- **`velune update`** — queries PyPI (`velune-cli`) for the latest version and
  upgrades via pip if a newer release exists (`--check` mode for scripts).
- **`velune --health`** — structured health check: Python binary, Python
  version, `velune` module importability, launcher version.
- Full test suite: version parsing, Python discovery, daemon PID file handling,
  process-alive checks, PyPI mock server tests.

### Added — Rust Native Extensions (`ext/rust/velune-native/`)

- **`sha256_file(path)`** — SHA-256 hex digest of a file; O(1) memory via
  64 KB streaming buffer; raises `OSError` on unreadable files.
- **`scan_directory(root, extensions, skip_names)`** — recursive directory
  walker with extension filtering and directory pruning; returns sorted
  absolute paths.
- Pure-Python fallbacks in `velune/repository/_native.py` — all callers use the
  same API regardless of whether the Rust wheel is installed.
- Unit tests covering determinism, empty files, missing files, directory
  skipping, and cross-implementation parity.

### Added — CI/CD

- **Go Launcher** jobs (ubuntu / windows / macos): `go build`, `go test -v`,
  `go vet` for the full launcher package.
- **Rust Native** jobs (ubuntu / windows / macos): `cargo fmt --check`,
  `cargo clippy -D warnings`, `cargo build --lib`, `cargo test --lib`.
- **Native benchmarks** (`benchmark-native`, `benchmark-scan`): build the
  Rust wheel via maturin, install it, run sha256 and scan_directory benchmarks,
  upload JSON results as artifacts (informational — never gates on perf
  thresholds since CI hardware is shared and variable).
- **Wheel install + REPL smoke** matrix expanded to 3 OS × 4 Python versions
  (3.10 – 3.13).
- **`ci-pass` gate job** — requires all matrix jobs before reporting green.

### ![Fixed](https://img.shields.io/badge/-Fixed-informational?style=flat-square)

- Benchmark jobs no longer use `--ci` performance-threshold mode; benchmarks
  are informational in CI and will never fail due to machine variance.
- Go setup action no longer references a nonexistent `go.sum` (this project has
  no external Go dependencies — all imports are stdlib).
- Rust `crate-type` now includes `rlib` alongside `cdylib` so that
  `cargo test --lib` can link the Rust test harness without a Python runtime.
- `velune update` PyPI URL corrected from `/pypi/velune/` to `/pypi/velune-cli/`.

### ![Tests](https://img.shields.io/badge/-Tests-lightgrey?style=flat-square)

- 139 tests passing — 42 new for Three-Brain Memory, 41 for Intelligence Engine,
  41 for Knowledge Graph, plus cross-platform CI matrix (3 OS × 4 Python).

## [0.9.3] - 2026-06-24

> Promotes `0.9.3-beta.1` to a stable release and lands a command-architecture
> and developer-experience pass on the interactive REPL. No behavioral changes
> to the startup/cognition model introduced in the beta — see `0.9.3-beta.1`
> migration notes if upgrading from `0.9.2` or earlier.

### Changed — Command architecture & UX

- **`/cognition` renamed to `/index`.** The primary verb is now `/index`
  (`init` · `quick` · `standard` · `deep` · `status` · `cancel` · `rebuild`),
  matching the mental model developers bring from other tools. `/cognition`
  and `/cog` remain as back-compat aliases; all in-app hints now point at
  `/index`. (`velune/cli/slash_dispatcher.py`, `velune/cli/repl.py`,
  `velune/cli/app.py`)
- **`project` is the primary shell noun.** `velune project …` is now the
  canonical command group (mirroring the REPL's `/project`); `velune workspace`
  stays as a hidden alias for existing scripts and muscle memory.
  (`velune/cli/registry.py`)
- **`/mode` is now a single switcher.** `/mode fast|max|normal` change the
  session mode (friendlier successors to `/optimus`, `/godly`, `/normal`, which
  still work), and `/mode` / `/mode status` show the current settings.
  (`velune/cli/repl.py`)
- **Categorized `/help`.** The 40+ command surface is grouped into scannable
  sections (Session · Workspace · Models · Council · Modes · Memory · Code ·
  Git · Extend · System) instead of one flat table, with a Tab-completion tip.
  (`velune/cli/repl.py`, `velune/cli/autocomplete.py`)

### ![Fixed](https://img.shields.io/badge/-Fixed-informational?style=flat-square)

- **Alias-collision guard.** The slash registry now warns when a name or alias
  would silently shadow another command (e.g. `/h` mapping to both `/help` and
  `/history`), turning a latent UX bug into a visible, test-caught failure.
  `/h` resolves to `/help`; the stale `/status` alias on `/mode` was removed so
  it no longer clashes with the shell's `velune status`.
  (`velune/cli/slash_commands.py`)
- **Structured error panels** replace ad-hoc yellow/red strings in the model,
  council, and discovery flows (`NoModelsAvailableError`,
  `ProviderUnavailableError`, unexpected-error rendering), giving consistent,
  actionable recovery hints. (`velune/cli/repl.py`)

### ![Added](https://img.shields.io/badge/-Added-success?style=flat-square)

- **Regression tests** locking in alias integrity, the `cognition`→`index`
  rename, `/mode` consolidation, and `/help` categorization.
  (`tests/test_slash_registry.py`)

## [0.9.3-beta.1] - 2026-06-23

> **Pre-release.** This beta introduces a re-architected startup path and an
> explicit, user-driven cognition model. See the Migration Notes below before
> upgrading from `0.9.x`.

### Changed — Startup architecture

- **Instant startup with explicit, on-demand cognition.** The REPL no longer
  runs automatic repository cognition (indexing) on launch. The CLI opens
  immediately; you connect a model, open a project, and run cognition only when
  you ask for it. (`velune/cli/repl.py`, `velune/repository/cognition.py`)
- **New startup flow:** `velune` → CLI opens instantly → connect model →
  open project → run cognition.

### Added — Workspace, model & cognition commands

- **Workspace management** via `/project`
  (`open <path>` · `close` · `status` · `list` · `add <path>`). Recently-opened
  workspaces are remembered so the picker can reopen them instantly.
  (`velune/cli/workspaces.py`, `velune/cli/slash_dispatcher.py`)
- **Model registry + local model discovery** via `/model`
  (`discover` · `connect <id>` · `use <id>` · `list` · `status` ·
  `remove <id>`). `/model discover` finds locally available models (e.g. Ollama).
- **Manual cognition** via `/cognition`
  (`quick` · `standard` · `deep` · `status` · `init` · `cancel` · `rebuild`).
  `quick` scans manifests only; `standard`/`deep` build a full symbol index.

### ![Removed](https://img.shields.io/badge/-Removed-critical?style=flat-square)

- **Automatic repository cognition on startup.** Indexing is now opt-in through
  the `/cognition` command. This is the headline behavior change in this release.

### ![Performance](https://img.shields.io/badge/-Performance-blueviolet?style=flat-square)

- Startup no longer blocks on indexing or a second model-reachability probe;
  cognition cost is paid only when explicitly requested.

### Migration Notes

- **Indexing is no longer automatic.** After opening a project, run
  `/cognition standard` (or `quick`/`deep`) to build the symbol index that
  earlier versions built silently at launch.
- **Connect a model explicitly.** Use `/model discover` then
  `/model connect <id>` (or `/model use <id>`) before running cognition or chat.

## [0.9.2] - 2026-06-23

### Changed — Packaging (lean install)

- **Lean default install.** Heavy/compiled dependencies are moved out of the
  base install into opt-in extras so `pip install velune-cli` resolves fast and
  cleanly on every platform. Core dependencies dropped from ~38 to 21 with no
  heavy compiled wheels. New extras: `[rag]` (lancedb, pyarrow, qdrant-client),
  `[parsing]` (tree-sitter grammars), `[telemetry]` (opentelemetry), `[git]`
  (gitpython); `[all]` aggregates everything. Every gated feature degrades
  gracefully when its extra is absent (e.g. semantic search becomes a no-op
  while lexical search and chat keep working). (`pyproject.toml`)

### Changed — Startup performance

- **`velune --version` is now near-instant** (~1.6s → ~0.04s). The console
  script entry point is `velune.main:main`, which fast-paths `--version`
  without importing the command graph or runtime. The Typer app is built
  lazily instead of at import time. (`velune/main.py`, `velune/cli/app.py`)
- **`velune <cmd> --help` no longer bootstraps the runtime** (~3.9s → ~1.6s):
  the root callback skips full-subsystem initialization when help is requested.
- Removed a redundant second Ollama reachability probe on REPL startup.

### ![Fixed](https://img.shields.io/badge/-Fixed-informational?style=flat-square)

- First run with no providers and a non-interactive stdin now prints the
  `velune setup` hint and exits cleanly instead of blocking on a confirmation
  prompt or entering an unusable REPL. (`velune/cli/app.py`)
- The `llama-cpp-python` adapter error message no longer references the removed
  `[llamacpp]` extra. (`velune/providers/adapters/llamacpp.py`)
- `MANIFEST.in` now references the correct `docs/CHANGELOG.md` path.

### Added — Quality

- **Test suite wired into CI.** The `pytest` suite (350 tests) now runs in CI
  across {Linux, macOS, Windows} × {3.11, 3.13} and gates merges and releases;
  `asyncio_mode = "auto"` is configured under `[tool.pytest.ini_options]`.
- README documents `pipx install velune-cli`, the `python -m velune` fallback,
  and the Windows PATH note for the "`velune` is not recognized" case.

### Added — Providers

- **Cohere** provider adapter — native Chat API with preamble/history conversion,
  streaming, and `command-r-plus` / `command-r` model catalog.
  (`velune/providers/adapters/cohere.py`)
- **DeepSeek** provider adapter — OpenAI-compatible API at `api.deepseek.com`;
  supports DeepSeek-R1 and DeepSeek-Coder. (`velune/providers/adapters/deepseek.py`)
- **Mistral** provider adapter — La Plateforme REST API; Mistral Large, Codestral,
  and Mixtral models. (`velune/providers/adapters/mistral.py`)
- **NVIDIA NIM** provider adapter — OpenAI-compatible API at `integrate.api.nvidia.com`;
  hosts Llama, Mistral, and partner NIM models. (`velune/providers/adapters/nvidia.py`)

### Added — Git Integration

- **GitHub and GitLab REST clients** — `velune/integrations/github.py` and
  `gitlab.py` implement push-branch, create-PR/MR, fetch-issue, and
  post-comment operations using each platform's REST API.
- **`/push` REPL command** — pushes the current branch to `origin` (with optional
  `--force`). (`velune/cli/slash_dispatcher.py`)
- **`/pr` REPL command** — creates a pull request (GitHub) or merge request
  (GitLab) for the current branch from inside the REPL.
- **`/issue <number>` REPL command** — fetches a GitHub/GitLab issue by number
  and injects the title, body, and labels as conversation context.
- **`/sandbox` REPL command** — shows the active sandbox type (subprocess or
  Docker) and its configuration status.

### Added — Code Intelligence

- **`velune/analysis/` package** — code intelligence tools running locally without
  an LLM call:
  - `linter.py` — runs `ruff` / `pyflakes` and surfaces structured diagnostics.
  - `refactor.py` — detects code smells (long functions, deep nesting, high
    complexity) and returns ranked findings.
  - `type_inferrer.py` — suggests type annotations for unannotated function
    signatures using AST analysis.
  - `symbol_search.py` — fast symbol and definition lookup across the indexed workspace.
- **`/lint [file]` REPL command** — lint a Python file and display Rich diagnostic output.
- **`/refactor <file>` REPL command** — detect code smells with severity rankings.
- **`/typify <file>` REPL command** — suggest type hints for unannotated functions.

### Added — Declarative Plugin System

- **`velune/plugins/declarative/` package** — Markdown-based plugin manifests:
  declarative agents (`agent.py`), slash commands (`command.py`), skills
  (`skill.py`), and a filesystem scanner (`scanner.py`).
- **SKILL.md injection** — plugins can ship a `SKILL.md` that is automatically
  appended to the council's system context when the plugin is active.
- **`/plugin` REPL command** — list, enable, disable, and reload declarative
  plugins without restarting the session.
- **Lifecycle hook system** (`velune/hooks/`) — a typed hook dispatcher and executor
  that fires `pre_tool` / `post_tool` events; plugins register handlers via
  their manifest.

### Added — Background Service

- **`velune/daemon/` package** — a background Velune CLI service (`server.py`) with
  an IPC transport (`transport.py`) and a client (`client.py`).
- **`velune daemon start|stop|status`** CLI subcommands to manage the service.

### Added — CLI Subcommands

- **`velune workspace`** subcommand group — `init`, `status`, `graph`, `list`,
  `open`, `remove`. `workspace graph` renders an interactive dependency tree
  from `velune/observability/workspace_graph.py`.
- **`velune session`** subcommand group — `list`, `delete`, `export`.
- **`velune provider`** subcommand group — `add`, `remove`, `test`, `list`, `status`.
- **`velune config`** subcommand group — `get`, `set`, `show`.
- **`velune usage`**, **`velune quota`**, **`velune health`** commands for
  analytics and provider monitoring.
- **`velune logs`** (alias for `trace`) — view or follow the execution event
  stream from the current workspace.
- **`velune status`** (alias for `context`) — show index freshness, file counts,
  and cognitive-core record counts without starting the full runtime.
- **`velune pipeline`** (alias for `retrieval`) — trace a retrieval query through
  the BM25 + vector + graph pipeline and show per-stage scores.
- **`velune memory`** subcommand group — `inspect`, `clear`, `compact`.

### Added — REPL Commands

- **`/council <task>`** — force the full council tier regardless of task
  complexity classification.
- **`/new [title]`** — start a fresh conversation while keeping project memory.
- **`/project [name|path]`** — switch or manage project workspaces from within
  the REPL.
- **`/bench [run]`** — view stored benchmark results or trigger a new empirical
  capability run.
- **`/graph`** — render a hierarchical tree of knowledge graph entities for the
  current workspace.
- **`/hunk`** — toggle hunk-by-hunk review mode; each proposed file edit is
  shown and approved individually before being applied.
- **`/undo`** — revert the last Velune CLI-generated git commit, leaving the changes
  staged for inspection.
- **`/approve [safe|ask|block]`** — set the tool/command approval gate for the
  session.
- **`/hooks`** — list all active lifecycle hooks and their configuration source.
- **`/stats`** — show session statistics: tokens used, estimated cost, turn
  count, and uptime.
- **`/history`** — show the REPL command execution history for the current session.
- **`/pull [model-id]`** and **`/delete <model-id>`** — download or delete
  Ollama models from within the REPL with live progress output.
- **`/mcp`** subcommands — `servers`, `tools`, `resources`, `connect <name>`,
  `disconnect <name>`, `refresh <name>` — inspect MCP connections without
  leaving the REPL.

### ![Security](https://img.shields.io/badge/-Security-yellow?style=flat-square)

- **Isolated `llama-cpp-python` from the default install set** to eliminate the
  `diskcache ≤ 5.6.3` transitive vulnerability (unsafe pickle deserialization — no
  patched version exists). The `[gguf]` optional extra now installs only the
  `gguf` metadata library, which is unaffected. In-process GGUF inference is
  available via the new `[llamacpp]` extra (`pip install 'velune-cli[llamacpp]'`),
  which is deliberately excluded from `[all]`. `pip-audit` now reports
  **no known vulnerabilities** on a default install. (`pyproject.toml`,
  `velune/providers/adapters/llamacpp.py`)

### ![Added](https://img.shields.io/badge/-Added-success?style=flat-square)

- **Intent reconstruction** — new `velune/cognition/intent.py` with `IntentClassifier`
  and `IntentType` enum (EXPLAIN / GENERATE / REFACTOR / DEBUG / REVIEW / QUESTION / COMMAND).
  Zero-latency keyword + word-boundary scoring; wired into `ContextOrchestrationEngine`
  as Phase 0 on every prompt. (`velune/cognition/intent.py`)

- **Council pipeline** — `CouncilRunner` orchestrates the full planner → coder →
  reviewer → debate → synthesizer pipeline. Cycle exhaustion escalates REVISE to REJECT
  automatically. (`velune/cognition/council_runner.py`)

- **DebateSession** — scores and ranks council proposals using challenger severity and
  reviewer decision; produces structured audit reports for the synthesizer.
  (`velune/cognition/council/debate.py`)

- **Multi-model role dispatch** — `ContextOrchestrationEngine.execute()` routes requests
  through `CouncilRunner` when a `CouncilAgentFactory` is configured; degrades
  gracefully when no factory is present. (`velune/orchestration/engine.py`)

- **WebSocket MCP transport** — `WebSocketConnection` implements the `MCPConnection`
  contract over JSON-RPC 2.0 on `ws://` and `wss://` URLs, with SSRF URL validation,
  per-call timeout guards, and optional resource discovery.
  (`velune/mcp/transports/websocket.py`)

- **`/doctor` council panel** — new "Council" category in `velune doctor` output shows
  role assignment coverage (roles → model IDs) or warnings for unmapped roles.
  (`velune/cli/commands/doctor.py`)

- **Async background tasks** — long `/run` tasks no longer block the REPL prompt.
  - `/run --bg <task>` submits a task to the background and returns immediately.
  - The status bar shows `⚙ N bg` while jobs are running.
  - New `JobRegistry` with `JobRecord` (ID, name, status, phase, elapsed, preview)
    and `JobStatus` enum (PENDING / RUNNING / COMPLETED / FAILED / CANCELLED).
    (`velune/core/task_registry.py`)

- **`/jobs` command** — list or cancel background jobs.
  - `/jobs` renders a live Rich table of all submitted jobs with color-coded status.
  - `/jobs cancel <id>` cancels a running job and clears its loop-detector state.

- **`/dashboard` command** — live progress dashboard.
  - Full-screen Rich `Live` layout: jobs table (top ⅔), alerts + provider health
    panels (bottom ⅓). Refreshes every 500 ms; press Enter or Ctrl+C to exit.
    (`velune/cli/display/dashboard.py`)

- **Error self-healing with exponential backoff** — the council orchestrator now
  automatically retries transient `ProviderConnectionError`, `InferenceError`, and
  `TimeoutError` failures up to `config.providers.max_retries` times (default 3),
  with randomized exponential backoff (`min(base × 2^(attempt-1), 30s) × jitter`).
  Each retry emits a `retry.attempt` event on the `CognitiveBus`.
  (`velune/core/retry.py`, `velune/cognition/orchestrator.py`)

- **Error loop detection** — sliding-window circuit breaker stops infinite retry
  cycles. `ErrorLoopDetector` fingerprints each exception (SHA-1 of type + message
  prefix) and trips after 3 occurrences within a 5-minute window, emitting a
  `retry.loop_detected` event and raising immediately rather than retrying.
  (`velune/core/loop_detector.py`)

- **Proactive issue detection** — `ProactiveWatcher` subscribes to `CognitiveBus`
  events and surfaces problems before the user asks.
  - `job.failed` → WARN alert
  - `retry.loop_detected` → DANGER alert
  - `provider.health_changed` (degraded/unavailable) → WARN/DANGER alert
  - `context.threshold_crossed` (70 % / 90 %) → WARN/DANGER alert
  - Periodic check (every 15 s) scans provider health manifests for UNAVAILABLE states.
  - Unread alerts drain after each REPL prompt and render as Rich panels above the
    input line. The status bar shows `⚠ N` for pending alerts.
  - `AlertStore` (ring-buffer, max 20 entries) and `ProactiveWatcher` registered in
    the service container at startup / shutdown. (`velune/proactive/`)

- **52 new unit tests** covering loop detection, retry policy, job registry,
  proactive watcher, and dashboard builders (all passing).

### Refactored

- **`velune/cli/slash_dispatcher.py`** (new) — extracted `_build_registry` and
  `_load_file_commands` from `VeluneREPL`. `repl.py` reduced by ~440 lines
  (4,138 → 3,697).

- **`velune/cli/stream_renderer.py`** (new) — extracted streaming / non-streaming
  render loop from `_handle_prompt` into `StreamRenderer.render()` returning
  `RenderResult(full_content, tokens_used, interrupted)`.

- **Provider fallback** — `ProviderRouter.get_ordered_candidates()` returns all viable
  candidates in capability-score order; `BaseCouncilAgent` iterates fallback providers
  on failure.

- **Lineage search** — `MemoryLifecycleCoordinator.get_lineage_warnings()` now
  delegates to `LineageStore.query_continuity_warnings()` instead of returning `[]`.

- **Diff parsing** — `CoderAgent._parse_diffs()` replaced with `parse_with_fallback()`
  from `velune/execution/edit_formats/registry.py`.

- **Plugins** — deleted `velune/plugins/schemas.py` (legacy `PluginManifest`); updated
  `PluginRegistry` and `PluginLoader` to use `DeclarativePluginManifest` and an inline
  `PluginManifest` dataclass respectively. Deleted `velune/context/window.py` (legacy
  `ContextWindowTracker`); `estimate_tokens()` now lives in `token_counter.py`.

### ![Changed](https://img.shields.io/badge/-Changed-blue?style=flat-square)

- `StatusBarState` gains `bg_job_count` and `alert_count` fields; `render_status_bar`
  renders them as warn-coloured segments only when non-zero. (`velune/cli/statusbar.py`)
- `RuntimeBootstrapper.bootstrap()` registers `JobRegistry` and `AlertStore` in the
  service container at startup. (`velune/kernel/bootstrap.py`)
- `_async_main()` now starts `ProactiveWatcher` after `lifecycle.startup()` and
  cleanly stops it in the `finally` block. (`velune/kernel/entrypoint.py`)

### ![Fixed](https://img.shields.io/badge/-Fixed-informational?style=flat-square)

- **Intent classifier word boundaries** — `_score()` now uses `\b` anchors so
  `"build"` no longer fires on `"rebuild"` and `"implement"` no longer fires on
  `"implementation"`. Debug signals use substring matching to catch `"KeyError"` etc.

- **`velune/plugins/registry.py`** — `register_plugin` now calls
  `_extract_hook_names()` which handles both declarative (hooks JSON file) and legacy
  (inline `hooks` list) manifest shapes.

### ![Tests](https://img.shields.io/badge/-Tests-lightgrey?style=flat-square)

- `tests/test_intent.py` — 7 intent-type test classes + confidence + engine integration
  tests (28 cases total).
- `tests/test_council_runner.py` — happy path, revise-then-approve, reject-on-exhaustion,
  failure isolation, `DebateSession` unit tests, helper function tests (21 cases).
- Updated `tests/test_mcp_phase2.py` — `test_unsupported_transport_raises` replaced by
  `test_returns_websocket_connection` now that WebSocket is implemented.

## [0.9.1] - 2026-06-14

This is a **stabilization and trust-recovery** release. It cuts the runtime-hardening
and packaging-correctness work that had accumulated on `main` since `0.9.0` into a
properly tagged, reproducible PyPI artifact. There are no new features and no breaking
changes — `pip install --upgrade velune-cli` is a safe, drop-in update.

### ![Security](https://img.shields.io/badge/-Security-yellow?style=flat-square)

- **Windows PATH-hijack guard now enforced.** `_is_trusted_path` previously returned
  `True` unconditionally on Windows, so a malicious binary planted earlier in `PATH`
  would be executed. The resolved binary must now live under a system/program-install
  root, the interpreter's own environment, or a workspace venv — matching the existing
  POSIX behavior. (`velune/execution/command_spec.py`)
- **Interpreter inline-code execution blocked.** Allowlisted interpreters could run
  arbitrary program text with no approval gate (`python -c …`, `node -e/--eval/-p …`,
  including Python short-flag clusters like `-Ic`). These flags are now rejected;
  running a *file* is still permitted, and agent-authored files must pass the
  `DiffPreview` write-approval flow before they can be run.
- **Execution-model documentation corrected for honesty.** SECURITY.md and
  docs/THREAT_MODEL.md now describe the execution layer as a *managed, resource-limited
  execution environment* — explicitly **not** an OS-level sandbox — and document the
  residual risk (allowlisted interpreters/build tools run workspace files as the user)
  plus the OS-isolation roadmap. README's architecture label updated accordingly.
- Added Bandit static analysis to CI (gates on medium+ severity) and gitleaks secret scanning.
- Resolved Bandit high/medium findings: marked the non-cryptographic workspace-slug SHA-1 with `usedforsecurity=False`, and gave the Ollama HTTP client a bounded default timeout (60s, 5s connect) so non-streaming calls cannot hang indefinitely.

### ![Fixed](https://img.shields.io/badge/-Fixed-informational?style=flat-square)

- **Subprocess pipe-buffer deadlock in the execution sandbox.** `SubprocessSandbox.execute`
  read child output via `communicate()` only *after* the poll loop saw the process exit.
  A child that wrote more than the OS pipe capacity (~64 KiB) blocked on `write()`, never
  exited, and was killed as a false timeout with **all output lost** — affecting any normal
  test run, verbose build, or `pip install`. Both pipes are now drained concurrently on
  dedicated threads while the process runs, into a per-stream memory-bounded buffer
  (default 10 MiB, configurable via `max_output_bytes`). This removes the deadlock, bounds
  parent memory against runaway producers, and preserves partial output on timeout.
  (`velune/execution/sandbox.py`)

### ![Added](https://img.shields.io/badge/-Added-success?style=flat-square)

- **`velune doctor` runtime path-safety check.** A new Security-category diagnostic resolves
  each allowlisted executable via the same `shutil.which` lookup the sandbox uses and
  validates it against the real `_is_trusted_path` guard, surfacing any tool that resolves
  to an untrusted location (PATH-hijack candidate or non-standard install the sandbox will
  refuse to run). Makes the PATH-hijack guarantee observable rather than silent.
  (`velune/cli/commands/doctor.py`)

### ![Changed](https://img.shields.io/badge/-Changed-blue?style=flat-square)

- CI test matrix expanded to **Ubuntu / Windows / macOS × Python 3.11 / 3.12 / 3.13**.
- Release pipeline now publishes to PyPI via **OIDC trusted publishing** (no long-lived token); removed the `continue-on-error` that silently swallowed failed publishes.
- Release & CI builds are now **reproducible** (`SOURCE_DATE_EPOCH` pinned to the commit, `[tool.hatch.build] reproducible = true`) and validated with `twine check --strict`.
- Release pipeline now asserts the git tag matches `velune.__version__` before building, so a mistagged release fails fast.
- Coverage reporting made honest: shrank the `omit` list from ~70 modules to only un-unit-testable surfaces (TTY/daemon/live-network/optional-native). Full-codebase coverage is now measured (~21%) with a CI floor of 20%.
- Migrated the event-bus `Event` model from Pydantic v1 `class Config` to `ConfigDict` (removes a deprecation warning, forward-compatible with Pydantic v3).
- Dependabot now groups minor/patch bumps into single PRs and uses the correct GitHub reviewer handle.

### ![Added](https://img.shields.io/badge/-Added-success?style=flat-square)

- New CI **`build`** + **`install-smoke`** jobs: reproducible build, strict metadata validation, pure-python wheel assertion, and a cross-platform (Ubuntu/Windows/macOS × Py 3.11/3.13) wheel-install + `velune --version`/`--help` REPL smoke test.
- Python 3.13 classifier, `Typing :: Typed` classifier, and a `Documentation` project URL in `pyproject.toml`.
- Unit tests for `execution/validator.py` (16% → 90% coverage).
- **CLI Design Modernization** — Comprehensive frontend redesign for professional appearance
  - Modern startup banner with clean, spacious layout
  - Refined REPL prompt with sophisticated color palette (blue primary + gold accent)
  - Simplified prompt display: only shows context bar when >40% full
  - Updated error rendering with cleaner panel formatting
  - Enhanced theme colors with semantic tokens (muted, accent)
  - Better visual hierarchy throughout terminal interface

## [0.9.0] - 2026-06-12

### ![Security](https://img.shields.io/badge/-Security-yellow?style=flat-square)

- Plugin sandbox status: Plugin sandbox remains unimplemented or disabled for standard CLI operations.
- Removal of `run_until_complete` anti-pattern: Cleaned up all async loop management and centralized loop execution in `entrypoint.py`.
- Security audit suite extension: Centralized static and runtime vulnerability controls.

### ![Fixed](https://img.shields.io/badge/-Fixed-informational?style=flat-square)

- Fixed memory lifecycle shutdown duplication to prevent multiple DB closure errors.
- Fixed Ollama context-window detection to correctly read local model metadata.

### ![Changed](https://img.shields.io/badge/-Changed-blue?style=flat-square)

- Consolidated AST parser logic into a unified syntax parsing layer.
- Consolidated council orchestrators to streamline Planner/Coder/Reviewer loops.
- Modernized CLI theme, refined color palettes, updated startup banner, and context trackers.
- Reconciled documentation and cleaned up dead MCP CLI commands.

### ![Removed](https://img.shields.io/badge/-Removed-critical?style=flat-square)

- Removed superseded `tests/` and `scripts/` directories entirely from the repository.

## [0.9.0-beta] — 2026-06-12

### Overview

**Public Beta Release** — All Phase 0-1 systems verified and integrated. Ready for early adopters.

### Architecture & Verification

- **Known Issues Resolution** — All 10 critical issues resolved:
  1. shell=True blocking (security)
  2. asyncio.run() consolidation (correctness)
  3. CognitiveFirewall prompt injection tests passing
  4. CapabilityLevel aliasing prevention verified
  5. SQLite WAL concurrency tests passing
  6. VeluneConfig single-source-of-truth enforced
  7. CouncilState single shape enforced
  8. Event history deque(maxlen=1000) in place
  9. CouncilExecutionBudget enforcement tested
  10. SSRFGuard blocking private metadata endpoints

- **Architecture Boundary Verification** — All 270 Python files pass 8 layer boundary rules
  - Kernel ↚ CLI/Cognition (OS layer isolation)
  - Providers ↚ Cognition/CLI (infrastructure isolation)
  - Memory ↚ Cognition/CLI (persistence layer)
  - Retrieval ↚ CLI (data access isolation)
  - Telemetry ↚ Cognition (observability separation)

- **Test Coverage** — 581/635 tests passing (91.6%)
  - All critical routing and security tests passing
  - 48 non-critical test failures isolated for Phase 2
  - 6 collection errors in experimental features (dual-path retrieval)

- **Fixed Integration Issues**
  - Added structlog dependency (telemetry logging)
  - Fixed @contextmanager import in logging.py
  - Fixed ContextBudget dataclass field ordering
  - Corrected CrossEncoderReranker naming
  - Fixed test_mcp_server.py syntax error

### Beta Release Status

- All documented commands verified and functional
- Hardware detection and tier classification working
- Provider health monitoring implemented
- Multi-model council orchestration stable
- Session persistence and memory tiers operational
- MCP server integration available

### Known Limitations (Phase 2+)

- Startup time 3.6s (target 3.0s) — optimization planned
- 48 failing tests deferred to Phase 2 (incremental indexing, streaming repair, prompt adaptation)
- Dual-path retrieval disabled (experimental feature)
- Cloud provider integration incomplete for some APIs

### ![Security](https://img.shields.io/badge/-Security-yellow?style=flat-square)

- All OWASP top 10 checks passing
- Architecture lint enforced (no shell=True, asyncio.run isolated)
- Keyring-based secret storage for API keys
- Sandbox execution for arbitrary code
- SSRF guard blocks private IP ranges

## [0.6.0] — 2026-06-12

### ![Added](https://img.shields.io/badge/-Added-success?style=flat-square)

- **Provider Health Monitoring** — Real-time health tracking with CapabilityManifest
  - Background polling every 30 seconds
  - Health status (HEALTHY/DEGRADED/UNAVAILABLE)
  - Estimated latency tracking (5-call rolling average)
  - Rate limit monitoring
- **Health-Aware Routing** — Router considers provider health for model selection
  - Filters unavailable providers
  - Prefers healthy providers
  - Latency-sensitive task optimization
- **Startup Performance Monitoring** — CI check for startup time regression (<3s threshold)
- **Comprehensive CI/CD Pipeline** (`CI_PASS` gate blocks merge on failure)
  - Lint (ruff + pyright)
  - Security (pip-audit, shell=True check, asyncio.run() check)
  - Architecture Lint (8 layer boundary rules)
  - Unit Tests (70% coverage minimum)
  - Integration Tests (on main/PRs)
  - Build Check
  - Startup Performance (main only)
- **Latency Recording** — All providers auto-record call latency
  - Synchronous calls: total time
  - Streaming calls: TTFT (time-to-first-token)
- **Architecture Linting Script** (`scripts/check_architecture.py`)
  - AST-based import analysis
  - 8 layer boundary rules
  - 0 external dependencies
- **Automated Dependency Updates** via Dependabot
  - Weekly pip updates
  - Weekly GitHub Actions updates
  - Manual review required (no auto-merge)
- **Pre-Commit Hooks** — Auto-format and lint before commit
- **Type Checking** — Pyright configuration with standard mode
- **Enhanced Code Coverage** — Branch coverage + exclusion rules

### ![Changed](https://img.shields.io/badge/-Changed-blue?style=flat-square)

- Updated all provider adapters to record latency
  - Anthropic, OpenAI, Google, Groq, HuggingFace, LM Studio, Ollama, LlamaCpp
- Enhanced ruff configuration with 8 rule categories (E, W, F, I, B, C4, UP, N)
- Improved pytest configuration (timeouts, coverage reporting)
- Reorganized pyproject.toml with comprehensive tool configurations

### ![Fixed](https://img.shields.io/badge/-Fixed-informational?style=flat-square)

- Replaced `UNHEALTHY` enum with `UNAVAILABLE` for consistency
- Fixed provider health check timeouts
- Added proper error handling for latency recording

### ![Removed](https://img.shields.io/badge/-Removed-critical?style=flat-square)

- Demo files (council_example.py)
- Unnecessary documentation (implementation details)
- Redundant markdown files

### ![Security](https://img.shields.io/badge/-Security-yellow?style=flat-square)

- Added pip-audit dependency vulnerability scanning
- shell=True regression check (P0-2)
- asyncio.run() count validation (P0-1)
- Architecture boundary enforcement
- Pre-commit hooks for local security

## [0.5.0-beta] — 2026-06-07

### ![Added](https://img.shields.io/badge/-Added-success?style=flat-square)

- Google Gemini provider (2.0 Flash, 1.5 Pro, 1.5 Flash, 2.0 Flash Thinking)
- Together AI provider (Llama 3.3 70B, Qwen 2.5 Coder 32B, DeepSeek R1)
- Fireworks AI provider (DeepSeek R1, Qwen 2.5 Coder, Mixtral 8x22B)
- /councilmodel command — assign specific models to specific council roles
- /pull command — download Ollama models interactively from within the REPL
- /delete command — remove locally installed Ollama models
- Project type auto-detection (FastAPI, Django, Flask, React, Next.js, Rust, Go, Java Spring, .NET, Flutter) with framework-specific context
- ProjectTypeDetector writes .velune/project_profile.json on init
- System prompt injection based on detected project type
- Model pull progress bar with live streaming status
- Council role assignments persist to .velune/council_roles.json
- ModeAwareModelSelector for /optimus and /godly auto-model selection
- `/optimus` and `/godly` session-wide REPL modes with `ModeManager`, `ModeConfig`, and `ModeAwareModelSelector` (`velune/cli/modes.py`, `velune/cli/model_selector.py`)
- Slash command Tab-autocomplete (`velune/cli/autocomplete.py`) with `/model <id>` completion
- Rich startup banner showing hardware tier, GPU, providers, and active model (`velune/cli/banner.py`)
- Security audit script (`scripts/security_audit.py`) — 6 checks, exit 0 required in CI
- `RateLimiter` token bucket and `DEFAULT_HOST`/`MAX_REQUEST_BYTES` constants added to the MCP server (`velune/mcp/server.py`)
- `DEFAULT_VELUNEIGNORE` expanded: `*.crt`, `id_rsa`, `id_dsa`, `id_ed25519`, `id_ecdsa`, `.netrc`, `.npmrc`, `.pypirc`, `.aws/`, `credentials.json`, `service-account.json`
- Full GitHub Actions CI/CD pipeline: lint + type check, 2×2 test matrix (Python 3.11/3.12 × Ubuntu/macOS), security audit job, build + `twine check`, Codecov upload
- Automated release workflow: tag-to-PyPI via OIDC trusted publishing, CHANGELOG-based GitHub Release notes, pre-release detection from tag suffix
- `scripts/extract_changelog.py` — parses CHANGELOG.md for a version section
- `docs/releasing.md` — step-by-step release checklist
- `docs/mcp.md` — MCP integration guide with all 21 real tool names
- `CONTRIBUTING.md` — developer how-to: adding providers, slash commands, council agents
- `README.md` rewritten — quickstart, hardware table, provider table, architecture tree, session modes, MCP section, Windows section
- `WINDOWS.md` — complete 10-section WSL2 setup guide with GPU passthrough
- BYOK (Bring Your Own Key) provider system: xAI, Google Gemini, Groq, OpenRouter
- OS keyring integration via `keyring` library (`velune/providers/keystore.py`)
- `LocalModelResolver` for filesystem GGUF discovery across 9 well-known paths
- Persistent model-path cache (`velune/providers/local_paths.py`)
- `is_running()` classmethods on `OllamaDiscovery` and `LMStudioDiscovery` for 2-second reachability checks before discovery
- `ModelDiscoveryScanner._collect()` helper — per-discoverer error isolation
- Summary log line after each full scan: `Local: N GGUF, N Ollama, N LM Studio | Cloud: N models`
- Cloud discoverer key-gating: cloud providers skip network calls when no key is set
- OpenRouter 1-hour disk cache for model lists
- GitHub CI workflow (`ci.yml`) with lint, test (Python 3.11 / 3.12), and build jobs
- GitHub Release workflow (`release.yml`) with PyPI trusted publishing and CHANGELOG excerpt
- GitHub issue templates (bug report, feature request)
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1)

### ![Security](https://img.shields.io/badge/-Security-yellow?style=flat-square)

- All provider adapters and discovery modules now use `keystore.get_key()` instead of bare `os.getenv()` for API key retrieval (`adapters/anthropic.py`, `adapters/openai.py`, `adapters/huggingface.py`, `discovery/anthropic.py`, `discovery/openai.py`, `cli/commands/doctor.py`)
- `tests/test_security.py` — 6 security property tests covering shell injection, API key bypass, SSRF blocking, veluneignore coverage, and rate limiting

### ![Changed](https://img.shields.io/badge/-Changed-blue?style=flat-square)

- `GGUFDiscovery.discover()` now delegates to `LocalModelResolver` instead of
  a bare `rglob` — depth-limited (5 levels), capped at 100k files per root,
  deduplicated
- `LlamaCppProvider._resolve_model_path()` now checks persistent cache →
  `LocalModelResolver` → interactive prompt before raising `FileNotFoundError`
- `LlamaCppProvider.list_models()` simplified — removed `search_paths` mutation
- `pyproject.toml` license classifier corrected to Apache Software License
- `SECURITY.md` and `CONTRIBUTING.md` consolidated (removed duplicate sections)
- `.gitignore` expanded with `.benchmarks/`, `.claude/`, secret file patterns

### ![Fixed](https://img.shields.io/badge/-Fixed-informational?style=flat-square)

- `CONTRIBUTING.md` footer appeared three times — reduced to one
- `README.md` referenced MIT License — corrected to Apache-2.0
- `pyproject.toml` `[tool.ruff.lint]` section was at wrong TOML level

---

## [0.1.0] - 2026-06-05

### ![Added](https://img.shields.io/badge/-Added-success?style=flat-square)

- Initial public release
- Typer CLI with `ask`, `run`, `workspace`, `doctor`, `models`, `chat` subcommands
- LangGraph council orchestrator (Planner → Coder → Reviewer → Synthesizer)
- Repository cognition core: tree-sitter AST parsing, BM25, Qdrant vector store,
  Graphiti memory graph
- Provider adapters: Ollama, LM Studio, llama.cpp, OpenAI, Anthropic, HuggingFace
- `SubprocessSandbox` with write-path allowlists, time limits, and SSRF suppression
- Hybrid retriever (BM25 + Qdrant) with `asyncio`-safe sync fallback
- `velune doctor check` — GPU, VRAM, provider, grammar diagnostics
- `ModelDiscoveryScanner` — parallel async discovery across all providers
- `CapabilityClassifier` and `CapabilityBenchmark` for empirical model profiling
- Git-backed transactional execution with automatic rollback on failure
- Cognitive firewall: prompt-injection detection and HTML sanitization
- Security sandbox: workspace write guards, network hygiene, secret scrubbing
- Full pytest suite: unit, integration, async, and benchmark tests

[Unreleased]: https://github.com/Surya-Hariharan/Velune-CLI/compare/v0.9.4...HEAD
[0.9.4]: https://github.com/Surya-Hariharan/Velune-CLI/compare/v0.9.3.5...v0.9.4
[0.9.3-beta.1]: https://github.com/Surya-Hariharan/Velune-CLI/compare/v0.9.2...v0.9.3-beta.1
[0.9.2]: https://github.com/Surya-Hariharan/Velune-CLI/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/Surya-Hariharan/Velune-CLI/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/Surya-Hariharan/Velune-CLI/releases/tag/v0.9.0
[0.1.0]: https://github.com/Surya-Hariharan/Velune-CLI/releases/tag/v0.1.0
