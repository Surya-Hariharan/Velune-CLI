# Velune lifecycle commands: contract, behaviour and limits

Commands: `velune update [--check]`, `velune repair`, `velune uninstall`,
`velune purge-data`. This page states what they do, what they refuse, and what has
and has not been verified. **It is not a claim of production readiness**: see
"Verification status".

## Supported installation

Only a **uv tool** installation (`uv tool install velune-cli`) can be changed by
`update`, `repair` or `uninstall`. Ownership is proven from several independent signals
(the tool directory, `uv-receipt.toml`, package metadata, editable marker); the
`INSTALLER=uv` file alone is never enough. Anything else is refused *before* any change:

| Ownership | `update --check` | `update`, `repair`, `uninstall` |
| --- | --- | --- |
| `uv_tool` (confirmed) | works | allowed (subject to the rules below) |
| `not_uv` (pip, pipx, ...) | works (no uv command offered) | refused |
| `editable` (development checkout) | works | refused |
| `ambiguous`, `indeterminate` | works | refused |

## How `uv` is found

`update`, `repair`, `uninstall` and the ownership check behind `update --check` locate uv
with Velune's own PATH walk (`velune/lifecycle/uvpath.py`), never `shutil.which`. A `uv`
in the current directory, a relative or empty PATH entry, an entry containing `..`, or one
whose real path is inside the current directory is ignored. Directories are also compared
by filesystem identity, so alias spellings of the current directory (extended-length
`\\?\` paths, UNC or admin-share paths, short names, links) are refused as well; a link to
a safe location is still accepted, and if identity cannot be established the entry is
refused. A directory whose identity cannot be trusted (the current directory is gone or
unreadable, or the filesystem reports no usable file identity) is treated as unknown and
refused as well. On Windows only `uv.exe` is
accepted (a `uv.bat` or `uv.cmd` would run through `cmd.exe`). If no trusted uv exists the
command refuses before changing anything and prints the manual command. The rule is the
same on every Python version.

**A legitimate uv can be refused.** Any uv at or below the current directory is refused on
purpose, so a terminal opened in a directory that *contains* uv (for example your home
directory when uv lives in `.local\bin` below it) cannot run `update`, `repair` or
`uninstall`. This is a security restriction, not a broken uv. Run the command again from a
directory that does not contain uv. The refusal (from `update`, `repair`, `uninstall` and
the `update --check` guidance) names this reason instead of suggesting a reinstall; the
check itself still works but cannot confirm uv ownership. If the current directory has been deleted, change to an existing one first.

## Commands

- **`update --check [--pre]`**: read-only; asks PyPI's Simple API for release metadata
  (no package files are downloaded). Exit 10 if an update exists.
- **`update`**: runs `uv tool upgrade velune-cli`. Refused for pinned installs,
  local-file installs, and `--pre`; those get the exact manual command.
- **`repair`**: runs `uv tool install --force "velune-cli[extras]==<installed version>"`.
  Never changes the version; keeps extras; refuses local-file/URL/VCS sources, version
  ranges, unreadable versions or extras. An unpinned install becomes pinned to its
  current version (stated in the output); the index is whatever your uv config says.
  Verifies ownership, version, extras, the package `RECORD` and the launcher file;
  **does not run the repaired CLI**.
- **`uninstall`**: runs `uv tool uninstall velune-cli` after an explicit confirmation
  (`--yes` skips only the question). Removes the program only.
- **`purge-data`**: the only command that deletes data. Preview by default;
  `--execute` plus a typed phrase deletes only the categories named with `-c`.
  See [data-inventory.md](data-inventory.md).

## What is preserved

`update`, `repair` and `uninstall` never read or write configuration, credentials,
memory, sessions, skills, project files, the uv cache or other uv tools. `uninstall`
contains no deletion code of its own: uv does the removal and Velune only observes
the result. Data is removed solely by `purge-data`, never implicitly.

## Windows: manual only

Windows locks the files of the running installation (observed in the P2.0 spike:
half-replaced or destroyed environments). So on Windows `update`, `repair` and
`uninstall` **refuse to change the installation Velune is running from** and print the
exact command to run after closing Velune, in a fresh terminal. There is no detached
helper, no self-deletion workaround, and no guard bypass (`--yes` does not bypass it).

## POSIX

No equivalent refusal is applied on Linux/macOS because no evidence justifies one;
this is an open question, not a guarantee that in-place replacement is safe. The
only real-uv Linux evidence is the P2.3.1 spike (WSL2, a synthetic package, an
interrupted `uv tool upgrade`); Velune's own `update` has not been run against a real
uv on POSIX. `repair` and `uninstall` were exercised against a real uv (0.13.7) only in
a disposable, isolated tool environment on Windows x64 during the audit (not part of the
test suite); they have not been run against a real uv on Linux or macOS.

## Journal and recovery

Before running uv, a mutating command writes a journal
(`<data dir>/lifecycle/journal.json`; phases `planned` -> `applying`). It records the
operation, versions, extras and pin; never secrets or user paths. Rules:

- An existing journal is never overwritten; a new mutation is refused until a human
  has looked at it.
- **Concurrent lifecycle commands are not supported, and are guarded.** The journal is
  created with one exclusive filesystem operation (a hard link, or `O_CREAT | O_EXCL` where
  hard links do not exist), not a check followed by a write. If two commands start at the
  same moment exactly one gets the journal; the other stops with exit 8 (`busy`) before
  running uv and changes nothing. There is no lock file, so there is no stale lock to
  clear: a crash leaves the journal for a person to look at. Each journal carries a random
  token, so two operations that start in the same second cannot take over each other's
  journal. On a filesystem without hard links a crash can leave a partial journal; it reads
  as malformed and blocks further commands (safe, but a person has to clear it).
- uv's exit code is never trusted. After uv runs, the installation is re-inspected.
- The journal is cleared only when the state is **known**: success verified, or
  verified unchanged/intact. Otherwise it is kept and manual recovery is printed.
- No automatic retry, rollback, reinstall or hand-deletion after an uncertain outcome.
- Nothing is atomic: uv replaces files in place.

To clear a retained journal: check that no Velune/uv process runs, that
`uv tool list` shows what you expect and `velune --version` runs (or, after an
uninstall, that `velune-cli` is gone), then remove the journal file yourself.

## Exit codes (shared)

The same table applies to `update`, `repair`, `uninstall` and `purge-data`. The second
column is the JSON `status`.

| Code | Status | Meaning | Was anything changed? |
| --- | --- | --- | --- |
| 0 | ok | Success, verified. Also: up to date, preview, nothing to delete. | as reported |
| 1 | error | The command failed and the installation or data is **verified unchanged or intact** (uv could not start, uv failed, the update did not take effect, a purge stopped before deleting anything). Also an unexpected error before any change started. | no |
| 2 | usage | Bad usage (unknown category, invalid flags). | no |
| 3 | refused | Refused before any change: ownership, Windows guard, pin/source, journal exists, no confirmation, or uv missing or not in a trusted location. | no |
| 4 | network | Network or index failure. | no |
| 8 | busy | Another lifecycle command holds the journal. | no |
| 9 | uncertain | A change was started and the result could not be verified, or is partial (uv ran and the files or packages do not check out; an unexpected error after uv started; a purge that stopped after deleting something). **The journal is kept** and manual recovery is printed. | possibly |
| 10 | update_available | `update --check`: an update is available. | no |
| 130 | cancelled | Cancelled or declined, with the state verified. A cancelled run whose state could not be verified is 9, not 130. | no |

Codes 5, 6 and 7 are reserved by the original design for validation and rollback outcomes;
Velune performs no rollback and never returns them.

**Reading it in a script:** 3, 4 and 8 mean nothing was touched and the command can be run
again once the cause is fixed. 1 means it failed but the state was checked and is
unchanged. **9 means stop**: do not retry automatically, read the recovery text, and clear
the journal only after checking the installation. The JSON `result.outcome` gives the
detail (for example `failed_intact`, `uncertain`), and `result.uv_ran` (uninstall) says
whether uv was actually started: `false` means the process could not be started.

JSON output (`--json`) uses one shape for all commands: `command`, `status`,
`exit_code`, `message`, `installation`, `plan`, `result`, `warnings`, and optionally
`recovery`. Paths and secrets are scrubbed in text and JSON alike.

## Scripted and non-interactive use

`uninstall --yes` and `purge-data --execute --confirm-phrase "delete my velune data"` run
without a person at a prompt. The phrase is public and fixed (it is printed in this
document and in the command's help), so supplying it proves nothing about who is asking: it
is **not equivalent to interactive human confirmation**, only a guard against typing the
wrong command. Any script, scheduler, CI job or AI agent that can run shell commands can
therefore delete credentials, memory, sessions or skills if it is told to or tricked into
it, and a `--yes` uninstall can remove the program the same way. Do not put these flags in
unattended automation, and do not allow an agent to run them without a review step by a
person. Category selection is always explicit (nothing is selected by default, and
`--execute` without `-c` stays a read-only inventory), and every path, identity and
symlink check still applies; scripted use removes none of them.

## Verification status

Automated tests use fakes for uv and synthetic directories; no real
update/repair/uninstall/purge is run by the test suite. CI runs the whole suite on
Linux, Windows and macOS (Python 3.10-3.14), but those results are not part of this
document. Real-uv validation of `repair`, `uninstall` and the POSIX behaviour of all
three remains outstanding.

## Known limitations

- Files are verified against the package's own `RECORD`; dependencies are not.
- The `velune` launcher check covers the environment's launcher and `PATH` resolution only.
- Purge is not atomic and cannot delete files another process holds open.
- Cancellation (Ctrl-C) waits for uv to finish; a second Ctrl-C or a timeout stops it
  and the result is then inspected, not assumed.
