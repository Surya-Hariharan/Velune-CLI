"""`velune purge-data` (P2.6): explicit, validated, preview-first data cleanup.

Everything runs in temporary directories with synthetic files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from velune.core.paths import COGNITIVE_DB_NAME, LANCEDB_STORE_NAME, QDRANT_STORE_NAME
from velune.lifecycle import purge
from velune.lifecycle.errors import ExitCode

# Typer forces styled terminal output when GITHUB_ACTIONS is set, and Rich's NO_COLOR keeps bold
# escapes that can fall between "--" and the option name. Child processes get an environment
# without the forcing variables, and assertions read the text with escapes removed.
_FORCE_TERMINAL_VARS = ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


def _cli_env() -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k not in _FORCE_TERMINAL_VARS}
    return {**base, "NO_COLOR": "1", "COLUMNS": "200", "PYTHONIOENCODING": "utf-8"}


PHRASE = purge.CONFIRM_PHRASE
SLUG = "proj-0123456789"
SECRET = "sk-SUPERSECRETVALUE"


def write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def env(tmp_path):
    home, cfg, data = tmp_path / "home" / ".velune", tmp_path / "cfg", tmp_path / "data"
    outside = tmp_path / "outside"
    write(outside / "precious.txt", "do not delete")
    files = {
        home / "model_registry_cache.json": "{}",
        home / "logs" / "velune-2026.log": "log",
        home / "crash_reports" / "r.json": "{}",
        home / "repl_history": "prompt",
        home / "velune.toml": "m='x'",
        home / "sessions" / "s1.json": "chat",
        home / "commands" / "mine.md": "cmd",
        home / "plugins" / "p" / "plugin.json": "{}",
        cfg / "credentials.json": SECRET,
        cfg / "credentials.json.bak": SECRET,
        cfg / "master.passphrase": SECRET,
        data / "workspaces" / SLUG / COGNITIVE_DB_NAME: "db",
        data / "workspaces" / SLUG / f"{COGNITIVE_DB_NAME}-wal": "wal",
        data / "workspaces" / SLUG / QDRANT_STORE_NAME / "c" / "seg": "v",
        data / "workspaces" / SLUG / LANCEDB_STORE_NAME / "t.lance": "v",
        data / "workspaces" / SLUG / "unknown_other_file.txt": "keep",
        data / "lifecycle" / "journal.json": "{}",
        data / "workspaces" / "notaslug" / COGNITIVE_DB_NAME: "db",
        tmp_path / "project" / "main.py": "print(1)",
        tmp_path / "project" / ".velune" / "snapshots" / "a": "snap",
    }
    for p, t in files.items():
        write(p, t)
    roots = purge.Roots(home, cfg, data)
    return type(
        "Env",
        (),
        {
            "roots": roots,
            "tmp": tmp_path,
            "outside": outside,
            "home": home,
            "cfg": cfg,
            "data": data,
        },
    )()


def snap(base: Path) -> dict[str, str]:
    return {
        str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(base.rglob("*"))
        if p.is_file()
    }


def run(env, cats, **kw):
    return purge.run_purge(tuple(cats), roots=env.roots, **kw)


# ---- inventory / dry run ----------------------------------------------------------


def test_no_selection_is_a_read_only_inventory(env):
    before = snap(env.tmp)
    result = run(env, [], execute=True, confirm_phrase=PHRASE)  # execute is ignored
    assert result.exit_code is ExitCode.OK and result.result["dry_run"] is True
    assert result.result["selected"] == [] and snap(env.tmp) == before


def test_preview_lists_exact_paths_and_changes_nothing(env):
    before = snap(env.tmp)
    result = run(env, ["credentials", "cache"])
    text = result.render_text()
    assert result.result["dry_run"] is True and snap(env.tmp) == before
    assert "credentials.json" in text and "model_registry_cache.json" in text
    assert SECRET not in json.dumps(result.to_dict()) and SECRET not in text
    assert "memory" in result.result["preserved_categories"]


def test_unknown_category_is_a_usage_error(env):
    assert run(env, ["everything"]).exit_code is ExitCode.USAGE


def test_inventory_matches_documentation_categories():
    doc = (Path(__file__).parent.parent / "docs" / "data-inventory.md").read_text(encoding="utf-8")
    for cat in purge.CATEGORIES:
        assert f"`{cat.name}`" in doc


# ---- confirmation ------------------------------------------------------------------


def test_execute_without_a_way_to_confirm_refuses(env):
    before = snap(env.tmp)
    result = run(env, ["cache"], execute=True)
    assert result.exit_code is ExitCode.REFUSED
    assert result.result["reason"] == "confirmation_unavailable" and snap(env.tmp) == before


@pytest.mark.parametrize("answer", ["", "yes", "y", "delete", "DELETE VELUNE", "delete my data"])
def test_generic_or_wrong_answers_do_not_confirm(env, answer):
    before = snap(env.tmp)
    result = run(env, ["credentials"], execute=True, confirm=lambda text: answer)
    assert result.exit_code is ExitCode.CANCELLED and snap(env.tmp) == before


def test_wrong_noninteractive_phrase_is_cancelled(env):
    before = snap(env.tmp)
    assert run(env, ["cache"], execute=True, confirm_phrase="yes").exit_code is ExitCode.CANCELLED
    assert snap(env.tmp) == before


def test_phrase_without_execute_still_only_previews(env):
    before = snap(env.tmp)
    result = run(env, ["cache"], confirm_phrase=PHRASE)
    assert result.result["dry_run"] is True and snap(env.tmp) == before


def test_prompt_receives_the_preview(env):
    seen = []
    run(env, ["cache"], execute=True, confirm=lambda t: seen.append(t) or "")
    assert "model_registry_cache.json" in seen[0]


# ---- deletion of selected categories only -------------------------------------------


def test_selected_category_only_is_deleted(env):
    result = run(env, ["cache"], execute=True, confirm_phrase=PHRASE.upper())
    assert result.exit_code is ExitCode.OK
    assert not (env.home / "model_registry_cache.json").exists()
    for kept in (
        env.home / "sessions" / "s1.json",
        env.home / "commands" / "mine.md",
        env.home / "velune.toml",
        env.home / "logs" / "velune-2026.log",
        env.cfg / "credentials.json",
        env.data / "workspaces" / SLUG / COGNITIVE_DB_NAME,
    ):
        assert kept.exists(), kept


@pytest.mark.parametrize("cat", ["credentials", "memory", "skills", "sessions"])
def test_protected_categories_are_preserved_unless_named(env, cat):
    run(env, ["cache", "logs", "state", "config"], execute=True, confirm_phrase=PHRASE)
    after = purge.build_plan((cat,), env.roots)
    assert after.deletable, f"{cat} should still exist"


def test_credentials_deleted_when_named_and_nothing_else(env):
    run(env, ["credentials"], execute=True, confirm_phrase=PHRASE)
    assert not (env.cfg / "credentials.json").exists()
    assert not (env.cfg / "master.passphrase").exists()
    assert (env.home / "sessions" / "s1.json").exists()
    assert (env.data / "workspaces" / SLUG / COGNITIVE_DB_NAME).exists()


def test_memory_deletes_only_known_store_entries(env):
    run(env, ["memory"], execute=True, confirm_phrase=PHRASE)
    slug = env.data / "workspaces" / SLUG
    assert not (slug / COGNITIVE_DB_NAME).exists() and not (slug / QDRANT_STORE_NAME).exists()
    assert not (slug / LANCEDB_STORE_NAME).exists()
    assert (slug / "unknown_other_file.txt").exists()  # not a known entry: kept, dir kept
    assert (env.data / "workspaces" / "notaslug" / COGNITIVE_DB_NAME).exists()  # name shape wrong
    assert (env.data / "lifecycle" / "journal.json").exists()


def test_emptied_workspace_dir_is_removed_but_not_a_populated_one(env):
    other = env.data / "workspaces" / "solo-abcdef0123"
    write(other / COGNITIVE_DB_NAME, "db")
    run(env, ["memory"], execute=True, confirm_phrase=PHRASE)
    assert not other.exists()
    assert (env.data / "workspaces" / SLUG).exists()


def test_project_files_journal_and_outside_are_never_touched(env):
    every = [c.name for c in purge.CATEGORIES]
    run(env, every, execute=True, confirm_phrase=PHRASE)
    assert (env.tmp / "project" / "main.py").exists()
    assert (env.tmp / "project" / ".velune" / "snapshots" / "a").exists()
    assert (env.data / "lifecycle" / "journal.json").exists()
    assert (env.outside / "precious.txt").read_text(encoding="utf-8") == "do not delete"
    for root in (env.home, env.cfg, env.data):
        assert root.is_dir()  # roots themselves are never removed


# ---- link / path safety ---------------------------------------------------------------


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")


def test_symlinked_entry_is_an_anomaly_and_blocks_execute(env):
    shutil.rmtree(env.home / "sessions")
    _symlink_or_skip(env.home / "sessions", env.outside)
    preview = run(env, ["sessions"])
    assert preview.result["anomalies"] and "symlink" in preview.result["anomalies"][0]["note"]
    result = run(env, ["sessions"], execute=True, confirm_phrase=PHRASE)
    assert result.exit_code is ExitCode.REFUSED
    assert (env.outside / "precious.txt").exists()


def test_symlink_inside_a_target_directory_blocks_it(env):
    _symlink_or_skip(env.home / "sessions" / "evil", env.outside)
    result = run(env, ["sessions"], execute=True, confirm_phrase=PHRASE)
    assert result.exit_code is ExitCode.REFUSED
    assert (env.outside / "precious.txt").exists() and (env.home / "sessions" / "s1.json").exists()


def test_symlinked_root_is_rejected(env, tmp_path):
    real = tmp_path / "realcfg"
    env.cfg.rename(real)
    _symlink_or_skip(env.cfg, real)
    result = run(env, ["credentials"], execute=True, confirm_phrase=PHRASE)
    assert result.exit_code is ExitCode.REFUSED and result.result["root_problems"]
    assert (real / "credentials.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows feature")
def test_junction_is_an_anomaly_and_its_target_is_untouched(env):
    shutil.rmtree(env.home / "sessions")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(env.home / "sessions"), str(env.outside)],
        capture_output=True,
    )
    if made.returncode != 0:
        pytest.skip("could not create a junction")
    result = run(env, ["sessions"], execute=True, confirm_phrase=PHRASE)
    assert result.exit_code is ExitCode.REFUSED
    assert (env.outside / "precious.txt").exists()


def test_symlinked_workspace_slug_is_skipped(env):
    link = env.data / "workspaces" / "link-aaaaaaaaaa"
    _symlink_or_skip(link, env.outside)
    result = run(env, ["memory"], execute=True, confirm_phrase=PHRASE)
    assert result.exit_code is ExitCode.REFUSED
    assert (env.outside / "precious.txt").exists()


@pytest.mark.parametrize("which", ["fsroot", "home", "repo", "cwd"])
def test_dangerous_roots_are_refused(env, tmp_path, monkeypatch, which):
    if which == "fsroot":
        bad = Path(tmp_path.anchor)
    elif which == "home":
        bad = tmp_path / "fakehome"
        bad.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: bad))
    elif which == "repo":
        bad = tmp_path / "repo"
        (bad / ".git").mkdir(parents=True)
    else:
        bad = tmp_path / "work"
        bad.mkdir()
        monkeypatch.chdir(bad)
    if which != "fsroot":  # never write into a real filesystem root
        write(bad / "credentials.json", SECRET)
    roots = purge.Roots(env.home, bad, env.data)
    result = purge.run_purge(("credentials",), execute=True, confirm_phrase=PHRASE, roots=roots)
    assert result.exit_code is ExitCode.REFUSED, which
    if which != "fsroot":
        assert (bad / "credentials.json").exists()


def test_containment_is_component_wise_not_string_prefix(tmp_path):
    root = tmp_path / "data"
    sibling = tmp_path / "data-evil"
    root.mkdir()
    sibling.mkdir()
    assert purge._inside(root, root / "a") is True
    assert purge._inside(root, sibling) is False
    assert purge._inside(root, root) is False


def test_path_traversal_names_cannot_enter_the_inventory():
    for cat in purge.CATEGORIES:
        for entry in cat.entries:
            assert entry == Path(entry).name and ".." not in entry and "/" not in entry
            assert "\\" not in entry


# ---- failures, races ------------------------------------------------------------------


def test_target_changed_after_preview_stops_without_widening(env):
    plan = purge.build_plan(("sessions", "skills"), env.roots)
    (env.home / "sessions" / "s1.json").write_text("changed after preview!", encoding="utf-8")
    report = purge.execute_plan(plan)
    assert report.stopped and "changed after the preview" in report.stopped
    assert (env.home / "commands" / "mine.md").exists()  # later target untouched
    assert (env.home / "sessions" / "s1.json").exists()


def test_new_file_added_after_preview_is_never_deleted(env):
    plan = purge.build_plan(("sessions",), env.roots)
    write(env.home / "sessions" / "late.json", "new")
    report = purge.execute_plan(plan)
    assert report.stopped and (env.home / "sessions" / "late.json").exists()


def test_partial_failure_stops_and_reports_honestly(env):
    calls = []

    def flaky(path: Path) -> None:
        calls.append(path.name)
        if len(calls) == 2:
            raise PermissionError("C:\\Users\\bob\\locked")
        os.unlink(path)

    result = run(env, ["logs", "cache"], execute=True, confirm_phrase=PHRASE, remove_file=flaky)
    assert result.exit_code is ExitCode.UNCERTAIN
    assert result.result["stopped"] and result.result["failed"][0]["error"] == "PermissionError"
    assert len(calls) == 2  # nothing attempted after the failure
    assert "bob" not in json.dumps(result.to_dict())
    assert "partial" in result.message


def test_missing_entries_are_not_an_error(env):
    shutil.rmtree(env.home)
    result = run(env, ["cache", "sessions"], execute=True, confirm_phrase=PHRASE)
    assert result.exit_code is ExitCode.OK


def test_no_shell_rmtree_or_subprocess_is_used(env, monkeypatch):
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: pytest.fail("rmtree"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("subprocess"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("subprocess"))
    monkeypatch.setattr(os, "system", lambda *a, **k: pytest.fail("shell"))
    result = run(env, ["sessions", "memory"], execute=True, confirm_phrase=PHRASE)
    assert result.exit_code is ExitCode.OK


def test_output_never_contains_secret_values(env):
    for execute in (False, True):
        result = run(env, ["credentials"], execute=execute, confirm_phrase=PHRASE)
        assert SECRET not in json.dumps(result.to_dict()) and SECRET not in result.render_text()


# ---- uninstall stays separate ------------------------------------------------------------


def test_uninstall_module_never_references_purge_or_deletion():
    src = (Path(purge.__file__).parent / "uninstall.py").read_text(encoding="utf-8")
    assert "purge" not in src.lower().replace("never purges", "").replace("no purge", "")
    assert "rmtree" not in src and "unlink(" not in src.replace("journal", "")


# ---- CLI ----------------------------------------------------------------------------------


def _app():
    import typer

    from velune.cli.commands import purge_data as cmd

    app = typer.Typer()
    app.command()(cmd.purge_data_cmd)
    return app


def test_cli_without_terminal_requires_phrase(monkeypatch, env):
    real = purge.run_purge
    monkeypatch.setattr(purge, "run_purge", lambda *a, **k: real(*a, roots=env.roots, **k))
    runner = CliRunner()
    out = runner.invoke(_app(), ["-c", "cache", "--execute", "--json"])
    assert out.exit_code == 3 and (env.home / "model_registry_cache.json").exists()
    out = runner.invoke(_app(), ["-c", "cache", "--execute", "--confirm-phrase", "yes", "--json"])
    assert out.exit_code == 130 and (env.home / "model_registry_cache.json").exists()
    out = runner.invoke(_app(), ["-c", "cache", "--execute", "--confirm-phrase", PHRASE, "--json"])
    assert out.exit_code == 0 and not (env.home / "model_registry_cache.json").exists()


def test_registered_and_help_runs():
    import importlib

    from velune.cli.registry import COMMAND_SPECS

    (spec,) = [s for s in COMMAND_SPECS if s.name == "purge-data"]
    assert spec.bootstrap == "light"
    assert callable(getattr(importlib.import_module(spec.module), spec.attr))
    done = subprocess.run(
        [sys.executable, "-m", "velune", "purge-data", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=_cli_env(),
    )
    assert done.returncode == 0 and "--execute" in _plain(done.stdout)


# ---- L2/M3: exit codes for a stopped purge, and the scripted confirmation ---------------------


def test_a_stop_before_anything_was_deleted_is_exit_1_and_says_the_data_is_untouched(env):
    def locked(path: Path) -> None:
        raise PermissionError("C:\\Users\\bob\\locked")

    before = snap(env.tmp)
    result = run(env, ["logs", "cache"], execute=True, confirm_phrase=PHRASE, remove_file=locked)
    assert result.exit_code is ExitCode.ERROR and result.to_dict()["status"] == "error"
    assert result.result["removed_items"] == 0 and "not touched" in result.message
    assert snap(env.tmp) == before and "bob" not in json.dumps(result.to_dict())


def test_a_stop_after_some_deletions_is_exit_9_and_counts_what_was_removed(env):
    calls = []

    def flaky(path: Path) -> None:
        calls.append(path)
        if len(calls) == 2:
            raise PermissionError("locked")
        os.unlink(path)

    result = run(env, ["logs", "cache"], execute=True, confirm_phrase=PHRASE, remove_file=flaky)
    assert result.exit_code is ExitCode.UNCERTAIN and result.exit_code == 9
    assert result.to_dict()["status"] == "uncertain" and result.result["removed_items"] >= 1
    assert "partial" in result.message


def test_scripted_confirmation_is_possible_and_documented_but_never_widens_the_selection(env):
    """The public phrase permits non-interactive deletion (documented), but only of what is named."""
    before = snap(env.tmp)
    assert run(env, [], execute=True, confirm_phrase=PHRASE).exit_code is ExitCode.OK
    assert snap(env.tmp) == before  # --execute with no category stays a read-only inventory
    assert run(env, ["cache"], execute=True, confirm_phrase="yes").exit_code is ExitCode.CANCELLED
    assert snap(env.tmp) == before  # a wrong phrase deletes nothing
    run(env, ["cache"], execute=True, confirm_phrase=PHRASE)
    after = snap(env.tmp)
    assert not any("model_registry_cache" in k for k in after)  # the named category is gone
    assert {k for k in before if "credentials" in k or "sessions" in k or "memory" in k} <= set(
        after
    )  # protected categories were not named, so they survive a scripted run


def test_the_scripted_confirmation_caveat_is_in_the_docs():
    root = Path(purge.__file__).resolve().parents[2]
    for doc in ("docs/data-inventory.md", "docs/lifecycle.md", "SECURITY.md"):
        text = " ".join((root / doc).read_text(encoding="utf-8").lower().split())
        assert "not equivalent to interactive" in text or "not the same as an interactive" in text
        assert "unattended" in text
