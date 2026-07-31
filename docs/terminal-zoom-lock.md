# Terminal zoom lock: investigation and outcome

**Requested:** lock the host terminal's font-zoom controls (`Ctrl +`, `Ctrl
-`, `Ctrl 0`, `Ctrl+scroll`, and platform equivalents) for the duration of a
`velune` session, reverting automatically on exit, without touching any
global terminal or OS configuration.

**Outcome: not implemented.** After investigating every terminal in scope,
none of them expose a way for a foreground/child process to suppress its own
zoom shortcuts on a session-scoped basis. Velune instead **detects** the
host terminal and **reports** this limitation — visible via `velune doctor`
— rather than silently doing nothing or attempting an unreliable workaround.

## Why this is a structural limitation, not a missing feature

A terminal emulator is a GUI (or console-host) application that owns a
window, a keyboard/mouse event loop, and a pseudo-terminal (PTY) it drives.
`velune` runs as the *child* process attached to the other end of that PTY.
The only thing that ever reaches `velune` is the byte stream the emulator
chooses to write to the PTY's input side — normal characters, and any
escape sequences the emulator decides to forward (arrow keys, mouse reports,
etc., only if the app has asked for them via the appropriate DEC private
modes).

Font-zoom shortcuts are, in every emulator investigated, resolved entirely
*inside the emulator's own input layer* — its window-message loop, its GUI
toolkit's accelerator table, or (for the legacy Windows console) the console
host's own key handling — **before** the emulator ever considers writing
anything to the PTY. From `velune`'s side of the PTY, a zoom keypress or
Ctrl+wheel scroll doesn't exist: no bytes are sent, so there is no key event,
mouse report, or signal for the child process to intercept, rebind, or
suppress. There is also no ANSI/DEC control sequence an application can send
*upstream* to tell the emulator "please stop handling one of your own
keybindings for a while" — escape sequences only configure how the emulator
renders and reports things *to* the application, not the other way around.

The one lever that does exist in every case is the emulator's own
persistent, global keybinding configuration (a settings file, a registry
key, or a system preferences panel). Editing that is explicitly out of scope
here — it isn't session-scoped (it would need to be reverted correctly on
every possible exit path: normal exit, crash, `kill -9`, terminal closed
out from under the process), and the requirements explicitly rule out
touching global terminal or OS configuration.

## Per-terminal findings

| Terminal | Zoom shortcuts | Where they're resolved | Session-scoped app API? |
|---|---|---|---|
| Windows Terminal | `Ctrl +/-/0`, `Ctrl+scroll` | Built-in `increaseFontSize`/`decreaseFontSize`/`resetFontSize` actions in Windows Terminal's own (XAML) input handling, before ConPTY forwards anything | No — only the user's persistent `settings.json` `actions` list |
| PowerShell / CMD (legacy Console Host, `conhost.exe`) | `Ctrl+scroll` | `conhost.exe`'s own font-resize handling | No — only the console window's persistent Properties dialog or `HKCU\Console` registry keys. `SetConsoleMode`/`GetConsoleMode` govern how input is *reported* to an app, not whether this host gesture fires at all |
| Windows Console Host (either shell) | *(same as above — PowerShell and cmd.exe both front the same `conhost.exe` unless run inside Windows Terminal)* | | |
| macOS Terminal.app | `Cmd +/-/0` | Cocoa `NSMenu` key-equivalents, resolved by AppKit before any key event reaches the pty | No — only the user's persistent Keyboard Shortcuts or Terminal preferences |
| iTerm2 | `Cmd +/-/0`, `Ctrl+scroll` | iTerm2's own menu key-equivalents / font-size actions | No — iTerm2's Python/scripting API manages tabs, panes, and text, not the app's own keyboard shortcut table; only its persistent Preferences → Keys |
| GNOME Terminal | `Ctrl +/-/0` | GTK accelerator actions (`zoom-in`/`zoom-out`/`zoom-normal`) wired at the `GtkApplication` level, consumed before the VTE terminal widget sees the keypress | No — only a persistent `gsettings`/dconf keybinding override |
| Konsole | `Ctrl +/-/0`, `Ctrl+scroll` | Konsole's own Qt shortcut system (`SessionController`), before VT100 processing | No — only Konsole's persistent "Configure Shortcuts" |
| Alacritty | Configured `IncreaseFontSize`/`DecreaseFontSize`/`ResetFontSize` bindings | Alacritty's own `winit` event loop, before any PTY write | No — only editing the user's persistent `alacritty.toml` |
| kitty | Configured font-size key mappings | kitty's own event loop | Partial and declined — kitty's remote-control protocol (`kitty @`) can *change* the current font size after the fact, but only if the user has already opted into `allow_remote_control` in their own `kitty.conf` (a persistent setting Velune won't set), and even then it can only *re-snap* the size after each zoom keypress fires — a guess-and-correct race, not a lock. Doesn't meet the reliability bar (see "Do not attempt unreliable hacks" in the requirements), so it isn't implemented |
| VS Code integrated terminal | `Ctrl/Cmd +/-/0` | VS Code's own "Terminal: Increase/Decrease/Reset Font Size" commands, resolved by VS Code's keybinding service before its `xterm.js` terminal (and long before the PTY) sees the keystroke | No, not from the CLI process. A VS Code *extension* could contribute a `keybindings` override in its `package.json`, but that's a separate, persistent, opt-in IDE artifact the user would have to install — not something `velune` itself can install or guarantee reverts on exit |

Every row reaches the same structural dead end: the shortcut is consumed
above the PTY, and the only override mechanism anywhere in this list is a
persistent, global config file or system setting.

## What Velune does instead

`velune doctor` includes a **Terminal** check (`velune/cli/terminal_env.py`,
wired into `velune/cli/commands/doctor.py`) that:

1. Detects the hosting terminal from environment variables (`WT_SESSION`,
   `TERM_PROGRAM`, `KONSOLE_VERSION`, `GNOME_TERMINAL_SCREEN`,
   `KITTY_WINDOW_ID`, `ALACRITTY_SOCKET`, `PSModulePath` to distinguish
   PowerShell from `cmd.exe` under the legacy console host, etc.).
2. Reports `zoom_lock_supported: False` for every detected terminal, with a
   terminal-specific explanation drawn from the table above.
3. Surfaces this as a `warn` (never a `fail`) — this isn't a problem with
   the user's environment to fix, just a documented limitation worth being
   upfront about instead of silently no-oping.

If a future terminal ships a genuine session-scoped API for this,
`detect_terminal()` is the single place to add it —
`TerminalInfo.zoom_lock_supported` already exists as a per-terminal field for
exactly that case, and nothing else in the codebase currently reads or acts
on it, so there is no dead-toggle risk from adding it now for something that
doesn't exist yet.
