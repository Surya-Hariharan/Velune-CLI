# Velune user-data inventory and `velune purge-data`

`velune uninstall` removes only the program and never touches any data below.
`velune purge-data` is the only command that deletes data, and only the categories
you name. Ownership is decided by **exact names inside validated roots**, never by a
directory name alone.

## Roots (how locations are resolved)

| Root | Resolution |
| --- | --- |
| home | `~/.velune` (`Path.home() / ".velune"`) |
| config | `platformdirs.user_config_dir("Velune")` |
| data | `velune.core.paths.app_data_root()` (`VELUNE_DATA_HOME`, else `%LOCALAPPDATA%\Velune`, `~/Library/Application Support/Velune`, or `$XDG_DATA_HOME/velune`) |

## Categories

| Category | Exact entries | Sensitive / user-created | Reversible | Preserved unless named |
| --- | --- | --- | --- | --- |
| `cache` | home: `model_registry_cache.json` | no | yes (rebuilt) | yes |
| `logs` | home: `logs/`, `crash_reports/` | may hold local paths | no | yes |
| `state` | home: `repl_history`, `palette_favorites.json`, `onboarding_progress.json`, `active_model.json` | prompt history | no | yes |
| `config` | home: `velune.toml`, `workspaces.json`, `council_roles.json`, `hooks.json`, `local_models.json` | user settings | no | yes |
| `sessions` | home: `sessions/` | **user content** | no | **protected** |
| `skills` | home: `commands/`, `plugins/` | **user-created** | no | **protected** |
| `credentials` | config: `credentials.json`, `credentials.json.bak`, `master.passphrase` | **secret** (never printed) | no | **protected** |
| `memory` | data: `workspaces/<name>-<10 hex>/` → cognitive DB (+`-wal`/`-shm`), `qdrant_local_store/`, `lancedb_semantic_store/`, `.migrated_from_workspace` | **user content** | no | **protected** |

Nothing is selected by default. "Protected" means the category is kept even if you
choose others; there is no "all" option.

## Never in any category

The program and its uv environment; the lifecycle journal (`<data>/lifecycle/`);
project files, including a workspace's own `.velune/` folder; the uv cache; other uv
tools; daemon socket/PID files (live runtime state); the OS keyring entry (it holds a
key, not credentials); anything not listed above. These are not candidates, so no
option can select them.

## Safeguards

- Roots are rejected if they are a symlink/junction, a filesystem root, the home
  directory (or contain it), a repository root, the current directory (or contain it),
  or resolve to a different path than given.
- Targets are walked without following links. A symlink, junction/reparse point, special
  file, alias, or unreadable entry makes the whole target an anomaly: reported, skipped,
  and any `--execute` that includes it is refused.
- Deletion uses `unlink` per file and `rmdir` per directory from the validated plan —
  never `rmtree`, never a shell. Each entry is re-checked (type, size, mtime) just
  before deletion; any difference, or any error, stops the run. Nothing is retried or
  widened. A directory that gained content after the preview cannot be removed.
- Immediately before **every** deletion the whole path is validated again: the approved
  root (a real directory with the identity recorded at preview), every directory between
  the root and the item (not a symlink, junction or reparse point; same identity as
  scanned), and the item itself (same identity, type, size and mtime). A path that
  escapes the root, contains `..`, or only shares a string prefix with it is refused.
  If the root's identity cannot be established the run fails closed.
- The preview and the deletion use the same plan object.
- Output lists paths and counts only, never file contents.

## Confirmation

`velune purge-data -c <category> ...` previews. Deleting requires `--execute` and the
exact phrase `delete my velune data` (typed at the prompt, or `--confirm-phrase` when
there is no terminal). An empty answer or a plain "yes" cancels.

**The phrase is public and the flag is scriptable.** `--confirm-phrase "delete my velune
data"` lets a script, a CI job or an AI agent with shell access delete the selected
categories, including `credentials`, `memory`, `sessions` and `skills`, with no human
involved. That is a deliberate escape hatch for terminal-less use and is not the same as an
interactive confirmation: it only prevents an accidental run. Treat it as you would
`rm`: do not wire it into unattended automation, and require a person to approve any agent
that wants to run it. What stays in force either way: categories must be named
explicitly (`--execute` with no `-c` is a read-only inventory), and the root, symlink,
junction and identity checks above are applied to scripted runs unchanged.
A run that stops before deleting anything exits 1; one that stops after deleting
something is a partial result and exits 9 (see `docs/lifecycle.md`).

## Known limits

Not atomic; a partial result is reported. The re-validation narrows the race with a
same-user attacker who can write inside Velune's own folders but is **not race-free**:
a directory could still be swapped in the instant between the check and the `unlink`.
Fd-relative deletion (`dir_fd`) would close that gap on Linux/macOS; it is not implemented
or verified. A running Velune may hold files open
(especially on Windows), which makes those deletions fail and stop the run. Journal
files are not used. Closing Velune and its daemon first is recommended.
