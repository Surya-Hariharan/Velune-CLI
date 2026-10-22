# Manual Windows Terminal verification

The automated suite proves behavior and cell-level rendering through a PTY and a
VT emulator. It cannot show real fonts, glyph widths, true colour, the terminal's
own padding/scrollbar, scrollback, or how Windows Terminal (WT) handles resize and
exit. Run this checklist in a real WT window before a release that touches the UI.

## Setup (once)

1. **Isolate it.** Use a throwaway sandbox so nothing touches your real config or
   keyring, and a fake backend so nothing leaves the machine. The scripts the
   harness uses are the model: `tests/tui/_launcher.py` (redirects the config dir,
   swaps in an in-memory keyring, reroutes non-loopback HTTP) and
   `tests/tui/fake_backend.py`. Start the backend, then in WT run:
   ```
   set VELUNE_TUI_SANDBOX=<tempdir>\sandbox
   set VELUNE_DATA_HOME=<tempdir>\sandbox\data
   set VELUNE_MASTER_PASSPHRASE=qa-passphrase
   set OLLAMA_HOST=<backend host:port>
   set VELUNE_TUI_FAKE_HOST=<backend host:port>
   .venv\Scripts\python tests\tui\_launcher.py
   ```
2. **Clear `NO_COLOR`** (`set NO_COLOR=`). VELUNE honors it and renders monochrome;
   that is correct, but it is not what a default WT user sees. Also run the
   checklist once with `NO_COLOR=1` and confirm the UI stays legible.
3. Use only the fake key `sk-velune-test-0000000000000000FAKE`. Never a real key.
4. Note WT version, font, colour scheme, and display scaling in your report.

Capturing screenshots from a script? Make the capturing process DPI-aware
(`SetProcessDPIAware`) before reading the window rectangle, or the image is a
cropped top-left corner on scaled displays. Capture only the WT window, and send
synthetic input only after verifying WT is the foreground window.

## Checklist

Mark each item pass/fail and attach a screenshot for any failure.

**Initial launch and first-run wizard**
- [ ] Wizard appears; step list, selection marker, and footer render; no clipping.
- [ ] Up/Down move the selection; Enter advances; Esc steps back one step.
- [ ] Long description lines do not break mid-word (known: the Welcome description
      overflows its pane and the terminal hard-wraps it).
- [ ] "Skip Setup" then "Continue to REPL" lands on the home screen.
- [ ] Banner gradient, status lines, and the version render; cursor is in the composer.

**Prompt submission and streamed responses**
- [ ] Type a prompt, Enter: echo line, streamed reply, token line, status bar updates.
- [ ] Composer is empty and focused afterwards; typing works immediately.
- [ ] Multi-line (Shift+Enter) input and a long reply render without stale lines.

**/help and slash-command autocomplete**
- [ ] Typing `/hel` opens the palette; filtering narrows results; Esc closes it.
- [ ] Enter on a matching command runs it; `/help` table wraps at word boundaries.
- [ ] The footer hint line under `/help` (known: hard-wraps mid-word on narrow windows).

**/connect and API-key masking**
- [ ] Picker lists providers; typing filters; Enter selects.
- [ ] Key prompt shows only `*` per character; the fake key never appears on screen.
- [ ] Bad/malformed key: clear error, "Try another key / Save anyway / Cancel".
- [ ] Right pane text is not clipped at your window width (known: the key prompt's
      "input is hidden" and the picker's footer clip below ~110 columns).

**Popup cancellation and return to the composer**
- [ ] Esc from the picker and from the key prompt: popup fully gone, composer empty,
      cursor on the composer, no leftover border cells or key text.
- [ ] One `❯ /connect` echo line remains per cancel (intended).
- [ ] Reopen the picker 5 times: identical each time, no accumulating artifacts.

**Unknown commands**
- [ ] `/zzzbogus` + Enter: red "Unknown command: /zzzbogus  Type /help…", empty composer.
- [ ] Palette does not stay stuck on "No matching commands".

**Narrow / wide resizing**
- [ ] Drag the window to ~60 columns: status bar truncates cleanly, composer stays usable.
- [ ] Drag wide (>180 columns): layout fills sensibly, nothing stale at the right edge.
- [ ] New output after a resize wraps at the new width (already-printed history is
      re-wrapped by WT and may split words; that is terminal behavior).
- [ ] Right edge: with your colour scheme, no visible stripe at the last column
      (the app deliberately leaves one column unpainted; default WT hides it, a
      light or high-contrast scheme may not).

**Ctrl+C, Ctrl+D, and /exit**
- [ ] Ctrl+C with text: clears the composer, stays running.
- [ ] Ctrl+C twice on an empty composer: exits.
- [ ] Ctrl+D: currently ignored (session intact). Record if that changes.
- [ ] `/exit`: exits with code 0.

**Terminal restoration after exit**
- [ ] Alt-screen is gone and the shell's earlier output is visible.
- [ ] Cursor is visible and blinking normally; typing in the shell echoes.
- [ ] Mouse wheel scrolls WT scrollback (mouse reporting off); paste works normally.
- [ ] Nothing odd printed after exit (known: a `WARNING:velune.cli.handlers.councilmodel:`
      line can appear on the main screen after the wizard; see findings).
