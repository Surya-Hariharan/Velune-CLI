"""`velune uninstall` (P2.5): program-only removal of a confirmed uv tool.

No real uv and no real uninstall: ``run`` is a fake that edits a fake observed
state. User data is a sentinel tree that must never change.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from velune.lifecycle import apply as apply_mod
from velune.lifecycle import journal as journal_mod
from velune.lifecycle import uninstall as un
from velune.lifecycle.errors import ExitCode
from velune.lifecycle.integrity import Integrity
from velune.lifecycle.ownership import OwnershipKind, OwnershipReport

UV = "/usr/bin/uv"
MANUAL = "uv tool uninstall velune-cli"
PREFIX = "/tools/velune-cli"


def report(kind=OwnershipKind.UV_TOOL, **kw) -> OwnershipReport:
    base: dict = {
        "kind": kind,
        "reasons": ("ok",),
        "prefix": PREFIX,
        "version": "0.9.9",
        "specifier": None,
        "extras": (),
    }
    return OwnershipReport(**(base | kw))


GONE = un.Observed(False, False, False, frozenset({"ruff", "black"}), True)
PRESENT = un.Observed(True, True, True, frozenset({"ruff", "black"}), True)


class World:
    def __init__(self, tmp_path: Path, **kw):
        self.state = report(**kw)
        self.journal = tmp_path / "lifecycle" / "journal.json"
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.on_uv = lambda: setattr(self, "obs", GONE)
        self.uv_raises: BaseException | None = None
        self.obs = PRESENT
        self.intact = True
        self.questions: list[str] = []
        self.journal_seen: list[str] = []
        self.data = tmp_path / "userdata"
        files = {
            "config/velune.toml": "model='x'",
            "creds/keys.json": '{"k":"sk-secret"}',
            "memory/mem.db": "rows",
            "skills/mine/SKILL.md": "skill",
            "project/main.py": "print(1)",
            "project/.git/HEAD": "ref: refs/heads/main",
        }
        for rel, text in files.items():
            f = self.data / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        self.before = self.snapshot()

    def snapshot(self):
        return {
            str(p.relative_to(self.data)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(self.data.rglob("*"))
            if p.is_file()
        }

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        read = journal_mod.read_journal(self.journal)
        self.journal_seen.append(read.entry.phase if read.entry else read.state)
        self.on_uv()
        if self.uv_raises is not None:
            raise self.uv_raises

    def yes(self, text):
        self.questions.append(text)
        return True

    def no(self, text):
        self.questions.append(text)
        return False

    def go(self, **kw):
        defaults = {
            "confirm": self.yes,
            "detect": lambda: self.state,
            "run": self.run,
            "which": lambda name: UV if name == "uv" else None,
            "observer": lambda prefix: self.obs,
            "integrity": lambda: Integrity("intact" if self.intact else "damaged", (), 1, None),
            "journal_path": self.journal,
            "python": (3, 12, 4),
            "platform": "linux",
            "current_prefix": "/somewhere/else",
        }
        return un.run_uninstall(**(defaults | kw))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def untouched(w):
    return w.snapshot() == w.before


# ---- success -----------------------------------------------------------------


def test_uninstalls_with_an_argument_list_and_verifies(world):
    result = world.go()
    assert result.exit_code is ExitCode.OK and result.result["uninstalled"] is True
    assert world.calls == [[UV, "tool", "uninstall", "velune-cli"]]
    assert world.kwargs == [{"timeout": un.UV_TIMEOUT}]
    assert world.journal_seen == ["applying"] and not world.journal.exists()
    assert untouched(world)


def test_confirmation_names_program_scope_and_what_stays(world):
    world.go()
    (text,) = world.questions
    for needle in (
        "velune-cli 0.9.9",
        "uv tool",
        "configuration",
        "credentials",
        "memory",
        "skills",
        "project files",
        "uv cache",
        "any other uv tool",
    ):
        assert needle in text


def test_journal_records_uninstall_planned_then_applying(world, monkeypatch):
    phases = []
    real = journal_mod.advance_phase

    def spy(entry, phase, path=None):
        cur = journal_mod.read_journal(path).entry
        phases.append((cur.operation, cur.phase, phase))
        return real(entry, phase, path)

    monkeypatch.setattr(journal_mod, "advance_phase", spy)
    world.go()
    assert phases == [("uninstall", "planned", "applying")]


def test_yes_skips_only_the_question(world):
    result = world.go(confirm=None, assume_yes=True)
    assert result.exit_code is ExitCode.OK and world.questions == []


# ---- refusals -----------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        OwnershipKind.NOT_UV,
        OwnershipKind.EDITABLE,
        OwnershipKind.AMBIGUOUS,
        OwnershipKind.INDETERMINATE,
    ],
)
def test_unconfirmed_ownership_is_refused_even_with_yes(tmp_path, kind):
    w = World(tmp_path, kind=kind)
    result = w.go(assume_yes=True)
    assert result.exit_code is ExitCode.REFUSED
    assert w.calls == [] and w.questions == [] and not w.journal.exists() and untouched(w)


def test_windows_running_installation_is_refused_even_with_yes(world):
    result = world.go(platform="win32", current_prefix=PREFIX, assume_yes=True)
    assert result.exit_code is ExitCode.REFUSED
    assert result.manual_command == MANUAL and "fresh terminal" in (result.recovery or "")
    assert world.calls == [] and not world.journal.exists()


def test_windows_other_environment_is_allowed(world):
    assert world.go(platform="win32", current_prefix="/other").exit_code is ExitCode.OK


@pytest.mark.parametrize("prefix", ["/tools/ruff", "/tools", "/home/me/project/.venv"])
def test_target_must_be_the_velune_tool_folder(tmp_path, prefix):
    w = World(tmp_path, prefix=prefix)
    result = w.go(assume_yes=True)
    assert result.exit_code is ExitCode.REFUSED
    assert result.result["reason"] == "unexpected_tool_folder" and w.calls == []


def test_non_interactive_without_yes_refuses(world):
    result = world.go(confirm=None)
    assert result.exit_code is ExitCode.REFUSED
    assert result.result["reason"] == "confirmation_unavailable"
    assert world.calls == [] and not world.journal.exists()


def test_declining_cancels_without_changes(world):
    result = world.go(confirm=world.no)
    assert result.exit_code is ExitCode.CANCELLED
    assert world.calls == [] and not world.journal.exists() and untouched(world)


@pytest.mark.parametrize("answer", [None, "yes", 1, ""])
def test_only_a_real_true_counts_as_consent(world, answer):
    result = world.go(confirm=lambda text: answer)
    assert result.exit_code is ExitCode.CANCELLED and world.calls == []


def test_existing_journal_blocks_and_is_kept(world):
    world.journal.parent.mkdir(parents=True)
    world.journal.write_text("keep me", encoding="utf-8")
    result = world.go(assume_yes=True)
    assert result.result["reason"] == "journal_exists" and world.calls == []
    assert world.journal.read_text(encoding="utf-8") == "keep me"


def test_missing_uv_refuses_before_asking_or_journaling(world):
    result = world.go(which=lambda name: None)
    assert result.result["reason"] == "uv_not_found" and result.manual_command == MANUAL
    assert world.questions == [] and not world.journal.exists()


def test_journal_write_failure_means_uv_never_runs(world, monkeypatch):
    def boom(entry, path=None):
        raise OSError("C:\\Users\\bob\\secret")

    monkeypatch.setattr(journal_mod, "write_journal", boom)
    result = world.go()
    assert result.exit_code is ExitCode.ERROR and world.calls == []
    assert "bob" not in json.dumps(result.to_dict())


# ---- failure and verification --------------------------------------------------


def test_uv_failure_with_installation_still_intact(world):
    world.on_uv = lambda: None  # uv "fails": nothing changes
    world.uv_raises = OSError("C:\\Users\\bob")
    result = world.go()
    assert result.exit_code is ExitCode.ERROR and result.result["outcome"] == "failed_intact"
    assert not world.journal.exists() and result.manual_command == MANUAL
    assert "bob" not in json.dumps(result.to_dict())


def test_partly_removed_is_uncertain_and_keeps_the_journal(world):
    world.on_uv = lambda: setattr(world, "obs", replace(PRESENT, receipt_exists=False))
    world.uv_raises = subprocess.TimeoutExpired("uv", 1)
    result = world.go()
    assert result.result["outcome"] == "uncertain" and world.journal.exists()
    assert len(world.calls) == 1  # no retry


def test_folder_left_behind_without_a_receipt_is_not_success(world):
    world.on_uv = lambda: setattr(
        world, "obs", replace(GONE, prefix_exists=True, receipt_exists=False)
    )
    result = world.go()
    assert result.result["outcome"] == "uncertain" and world.journal.exists()


def test_present_but_damaged_is_uncertain(world):
    world.on_uv = lambda: None
    world.intact = False
    assert world.go().result["outcome"] == "uncertain" and world.journal.exists()


def test_launcher_still_resolving_is_not_success(world):
    world.on_uv = lambda: setattr(world, "obs", replace(GONE, launcher_in_prefix=True))
    assert world.go().result["outcome"] == "uncertain" and world.journal.exists()


def test_another_tool_disappearing_is_flagged(world):
    world.on_uv = lambda: setattr(world, "obs", replace(GONE, siblings=frozenset({"ruff"})))
    result = world.go()
    assert result.result["outcome"] == "uncertain" and "another uv tool" in result.message
    assert world.journal.exists()


def test_data_directory_disappearing_is_flagged(world):
    world.on_uv = lambda: setattr(world, "obs", replace(GONE, data_dir_exists=False))
    result = world.go()
    assert result.result["outcome"] == "uncertain" and "data directory" in result.message


def test_observer_crash_is_uncertain(world):
    calls = {"n": 0}

    def observer(prefix):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("C:\\Users\\bob\\secret")
        return PRESENT

    result = world.go(observer=observer)
    assert result.result["outcome"] == "uncertain" and world.journal.exists()
    assert "bob" not in json.dumps(result.to_dict())


def test_cancel_after_a_full_removal_is_not_reported_as_success(world):
    world.uv_raises = KeyboardInterrupt()
    result = world.go()
    assert result.exit_code is ExitCode.UNCERTAIN and result.result["outcome"] == "uncertain"


def test_cancel_with_installation_intact(world):
    world.on_uv = lambda: None
    world.uv_raises = KeyboardInterrupt()
    result = world.go()
    assert result.exit_code is ExitCode.CANCELLED and not world.journal.exists()


# ---- isolation ------------------------------------------------------------------


def test_no_manual_deletion_and_no_real_subprocess(world, monkeypatch):
    removed: list[str] = []
    real_unlink = Path.unlink

    def spy(self, *a, **k):
        removed.append(self.name)
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", spy)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("real subprocess"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("real subprocess"))
    import shutil

    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: pytest.fail("rmtree"))
    monkeypatch.setattr(os, "remove", lambda *a, **k: pytest.fail("os.remove"))
    world.go()
    assert all(n.startswith(".journal-") or n == "journal.json" for n in removed)
    assert untouched(world)


def test_every_outcome_leaves_user_data_alone(tmp_path):
    cases = [
        lambda w: None,
        lambda w: setattr(w, "uv_raises", OSError("x")),
        lambda w: setattr(w, "uv_raises", KeyboardInterrupt()),
        lambda w: setattr(w, "on_uv", lambda: None),
    ]
    for i, tweak in enumerate(cases):
        w = World(tmp_path / str(i))
        tweak(w)
        w.go()
        assert untouched(w)


def test_observe_is_read_only_on_a_synthetic_tool_dir(tmp_path):
    tools = tmp_path / "tools"
    (tools / "velune-cli").mkdir(parents=True)
    (tools / "velune-cli" / "uv-receipt.toml").write_text("x", encoding="utf-8")
    (tools / "ruff").mkdir()
    before = sorted(p.name for p in tools.rglob("*"))
    seen = un.observe(tools / "velune-cli")
    assert seen.prefix_exists and seen.receipt_exists and seen.siblings == {"ruff"}
    assert sorted(p.name for p in tools.rglob("*")) == before
    gone = un.observe(tools / "missing-tool")
    assert not gone.prefix_exists and not gone.receipt_exists


def test_json_output_is_sanitised(tmp_path):
    w = World(tmp_path, prefix="C:\\Users\\bob\\AppData\\uv\\tools\\velune-cli")
    w.on_uv = lambda: None
    w.intact = False
    result = w.go()
    assert "bob" not in json.dumps(result.to_dict()) and "bob" not in result.render_text()
    assert result.to_dict()["command"] == "uninstall"


# ---- CLI --------------------------------------------------------------------------


def _app():
    import typer

    from velune.cli.commands import uninstall as cmd

    app = typer.Typer()
    app.command()(cmd.uninstall_cmd)
    return app


def test_cli_without_a_terminal_refuses_unless_yes(monkeypatch, world):
    real = un.run_uninstall
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return real(
            **(
                kw
                | {
                    "detect": lambda: world.state,
                    "run": world.run,
                    "which": lambda n: UV,
                    "observer": lambda p: world.obs,
                    "integrity": lambda: Integrity("intact", (), 1, None),
                    "journal_path": world.journal,
                    "platform": "linux",
                    "current_prefix": "/other",
                }
            )
        )

    monkeypatch.setattr(un, "run_uninstall", fake)
    out = CliRunner().invoke(_app(), ["--json"])  # CliRunner stdin is not a tty
    assert out.exit_code == 3 and seen["confirm"] is None and seen["assume_yes"] is False
    assert world.calls == []
    out = CliRunner().invoke(_app(), ["--yes", "--json"])
    assert out.exit_code == 0 and json.loads(out.output)["status"] == "ok"


def test_registered_and_has_no_purge_option():
    import importlib

    from velune.cli.registry import COMMAND_SPECS

    (spec,) = [s for s in COMMAND_SPECS if s.name == "uninstall"]
    assert spec.bootstrap == "light"
    func = getattr(importlib.import_module(spec.module), spec.attr)
    assert callable(func)
    done = subprocess.run(
        [sys.executable, "-m", "velune", "uninstall", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env={**os.environ, "NO_COLOR": "1", "COLUMNS": "200", "PYTHONIOENCODING": "utf-8"},
    )
    assert done.returncode == 0 and "--yes" in done.stdout
    assert "purge" not in done.stdout.lower()


# ---- L1: uv_ran says whether uv was actually started -----------------------------------------
#
# Runner contract (see apply._run_uv): OSError means the process could not be started; returning
# (even with a non-zero exit code), TimeoutExpired and KeyboardInterrupt mean it had started.


@pytest.mark.parametrize(
    "exc",
    [FileNotFoundError("uv"), PermissionError("uv"), OSError("Exec format error")],
    ids=["file-not-found", "permission-denied", "exec-format"],
)
def test_uv_ran_is_false_when_the_process_could_not_be_started(world, exc):
    world.on_uv = lambda: None  # nothing changes
    world.uv_raises = exc
    result = world.go()
    assert result.result["uv_ran"] is False
    assert result.exit_code is ExitCode.ERROR and result.result["outcome"] == "failed_intact"
    assert "could not be started" in result.message and not world.journal.exists()


def test_uv_ran_is_true_after_a_successful_run(world):
    result = world.go()
    assert result.exit_code is ExitCode.OK and result.result["uv_ran"] is True


def test_uv_ran_is_true_for_a_non_zero_exit(world):
    result = world.go(run=lambda argv, **kw: subprocess.CompletedProcess(argv, 2))
    assert result.result["uv_ran"] is True and result.exit_code is ExitCode.ERROR
    assert result.result["outcome"] == "failed_intact" and "did not uninstall" in result.message


def test_uv_ran_is_true_for_a_timeout_after_the_process_started(world):
    world.on_uv = lambda: None
    world.uv_raises = subprocess.TimeoutExpired("uv", 900)
    result = world.go()
    assert result.result["uv_ran"] is True and result.result["outcome"] == "failed_intact"
    assert "could not be started" not in result.message


def test_uv_ran_is_true_after_an_interrupt(world):
    world.uv_raises = KeyboardInterrupt()  # uv had started; the removal completed
    result = world.go()
    assert result.result["uv_ran"] is True and result.exit_code is ExitCode.UNCERTAIN


def test_an_unexpected_runner_error_leaves_uv_ran_unknown(world):
    world.on_uv = lambda: None
    world.uv_raises = RuntimeError("C:\\Users\\bob\\x")
    result = world.go()
    assert result.exit_code is ExitCode.UNCERTAIN and result.result["uv_ran"] is None
    assert world.journal.exists() and "bob" not in json.dumps(result.to_dict())


def test_a_vanished_installation_without_uv_running_is_not_reported_as_uninstalled(world):
    world.uv_raises = FileNotFoundError("uv")  # on_uv still flips the observed state to GONE
    result = world.go()
    assert result.result["uv_ran"] is False and result.result["outcome"] == "uncertain"
    assert result.exit_code is ExitCode.UNCERTAIN and result.result["uninstalled"] is None
    assert "could not be started" in result.message and world.journal.exists()


def test_the_uncertain_result_always_says_whether_uv_ran(world):
    world.on_uv = lambda: None
    world.intact = False  # present but damaged
    assert world.go().result["uv_ran"] is True


# The real runner, with no uv: a missing executable never starts; a started process that fails
# is a normal return. Both are exercised through run_uninstall so the contract is end to end.


def test_real_runner_with_a_missing_executable_reports_uv_ran_false(world, tmp_path):
    result = world.go(run=apply_mod._run_uv, which=lambda name: str(tmp_path / "no-such-uv"))
    assert result.result["uv_ran"] is False and result.exit_code is ExitCode.ERROR


def test_real_runner_with_a_process_that_fails_reports_uv_ran_true(world, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # python is asked to run a script called "tool": there is none
    result = world.go(run=apply_mod._run_uv, which=lambda name: sys.executable)
    assert result.result["uv_ran"] is True and result.exit_code is ExitCode.ERROR
    assert result.result["outcome"] == "failed_intact"


def test_the_real_runner_raises_oserror_only_for_a_process_that_never_started(tmp_path):
    with pytest.raises(OSError):
        apply_mod._run_uv([str(tmp_path / "no-such-uv")], timeout=5)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        apply_mod._run_uv([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5)
    assert not isinstance(caught.value, OSError)  # started, then stopped: not "never ran"


def test_stopping_a_process_that_already_exited_does_not_raise_oserror():
    class Gone:
        def kill(self):
            raise ProcessLookupError("already exited")

        def wait(self, timeout=None):
            return 0

    apply_mod._stop(Gone())  # an OSError here would be misread as "uv never started"
