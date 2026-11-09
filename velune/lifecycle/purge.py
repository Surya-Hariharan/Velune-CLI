"""``velune purge-data``: delete *selected categories* of Velune's own user data.

Separate from ``velune uninstall`` on purpose: removing the program never removes
data, and removing data never touches the program. See ``docs/data-inventory.md``
for the full inventory and the reasons behind each rule below.

Ownership is **never inferred from a directory name**. Velune data lives in three
roots (``~/.velune``, the platform config directory, the app data directory). Only
entries that this module lists *by exact name* inside a validated root are ever
candidates; the roots themselves, the lifecycle journal, project folders
(including a workspace's own ``.velune``), the uv cache and every other file are
not in the inventory, so nothing can select them. Per-workspace stores under
``<data>/workspaces/`` are the one dynamic part: a folder counts only if its name
has the exact ``<name>-<10 hex>`` shape Velune writes *and* it contains a known
store file, and then only those known entries are candidates.

Flow (one validated plan, used by both the preview and the deletion):

1. ``build_plan`` validates the roots, walks each selected target **without
   following links**, and records every file/directory with a fingerprint. Any
   symlink, junction/reparse point, unexpected path alias, or unreadable entry
   makes that whole target an anomaly: it is reported and never deleted.
2. The preview prints that plan. It changes nothing.
3. ``execute_plan`` deletes exactly the recorded entries (``unlink`` for files,
   ``rmdir`` for directories, deepest first; never a recursive delete and never a
   shell). Before each deletion the entry is re-checked against its fingerprint.
   If anything differs, or any deletion fails, it **stops** and reports; it does
   not continue, retry, or widen the scope.

Not atomic. A failed or stopped purge leaves a partial result which is reported
honestly. Credentials and memory are never reconstructed. The OS keyring entry
that may protect the credential store is not touched (it holds only a key).
The lifecycle journal is deliberately not used: it records uv operations on the
installation, and nothing here needs recovery state beyond the report.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velune.core.paths import COGNITIVE_DB_NAME, LANCEDB_STORE_NAME, QDRANT_STORE_NAME
from velune.lifecycle.errors import ExitCode, LifecycleError, LifecycleResult

COMMAND = "purge-data"
CONFIRM_PHRASE = "delete my velune data"
MAX_ENTRIES = 200_000
_REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (symlinks, junctions, mount points)
_SLUG = re.compile(r"^[A-Za-z0-9._-]+-[0-9a-f]{10}$")
_MIGRATION_MARKER = ".migrated_from_workspace"

HOME, CONFIG, DATA = "home", "config", "data"


@dataclass(frozen=True)
class Category:
    name: str
    root: str  # HOME | CONFIG | DATA
    entries: tuple[str, ...]  # exact names under the root (or per-workspace for "memory")
    protected: bool  # user-created or sensitive: preserved unless named explicitly
    reversible: bool
    summary: str


CATEGORIES: tuple[Category, ...] = (
    Category(
        "cache",
        HOME,
        ("model_registry_cache.json",),
        False,
        True,
        "Cached model registry; rebuilt automatically.",
    ),
    Category(
        "logs",
        HOME,
        ("logs", "crash_reports"),
        False,
        False,
        "Diagnostic logs and crash reports (may contain local paths).",
    ),
    Category(
        "state",
        HOME,
        ("repl_history", "palette_favorites.json", "onboarding_progress.json", "active_model.json"),
        False,
        False,
        "Prompt history, palette favourites, onboarding progress, last model choice.",
    ),
    Category(
        "config",
        HOME,
        ("velune.toml", "workspaces.json", "council_roles.json", "hooks.json", "local_models.json"),
        False,
        False,
        "User-level settings, workspace registry, council roles, hooks, local models.",
    ),
    Category(
        "sessions", HOME, ("sessions",), True, False, "Saved conversation sessions (user content)."
    ),
    Category(
        "skills",
        HOME,
        ("commands", "plugins"),
        True,
        False,
        "User-created custom commands and user-installed plugins/skills.",
    ),
    Category(
        "credentials",
        CONFIG,
        ("credentials.json", "credentials.json.bak", "master.passphrase"),
        True,
        False,
        "Encrypted API-key store and its passphrase file (secret; contents never shown).",
    ),
    Category(
        "memory",
        DATA,
        (
            COGNITIVE_DB_NAME,
            f"{COGNITIVE_DB_NAME}-wal",
            f"{COGNITIVE_DB_NAME}-shm",
            QDRANT_STORE_NAME,
            LANCEDB_STORE_NAME,
            _MIGRATION_MARKER,
        ),
        True,
        False,
        "Per-workspace memory databases and vector stores (user content).",
    ),
)
BY_NAME = {c.name: c for c in CATEGORIES}


@dataclass(frozen=True)
class Roots:
    home_dir: Path  # ~/.velune
    config_dir: Path  # platform user config dir for Velune
    data_dir: Path  # app data root

    def get(self, which: str) -> Path:
        return {HOME: self.home_dir, CONFIG: self.config_dir, DATA: self.data_dir}[which]


def default_roots() -> Roots:
    from platformdirs import user_config_dir

    from velune.core.paths import app_data_root

    return Roots(Path.home() / ".velune", Path(user_config_dir("Velune")), app_data_root())


@dataclass(frozen=True)
class Item:
    path: Path
    kind: str  # "file" | "dir"
    size: int
    mtime_ns: int
    ident: tuple[int, int] | None = None  # (st_dev, st_ino); None where the OS gives none


@dataclass
class Target:
    category: str
    path: Path
    items: list[Item] = field(default_factory=list)  # deletion order: files, deepest dirs last
    anomaly: str | None = None
    root_label: str = ""


@dataclass
class Plan:
    selected: tuple[str, ...]
    targets: list[Target] = field(default_factory=list)
    root_problems: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)  # "<category>: <entry>" not present
    workspace_dirs: list[Path] = field(default_factory=list)  # emptied-slug dirs to rmdir
    roots: dict[str, RootInfo] = field(default_factory=dict)  # validated roots + identity
    workspace_root: str = ""

    @property
    def anomalies(self) -> list[Target]:
        return [t for t in self.targets if t.anomaly]

    @property
    def deletable(self) -> list[Target]:
        return [t for t in self.targets if not t.anomaly]


@dataclass(frozen=True)
class RootInfo:
    path: Path
    ident: tuple[int, int] | None


def _ident(st: os.stat_result) -> tuple[int, int] | None:
    """Filesystem identity of an lstat result; None when the OS reports none (inode 0)."""
    return (st.st_dev, st.st_ino) if st.st_ino else None


def _link_like(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def _inside(root: Path, candidate: Path) -> bool:
    """True if *candidate* is strictly below *root* (component-wise, not a string prefix)."""
    try:
        return candidate != root and os.path.commonpath([str(root), str(candidate)]) == str(root)
    except ValueError:  # different drives
        return False


def validate_root(label: str, root: Path) -> tuple[Path | None, str | None]:
    """Return ``(resolved root, None)`` or ``(None, reason)``. A missing root is fine."""
    try:
        if not os.path.lexists(root):
            return root.absolute(), None
        st = os.lstat(root)
        if _link_like(st):
            return None, f"the {label} directory is a symlink or junction"
        if not stat.S_ISDIR(st.st_mode):
            return None, f"the {label} path is not a directory"
        resolved = root.resolve(strict=True)
        if os.path.normcase(str(resolved)) != os.path.normcase(str(root.absolute())):
            return None, f"the {label} directory resolves to a different path (alias)"
        if resolved.parent == resolved:
            return None, f"the {label} directory is a filesystem root"
        home = Path.home().resolve()
        if resolved == home or _inside(resolved, home):
            return None, f"the {label} directory is the home directory or contains it"
        if os.path.lexists(resolved / ".git"):
            return None, f"the {label} directory looks like a repository root"
        if resolved == Path.cwd().resolve() or _inside(resolved, Path.cwd().resolve()):
            return None, f"the {label} directory is, or contains, the current directory"
        return resolved, None
    except (OSError, RuntimeError, ValueError):
        return None, f"the {label} directory could not be validated"


def _scan(path: Path, root: Path) -> Target:
    """Record *path* (a file or directory) and everything below it, never following links."""
    target = Target("", path)
    try:
        st = os.lstat(path)
    except OSError:
        target.anomaly = "could not be read"
        return target
    if _link_like(st):
        target.anomaly = "is a symlink or junction"
        return target
    if not _inside(root, path.resolve(strict=True)) or os.path.normcase(
        str(path.resolve(strict=True))
    ) != os.path.normcase(str(path.absolute())):
        target.anomaly = "resolves outside its expected location"
        return target
    if stat.S_ISREG(st.st_mode):
        target.items.append(Item(path, "file", st.st_size, st.st_mtime_ns, _ident(st)))
        return target
    if not stat.S_ISDIR(st.st_mode):
        target.anomaly = "is not a regular file or directory"
        return target
    dirs: list[Item] = [Item(path, "dir", 0, 0, _ident(st))]
    files: list[Item] = []
    stack = [path]
    try:
        while stack:
            current = stack.pop()
            with os.scandir(current) as it:
                for entry in it:
                    child = Path(entry.path)
                    # lstat, not DirEntry.stat(): on Windows the latter reports no inode.
                    cst = os.lstat(entry.path)
                    if _link_like(cst):
                        target.anomaly = "contains a symlink or junction"
                        return target
                    if stat.S_ISDIR(cst.st_mode):
                        dirs.append(Item(child, "dir", 0, 0, _ident(cst)))
                        stack.append(child)
                    elif stat.S_ISREG(cst.st_mode):
                        files.append(Item(child, "file", cst.st_size, cst.st_mtime_ns, _ident(cst)))
                    else:
                        target.anomaly = "contains a special file"
                        return target
                    if len(files) + len(dirs) > MAX_ENTRIES:
                        target.anomaly = "is too large to validate"
                        return target
    except OSError:
        target.anomaly = "could not be read completely"
        return target
    dirs.sort(key=lambda i: len(i.path.parts), reverse=True)  # deepest first
    target.items = files + dirs
    return target


def _root_ident(root: Path) -> tuple[int, int] | None:
    try:
        return _ident(os.lstat(root))
    except OSError:
        return None


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def build_plan(selected: tuple[str, ...], roots: Roots | None = None) -> Plan:
    """Validate and inventory the selected categories. Read-only."""
    for name in selected:
        if name not in BY_NAME:
            raise LifecycleError(
                ExitCode.USAGE,
                f"Unknown category '{name}'. Choose from: {', '.join(BY_NAME)}.",
                details={"reason": "unknown_category"},
            )
    roots = roots or default_roots()
    plan = Plan(selected=tuple(dict.fromkeys(selected)))
    resolved: dict[str, Path] = {}
    for label in (HOME, CONFIG, DATA):
        if not any(BY_NAME[n].root == label for n in plan.selected):
            continue
        ok, problem = validate_root(label, roots.get(label))
        if ok is None:
            plan.root_problems[label] = problem or "invalid"
        else:
            resolved[label] = ok
            plan.roots[label] = RootInfo(ok, _root_ident(ok))

    for name in plan.selected:
        cat = BY_NAME[name]
        root = resolved.get(cat.root)
        if root is None:
            continue
        if name == "memory":
            _plan_memory(plan, cat, root)
            continue
        for entry in cat.entries:
            path = root / entry
            if not _exists(path):
                plan.missing.append(f"{name}: {entry}")
                continue
            target = _scan(path, root)
            target.category = name
            target.root_label = cat.root
            plan.targets.append(target)
    return plan


def _plan_memory(plan: Plan, cat: Category, root: Path) -> None:
    base = root / "workspaces"
    if not _exists(base):
        plan.missing.append("memory: workspaces")
        return
    try:
        bst = os.lstat(base)
        if _link_like(bst) or not stat.S_ISDIR(bst.st_mode):
            plan.targets.append(Target("memory", base, anomaly="is a symlink or not a directory"))
            return
        slugs = sorted(p for p in base.iterdir() if _SLUG.fullmatch(p.name))
    except OSError:
        plan.targets.append(Target("memory", base, anomaly="could not be read"))
        return
    for slug in slugs:
        try:
            sst = os.lstat(slug)
        except OSError:
            continue
        if _link_like(sst):
            plan.targets.append(Target("memory", slug, anomaly="is a symlink or junction"))
            continue
        if not stat.S_ISDIR(sst.st_mode):
            continue
        found = 0
        for entry in cat.entries:
            path = slug / entry
            if not _exists(path):
                continue
            found += 1
            target = _scan(path, root)
            target.category = "memory"
            target.root_label = cat.root
            plan.targets.append(target)
        if found:
            plan.workspace_dirs.append(slug)
            plan.workspace_root = cat.root


def summarize(plan: Plan) -> list[dict[str, Any]]:
    """One line per target for display. Paths only: no contents, ever."""
    rows = []
    for t in plan.targets:
        files = sum(1 for i in t.items if i.kind == "file")
        rows.append(
            {
                "category": t.category,
                "path": str(t.path),
                "files": files,
                "bytes": sum(i.size for i in t.items),
                "action": "skip" if t.anomaly else "delete",
                "note": t.anomaly,
            }
        )
    return rows


@dataclass
class Report:
    deleted: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    stopped: str | None = None
    not_attempted: list[str] = field(default_factory=list)
    removed: int = 0  # individual files/directories actually removed (not whole targets)


def _unchanged(item: Item) -> bool:
    """The leaf still is what the preview recorded (type, identity and, for files, size/mtime)."""
    try:
        st = os.lstat(item.path)
    except OSError:
        return False
    if _link_like(st):
        return False
    if item.ident is not None and _ident(st) != item.ident:
        return False  # a different file or directory now sits at this path
    if item.kind == "dir":
        return stat.S_ISDIR(st.st_mode)
    return stat.S_ISREG(st.st_mode) and st.st_size == item.size and st.st_mtime_ns == item.mtime_ns


def _chain_ok(
    info: RootInfo | None,
    path: Path,
    known: dict[Path, tuple[int, int] | None] | None = None,
) -> bool:
    """Every directory from the approved root down to *path*'s parent is still the real one.

    Re-checked immediately before each deletion, so a directory swapped for a
    junction/symlink after the preview (or between two deletions) is noticed even
    though the leaf itself looks unchanged. Components are taken relative to the
    root, never compared as string prefixes. Directories scanned in the plan must
    also keep their recorded identity. Fails closed when the root has no recorded
    identity to compare against.
    """
    if info is None or info.ident is None:
        return False
    try:
        rel = path.relative_to(info.path).parts
    except ValueError:
        return False
    if not rel or any(part in ("", ".", "..") or os.sep in part for part in rel):
        return False
    current = info.path
    try:
        st = os.lstat(current)
        if _link_like(st) or not stat.S_ISDIR(st.st_mode) or _ident(st) != info.ident:
            return False
        for part in rel[:-1]:
            current = current / part
            st = os.lstat(current)
            if _link_like(st) or not stat.S_ISDIR(st.st_mode):
                return False
            want = (known or {}).get(current)
            if want is not None and _ident(st) != want:
                return False
    except OSError:
        return False
    return True


def execute_plan(
    plan: Plan,
    remove_file: Callable[[Path], None] = os.unlink,
    remove_dir: Callable[[Path], None] = os.rmdir,
) -> Report:
    """Delete exactly what the plan recorded; stop at the first surprise.

    Immediately before every single deletion the whole path is re-validated: the root
    (a directory, not a link, same identity), each directory below it (a real directory,
    not a symlink/junction/reparse point, same identity as scanned) and the leaf. This
    narrows the race; it is **not** race-free protection. Between that check and the
    ``unlink`` call, a same-user attacker who can write inside Velune's own folders could
    still swap a directory. Only ``dir_fd``-based deletion could close that gap, and this
    implementation does not claim to.
    """
    report = Report()
    targets = plan.deletable
    known: dict[Path, tuple[int, int] | None] = {}
    for target in targets:
        for item in target.items:
            if item.kind == "dir":
                known[item.path] = item.ident
    for index, target in enumerate(targets):
        info = plan.roots.get(target.root_label)
        for item in target.items:
            if not (_chain_ok(info, item.path, known) and _unchanged(item)):
                report.stopped = "an item changed after the preview, so deletion stopped"
                report.failed.append((target.category, "changed_since_preview"))
                _rest(report, targets, index, target)
                return report
            try:
                (remove_file if item.kind == "file" else remove_dir)(item.path)
                report.removed += 1
            except OSError as exc:
                report.stopped = "a deletion failed, so deletion stopped"
                report.failed.append((target.category, type(exc).__name__))
                _rest(report, targets, index, target)
                return report
        report.deleted.append(str(target.path))
    info = plan.roots.get(plan.workspace_root)
    for slug in plan.workspace_dirs:  # only if now empty; rmdir cannot remove content
        if not _chain_ok(info, slug, known):
            continue  # fail closed: a changed path is simply left alone
        try:
            os.rmdir(slug)
        except OSError:
            pass
    return report


def _rest(report: Report, targets: list[Target], index: int, current: Target) -> None:
    report.not_attempted.append(str(current.path))
    report.not_attempted += [str(t.path) for t in targets[index + 1 :]]


# ---- command-level orchestration ------------------------------------------------


def _preserved(selected: tuple[str, ...]) -> list[str]:
    return [c.name for c in CATEGORIES if c.name not in selected]


def run_purge(
    selected: tuple[str, ...] = (),
    *,
    execute: bool = False,
    confirm: Callable[[str], str | None] | None = None,
    confirm_phrase: str | None = None,
    roots: Roots | None = None,
    remove_file: Callable[[Path], None] = os.unlink,
    remove_dir: Callable[[Path], None] = os.rmdir,
) -> LifecycleResult:
    """Preview by default; delete only with ``execute`` and the exact phrase. Never raises."""
    try:
        plan = build_plan(selected if selected else tuple(BY_NAME), roots)
        if not selected:  # inventory-only: nothing is selected, so nothing can be executed
            execute = False
        rows = summarize(plan)
        base: dict[str, Any] = {
            "selected": list(selected),
            "preserved_categories": _preserved(selected),
            "targets": rows,
            "missing": plan.missing,
            "root_problems": plan.root_problems,
            "anomalies": [r for r in rows if r["note"]],
        }
        warnings = tuple(f"{k}: {v}" for k, v in plan.root_problems.items())
        if not execute:
            return LifecycleResult(
                COMMAND,
                message=_preview_message(selected, rows),
                result=base | {"dry_run": True, "deleted": []},
                warnings=warnings,
            )
        if plan.root_problems or plan.anomalies:
            raise LifecycleError(
                ExitCode.REFUSED,
                "Data cleanup was refused because the preview found problems with the selected "
                "targets (see 'anomalies' and 'root_problems'). Nothing was deleted.",
                recovery="Resolve them yourself or select other categories. Velune never "
                "deletes through a symlink, junction or unrecognised path.",
                details=base | {"reason": "validation_failed"},
            )
        if not plan.deletable:
            return LifecycleResult(
                COMMAND,
                message="Nothing to delete for the selected categories.",
                result=base | {"dry_run": False, "deleted": []},
            )
        answer = confirm_phrase
        if answer is None and confirm is not None:
            answer = confirm(_preview_message(selected, rows))
        if answer is None:
            raise LifecycleError(
                ExitCode.REFUSED,
                "Data cleanup needs your explicit confirmation, and this session cannot ask for "
                "it. Nothing was deleted.",
                recovery=f'Run it in an interactive terminal, or pass --confirm-phrase "{CONFIRM_PHRASE}".',
                details=base | {"reason": "confirmation_unavailable"},
            )
        if answer.strip().lower() != CONFIRM_PHRASE:
            return LifecycleResult(
                COMMAND,
                exit_code=ExitCode.CANCELLED,
                message="Confirmation phrase did not match. Nothing was deleted.",
                result=base | {"dry_run": False, "deleted": []},
            )
        report = execute_plan(plan, remove_file, remove_dir)
        return _report_result(base, report, plan)
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        return LifecycleResult.from_exception(COMMAND, exc)


def _preview_message(selected: tuple[str, ...], rows: list[dict[str, Any]]) -> str:
    if not selected:
        head = "Velune data inventory (nothing is selected, so nothing would be deleted):"
    else:
        head = "Preview: these selected items would be deleted. Everything else is preserved."
    lines = [head]
    for r in rows:
        mark = "SKIP" if r["note"] else ("delete" if selected else "found ")
        extra = f" ({r['note']})" if r["note"] else f" ({r['files']} file(s), {r['bytes']} bytes)"
        lines.append(f"  [{mark}] {r['category']}: {r['path']}{extra}")
    if not rows:
        lines.append("  (nothing found)")
    lines.append(
        "Not touched by any category: the program, project files, the uv cache, other tools."
    )
    return "\n".join(lines)


def _report_result(base: dict[str, Any], report: Report, plan: Plan) -> LifecycleResult:
    data = base | {
        "dry_run": False,
        "deleted": report.deleted,
        "failed": [{"category": c, "error": e} for c, e in report.failed],
        "not_attempted": report.not_attempted,
        "stopped": report.stopped,
        "removed_items": report.removed,
    }
    if report.stopped:
        return LifecycleResult(
            COMMAND,
            # Something already deleted means a partial result (uncertain); nothing deleted
            # means the data is verified untouched (plain failure).
            exit_code=ExitCode.UNCERTAIN if report.removed else ExitCode.ERROR,
            message=(
                (
                    f"Data cleanup stopped part-way: {report.stopped}. {len(report.deleted)} "
                    f"target(s) were deleted, {len(report.not_attempted)} were not fully "
                    "processed. This is a partial result; nothing further was attempted."
                )
                if report.removed
                else f"Data cleanup stopped before deleting anything: {report.stopped}. "
                "Your data was not touched."
            ),
            recovery="Close Velune and anything using its data, then run the preview again to "
            "see what remains.",
            result=data,
        )
    return LifecycleResult(
        COMMAND,
        message=f"Deleted {len(report.deleted)} target(s) from the selected categories "
        "(not atomic; nothing else was touched).",
        result=data,
    )
