# Terminal UI audit harness (`tests/tui`)

Drives the **real `velune` program inside a pseudo-terminal**, sends real
keystrokes, and asserts on the emulated screen. This is different from the
in-process prompt_toolkit tests elsewhere in `tests/` (which build widgets over
a pipe): these exercise the actual entry point, onboarding wizard, `/connect`
key flow, streaming, navigation, resize and exit, exactly as a user sees them.

```
pytest tests/tui                 # run everything (journeys boot the CLI, so ~4 min)
pytest tests/tui/test_frames.py  # comparator self-tests only (instant, no PTY)
pytest tests/tui -k connect      # one area
pytest -m "not tui"              # skip them in the fast loop
VELUNE_TUI_ARTIFACTS=out pytest tests/tui   # where transient output goes (default: system temp)
```

Real-terminal checks that automation cannot make live in [`MANUAL_QA.md`](MANUAL_QA.md).

Needs the `dev` extra (`pywinpty` on Windows, `ptyprocess` elsewhere, `pyte`).
Without a PTY backend the tests skip rather than fail.

## How it works

| Piece | Role |
|---|---|
| `harness.py` | `TermSession`: spawns the CLI under ConPTY/PTY, a reader thread feeds a `pyte` emulator; `wait_for`, `type`, `key`, `resize`, `settle`, `snapshot` |
| `_launcher.py` | Child entry point. Redirects `platformdirs` config dir and swaps the OS keyring for an in-memory one **before** importing velune, and reroutes every non-loopback `httpx` request to the fake backend. No app code is modified |
| `fake_backend.py` | Local HTTP server: Ollama routes + OpenAI-shaped routes. `FAKE_KEY` is the only bearer it accepts; replies are chosen by markers in the prompt (`LONGREPLY`, `MULTILINE`) |
| `flows.py` | Reusable journeys: skip onboarding, `/connect`, ask, wait for idle |
| `frames.py` | Normalized cell-level frame capture, baselines, and row/column/attribute diffs |
| `baselines/*.json` | Approved reference frames (the only fixtures stored in the repo) |
| `test_journeys.py` | Behavior journeys: prompts, slash commands, `/connect`, navigation, exit |
| `test_visual.py` | Baseline comparisons and popup-geometry checks on the real REPL |
| `test_frames.py` | Self-tests proving the comparator fails on a changed glyph/style/cursor/size |

### Safety

* `FAKE_KEY` is `sk-velune-test-…FAKE`; it can never authenticate anywhere.
* Provider `*_API_KEY` env vars are stripped from the child environment.
* The child gets a throwaway sandbox for config, data, and keyring; the real
  Credential Manager / `%LOCALAPPDATA%\Velune` are never touched.
* Cloud calls are rerouted to `127.0.0.1`; the fake backend records the original
  host (`X-Velune-Orig-Host`) and tests assert nothing else was contacted.
* `snapshot()` redacts key-shaped strings, and a test scans every artifact for
  the fake key.

## Visual regression

`assert_frame(session, "name")` captures the emulated screen as terminal *cells*
(character plus fg, bg, bold, italics, underline, strike, reverse) and the cursor,
masks legitimately dynamic values, and compares to `baselines/name.json`.

* **Masked** (replaced by fixed tokens): temp/workspace paths, `vX.Y.Z`, token and
  cost counts, `n/128k` context, elapsed times, clock times, key-shaped strings.
  A row that contained a mask is compared on text only, not style.
* **Compared**: every other row's text and per-column style, cursor row/column and
  visibility, and terminal size. Pass `cursor=False` where it trails dynamic text.
* **Failure output** names the row, the column range, expected vs actual text, and
  the attribute change (`col 3: style a78bfb/0a0a0a+b -> a78bfa/0a0a0a+b`). The
  full actual frame is written to the system temp dir, never the repo.
* A **missing baseline fails**; it is never auto-created.
* There is deliberately **no pixel/image diff**: the SVG/PNG renderings are drawn
  from these same cells, so a pixel diff would only add font-rasterization noise
  from our reconstruction, not information about a real terminal.

### Regenerating baselines safely

1. Run the suite and read the failure diff. Decide whether the change is intended.
2. Re-record only what you mean to: `VELUNE_TUI_UPDATE_BASELINES=1 pytest tests/tui/test_visual.py -k <name>`.
3. **Review `git diff tests/tui/baselines`** — one row per line, so changes read
   like a normal text diff. An unintended glyph, colour or cursor shift is a bug,
   not a baseline to refresh.
4. `test_shipped_baselines_are_sanitized` rejects secrets and machine-specific
   paths; keep it green. Commit baseline changes together with the code change.

Baselines depend on the registered command list (`/help` is part of two of them),
so adding or renaming a command legitimately changes `picker_over_help` and
`key_prompt_over_help`. Baselines are recorded at fixed sizes (120x40, 80x30, 60x24).

### Transient screenshots

`snapshot(name)` writes `<name>.txt` and a colored `<name>.svg` to the system temp
dir (`$TMP/velune-tui-artifacts`, or `VELUNE_TUI_ARTIFACTS`) for human review.
They are debugging aids, not assertions, and never live in the repository.
Open an SVG in a browser, or render it with Edge/Chrome headless, to look at it.

## Known limits

* `pyte` has no alternate-screen buffer, so tests see the live frame only.
  Terminal restoration after exit is verified from the **raw escape stream**
  (every `?1049/?1000/?1006/?2004` enable has a matching disable, cursor shown),
  not from the shell's scrollback.
* Real terminals differ (fonts, glyph widths, true-color support). This proves
  behavior under ConPTY + a VT emulator; it does not replace a look in Windows
  Terminal, iTerm2, etc. for pixel-level judgments.
* Ollama is only partly `OLLAMA_HOST`-aware: the chat adapter, discovery and
  `ollama_manager` honor it, but the credential validator the wizard and
  `/connect` use (`providers/validation.py`) and the liveness probe
  `keystore.is_ollama_live` hardcode `localhost:11434`. Wizard-driven local-model
  setup therefore cannot be pointed at the fake; chat journeys connect a cloud
  provider through `/connect` instead. (Reported, not changed.)
* These are frames reconstructed from the PTY byte stream through `pyte`, not
  screenshots of a real Windows Terminal session.
* POSIX support (`ptyprocess`) is implemented but has only been exercised on
  Windows so far, so on non-Windows the suite skips unless `VELUNE_TUI_POSIX=1`.
* Terminal restoration is checked from the escape stream the program wrote
  (modes off, last event is the disable, cursor visible). It cannot observe the
  host console's own mode flags (Windows) or termios state (POSIX).
