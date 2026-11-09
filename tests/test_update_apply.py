"""`velune update` (P2.3): applying an update to a confirmed uv tool.

No real uv, no real upgrade: ``run`` is a fake that edits a fake installation.
Tests that compare versions need a working ``packaging`` and skip without it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from velune.lifecycle import apply as apply_mod
from velune.lifecycle import journal as journal_mod
from velune.lifecycle.errors import ExitCode, LifecycleError
from velune.lifecycle.integrity import Integrity
from velune.lifecycle.ownership import OwnershipKind, OwnershipReport

pytest.importorskip("packaging.version")

PY = (3, 12, 4)
UV = "/usr/bin/uv"


def rel(*items: str) -> dict:
    return {v: [{"yanked": False, "requires_python": ">=3.10"}] for v in items}


def report(kind=OwnershipKind.UV_TOOL, version="0.9.9", **kw) -> OwnershipReport:
    base: dict = {
        "kind": kind,
        "reasons": ("ok",),
        "prefix": "/t/velune-cli",
        "version": version,
        "specifier": None,
        "extras": (),
    }
    return OwnershipReport(**(base | kw))


class World:
    """A fake installation plus a fake uv that can change it."""

    def __init__(self, tmp_path: Path, **kw):
        self.state = report(**kw)
        self.journal = tmp_path / "lifecycle" / "journal.json"
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.on_uv = lambda: None  # edits self.state
        self.uv_result = SimpleNamespace(returncode=0, stdout="", stderr="")
        self.uv_raises: BaseException | None = None
        self.detects = 0
        self.journal_seen: list[str] = []
        # fake file check: what verify_record/list_distributions would report
        self.record = "intact"
        self.problems: tuple[str, ...] = ()
        self.dists: tuple[tuple[str, str], ...] | None = (("velune-cli", "0.9.9"), ("click", "8"))
        self.integrity_calls = 0
        self.integrity_raises_on: set[int] = set()

    def detect(self):
        self.detects += 1
        return self.state

    def integrity(self):
        self.integrity_calls += 1
        if self.integrity_calls in self.integrity_raises_on:
            raise RuntimeError("C:\\Users\\bob\\secret")
        return Integrity(self.record, self.problems, 9, self.dists)

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        read = journal_mod.read_journal(self.journal)
        self.journal_seen.append(read.entry.phase if read.entry else read.state)
        self.on_uv()
        if self.uv_raises is not None:
            raise self.uv_raises
        return self.uv_result

    def upgrade_to(self, version: str):
        def go():
            self.state = report(version=version)

        self.on_uv = go

    def go(self, releases=None, **kw):
        defaults = {
            "detect": self.detect,
            "fetch": lambda: releases if releases is not None else rel("0.9.9", "0.9.10"),
            "run": self.run,
            "integrity": self.integrity,
            "which": lambda name: UV if name == "uv" else None,
            "journal_path": self.journal,
            "python": PY,
            "platform": "linux",
            "current_prefix": "/somewhere/else",
            "environ": {},
        }
        return apply_mod.run_update(**(defaults | kw))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# ---- success --------------------------------------------------------------


def test_applies_with_an_argument_list_and_verifies_the_new_version(world):
    world.upgrade_to("0.9.10")
    result = world.go()
    assert result.exit_code is ExitCode.OK and result.ok
    assert "Updated Velune 0.9.9 -> 0.9.10" in result.message
    assert result.result["applied"] is True and result.result["installed_version"] == "0.9.10"
    assert world.calls == [[UV, "tool", "upgrade", "velune-cli"]]  # argv list, nothing else
    assert world.kwargs == [{"timeout": apply_mod.UV_TIMEOUT}]  # no shell, no extra options
    assert "index" not in " ".join(world.calls[0]).lower()


def test_journal_is_planned_then_applying_then_removed(world):
    world.upgrade_to("0.9.10")
    seen_during = []
    inner = world.on_uv

    def on_uv():
        seen_during.append(journal_mod.read_journal(world.journal))
        inner()

    world.on_uv = on_uv
    world.go()
    (during,) = seen_during
    assert during.state == "ok" and during.entry.phase == "applying"
    assert during.entry.operation == "update"
    assert during.entry.from_version == "0.9.9" and during.entry.to_version == "0.9.10"
    assert not world.journal.exists()  # verified success clears it


def test_planned_phase_is_written_before_applying(world, monkeypatch):
    phases = []
    real_advance = journal_mod.advance_phase

    def spy(entry, phase, path=None):
        phases.append((journal_mod.read_journal(path).entry.phase, phase))
        return real_advance(entry, phase, path)

    monkeypatch.setattr(journal_mod, "advance_phase", spy)
    world.upgrade_to("0.9.10")
    world.go()
    assert phases == [("planned", "applying")]


def test_extras_are_not_passed_so_uv_keeps_them(tmp_path):
    w = World(tmp_path, extras=("rag", "git"))
    w.upgrade_to("0.9.10")
    w.go()
    assert w.calls == [[UV, "tool", "upgrade", "velune-cli"]]


def test_extras_reach_the_journal_and_recovery_command(tmp_path):
    w = World(tmp_path, extras=("rag", "git"))
    w.on_uv = lambda: setattr(w, "state", report(OwnershipKind.INDETERMINATE, version=None))
    result = w.go()
    entry = journal_mod.read_journal(w.journal).entry
    assert entry.extras == ("rag", "git")
    assert result.manual_command == 'uv tool install --force "velune-cli[rag,git]"'


def test_a_newer_release_than_the_checked_one_is_reported_not_hidden(world):
    world.upgrade_to("0.9.11")
    result = world.go()
    assert result.exit_code is ExitCode.OK and result.result["installed_version"] == "0.9.11"
    assert any("not the 0.9.10" in w for w in result.warnings)


def test_uv_exit_code_is_not_proof_success_is_verified_state(world):
    world.upgrade_to("0.9.10")
    world.uv_result = SimpleNamespace(returncode=1, stdout="", stderr="boom")
    result = world.go()
    assert result.exit_code is ExitCode.OK and result.result["applied"] is True


def test_exit_zero_with_no_change_is_not_reported_as_success(world):
    result = world.go()  # uv "succeeds" but the version never moves
    assert result.exit_code is ExitCode.ERROR and result.result["applied"] is False
    assert "still 0.9.9 (verified)" in result.message
    assert not world.journal.exists()  # verified unchanged: nothing to recover


# ---- nothing to do / index problems --------------------------------------


def test_up_to_date_never_touches_uv_or_the_journal(world):
    result = world.go(rel("0.9.8", "0.9.9"))
    assert result.exit_code is ExitCode.OK and "up to date" in result.message
    assert world.calls == [] and not world.journal.exists()


def test_installed_newer_than_index_is_not_a_downgrade(world):
    result = world.go(rel("0.9.8"))
    assert result.exit_code is ExitCode.OK and "newer than" in result.message
    assert world.calls == []


def test_index_failure_changes_nothing(world):
    def down():
        raise LifecycleError(ExitCode.NETWORK, "no route. Nothing was changed.")

    result = world.go(fetch=down)
    assert result.exit_code is ExitCode.NETWORK
    assert world.calls == [] and not world.journal.exists()


def test_exactly_one_index_request(world):
    count = []
    world.upgrade_to("0.9.10")
    world.go(fetch=lambda: count.append(1) or rel("0.9.10"))
    assert count == [1]


def test_prereleases_are_never_the_target(world):
    world.upgrade_to("0.9.10")
    result = world.go(rel("0.9.9", "0.9.10", "1.0.0rc1"))
    assert result.result["to_version"] == "0.9.10"


def test_yanked_and_incompatible_releases_are_not_targets(world):
    releases = rel("0.9.9") | {
        "0.9.10": [{"yanked": True}],
        "0.9.11": [{"requires_python": ">=3.13"}],
    }
    assert world.go(releases).exit_code is ExitCode.OK
    assert world.calls == []


def test_pre_flag_is_refused_before_any_work(world):
    result = world.go(include_prereleases=True)
    assert result.exit_code is ExitCode.REFUSED
    assert result.to_dict()["result"]["reason"] == "prerelease_apply_unsupported"
    assert world.calls == [] and not world.journal.exists()


# ---- refusals ----------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        OwnershipKind.NOT_UV,
        OwnershipKind.EDITABLE,
        OwnershipKind.AMBIGUOUS,
        OwnershipKind.INDETERMINATE,
    ],
)
def test_every_unconfirmed_ownership_state_is_refused_before_any_work(tmp_path, kind):
    w = World(tmp_path, kind=kind)
    fetched = []
    result = w.go(fetch=lambda: fetched.append(1) or {})
    assert result.exit_code is ExitCode.REFUSED and result.exit_code == 3
    assert "Nothing was changed" in (result.recovery or "") + result.message
    assert result.installation["owned"] is False
    assert w.calls == [] and fetched == [] and not w.journal.exists()


def test_pinned_install_is_refused_with_the_manual_command(tmp_path):
    w = World(tmp_path, specifier="==0.9.8", extras=("rag",))
    result = w.go()
    assert result.exit_code is ExitCode.REFUSED
    assert result.to_dict()["result"]["reason"] == "pinned_install"
    assert result.manual_command == 'uv tool install --force "velune-cli[rag]"'
    assert w.calls == [] and not w.journal.exists()


def test_local_file_install_is_refused(tmp_path):
    w = World(tmp_path, installed_from_local_file=True)
    result = w.go()
    assert result.exit_code is ExitCode.REFUSED
    assert result.to_dict()["result"]["reason"] == "local_file_install"
    assert w.calls == []


def test_windows_never_updates_the_running_installation(tmp_path):
    w = World(tmp_path, prefix="C:/tools/velune-cli")
    result = w.go(platform="win32", current_prefix="C:/tools/velune-cli")
    assert result.exit_code is ExitCode.REFUSED
    assert result.to_dict()["result"]["reason"] == "windows_running_installation"
    assert result.manual_command == "uv tool upgrade velune-cli"
    assert w.calls == [] and not w.journal.exists()


def test_existing_journal_is_never_overwritten(world):
    world.journal.parent.mkdir(parents=True)
    world.journal.write_text("earlier operation", encoding="utf-8")
    result = world.go()
    assert result.exit_code is ExitCode.REFUSED
    assert result.to_dict()["result"]["reason"] == "journal_exists"
    assert world.journal.read_text(encoding="utf-8") == "earlier operation"
    assert world.calls == []


def test_uv_missing_is_a_refusal_before_the_journal(world):
    result = world.go(which=lambda name: None)
    assert result.exit_code is ExitCode.REFUSED
    assert result.manual_command == "uv tool upgrade velune-cli"
    assert world.calls == [] and not world.journal.exists()


def test_journal_write_failure_stops_before_uv(world, monkeypatch):
    def fail(entry, path=None):
        raise OSError("disk full C:\\Users\\bob\\secret")

    monkeypatch.setattr(journal_mod, "write_journal", fail)
    result = world.go()
    assert result.exit_code is ExitCode.ERROR and world.calls == []
    assert "bob" not in json.dumps(result.to_dict())


def test_detection_crash_is_contained(world):
    def boom():
        raise RuntimeError("C:\\Users\\bob\\secret")

    result = world.go(detect=boom)
    assert result.exit_code is ExitCode.ERROR
    assert "bob" not in json.dumps(result.to_dict()) and world.calls == []


# ---- failures during mutation -------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        OSError("C:\\Users\\bob\\uv.exe: denied"),
        subprocess.TimeoutExpired(["uv"], 900),
        subprocess.CalledProcessError(2, ["uv"]),
    ],
)
def test_uv_cannot_run_but_state_is_verified_unchanged(world, exc):
    world.uv_raises = exc
    result = world.go()
    assert result.exit_code is ExitCode.ERROR and result.result["applied"] is False
    assert "still 0.9.9 (verified)" in result.message
    assert "bob" not in json.dumps(result.to_dict())
    assert not world.journal.exists()


def test_failure_that_damages_the_install_keeps_the_journal_and_does_not_roll_back(world):
    def damage():
        world.state = report(OwnershipKind.INDETERMINATE, version=None)

    world.on_uv = damage
    world.uv_result = SimpleNamespace(returncode=1, stdout="", stderr="token=SECRET123")
    result = world.go()
    assert result.exit_code is ExitCode.UNCERTAIN and result.result["applied"] is None
    assert "could not verify" in result.message and "No automatic recovery" in result.message
    assert result.manual_command == "uv tool install --force velune-cli"
    assert "rolled back" not in result.message.lower()
    assert "SECRET123" not in json.dumps(result.to_dict())
    assert len(world.calls) == 1  # no second uv call: no rollback attempted
    kept = journal_mod.read_journal(world.journal)
    assert kept.state == "ok" and kept.entry.phase == "applying"


def test_ownership_lost_after_uv_is_unknown_not_success(world):
    world.on_uv = lambda: setattr(world, "state", report(OwnershipKind.AMBIGUOUS, version="0.9.10"))
    result = world.go()
    assert result.exit_code is ExitCode.UNCERTAIN and result.result["applied"] is None
    assert journal_mod.read_journal(world.journal).state == "ok"


def test_unreadable_version_after_uv_is_unknown(world):
    world.on_uv = lambda: setattr(world, "state", report(version="not a version"))
    result = world.go()
    assert result.result["applied"] is None and world.journal.exists()


def test_downgrade_after_uv_is_not_success(world):
    world.upgrade_to("0.9.0")
    result = world.go()
    assert result.exit_code is ExitCode.UNCERTAIN and result.result["applied"] is None


def test_detection_crash_after_uv_is_unknown_and_journal_kept(world):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("C:\\Users\\bob\\secret")
        return world.state

    result = world.go(detect=flaky)
    assert result.result["applied"] is None and world.journal.exists()
    assert "bob" not in json.dumps(result.to_dict())


def test_another_journal_appearing_mid_run_is_not_deleted(world):
    def swap():
        world.journal.write_text("someone else's journal", encoding="utf-8")
        world.upgrade_to("0.9.10")()

    world.on_uv = swap
    world.go()
    assert world.journal.read_text(encoding="utf-8") == "someone else's journal"


# ---- cancellation ---------------------------------------------------------------


def test_cancel_with_nothing_changed_is_130_and_verified(world):
    world.uv_raises = KeyboardInterrupt()
    result = world.go()
    assert result.exit_code is ExitCode.CANCELLED
    assert "still 0.9.9 (verified)" in result.message
    assert not world.journal.exists()


def test_cancel_that_leaves_a_broken_install_keeps_the_journal(world):
    world.on_uv = lambda: setattr(world, "state", report(OwnershipKind.INDETERMINATE, version=None))
    world.uv_raises = KeyboardInterrupt()
    result = world.go()
    assert result.exit_code is ExitCode.UNCERTAIN and result.result["applied"] is None
    assert world.journal.exists()


def test_cancel_after_the_update_finished_reports_it(world):
    world.upgrade_to("0.9.10")
    world.uv_raises = KeyboardInterrupt()
    result = world.go()
    assert result.exit_code is ExitCode.OK and result.result["applied"] is True
    assert any("deferred until uv finished" in w for w in result.warnings)


def test_cancel_before_mutation_leaves_no_journal(world, monkeypatch):
    real = journal_mod.advance_phase

    def interrupt(entry, phase, path=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(journal_mod, "advance_phase", interrupt)
    result = world.go()
    monkeypatch.setattr(journal_mod, "advance_phase", real)
    assert result.exit_code is ExitCode.CANCELLED
    assert world.calls == [] and not world.journal.exists()


# ---- redaction & output --------------------------------------------------------


def test_uv_output_is_never_shown(world):
    world.uv_result = SimpleNamespace(
        returncode=1, stdout="https://user:hunter2@idx.example/simple", stderr="C:\\Users\\bob"
    )
    result = world.go()
    blob = json.dumps(result.to_dict()) + result.render_text()
    assert "hunter2" not in blob and "bob" not in blob and "idx.example" not in blob


def test_index_override_warning_is_carried_into_the_result(world):
    world.upgrade_to("0.9.10")
    result = world.go(environ={"UV_INDEX_URL": "https://u:p@corp.example/simple"})
    blob = json.dumps(result.to_dict())
    assert "UV_INDEX_URL" in blob and "corp.example" not in blob and "u:p" not in blob


# ---- journal helpers -------------------------------------------------------------


def test_advance_and_clear_only_touch_their_own_journal(tmp_path):
    path = tmp_path / "j" / "journal.json"
    entry = journal_mod.new_entry("update", report(), python_version="3.12.4", to_version="1.0.0")
    journal_mod.write_journal(entry, path)
    other = journal_mod.new_entry(
        "update", report(version="0.1.0"), python_version="3.12.4", to_version="1.0.0"
    )
    with pytest.raises(LifecycleError):
        journal_mod.advance_phase(other, "applying", path)
    assert journal_mod.clear_journal(other, path) is False and path.exists()
    advanced = journal_mod.advance_phase(entry, "applying", path)
    assert journal_mod.read_journal(path).entry.phase == "applying"
    assert journal_mod.clear_journal(advanced, path) is True and not path.exists()


def test_advance_rejects_unknown_phases_and_clear_tolerates_missing(tmp_path):
    path = tmp_path / "journal.json"
    entry = journal_mod.new_entry("update", report(), python_version="3.12.4")
    with pytest.raises(ValueError):
        journal_mod.advance_phase(entry, "done", path)
    assert journal_mod.clear_journal(entry, path) is False


# ---- nothing else changes ----------------------------------------------------------


def _digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_only_the_journal_directory_is_touched(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "data" / "workspaces").mkdir(parents=True)
    (home / "data" / "workspaces" / "memory.db").write_bytes(b"memory")
    (home / "velune.toml").write_text('default_provider = "x"\n')
    (home / "credentials.json").write_bytes(b"encrypted-blob")
    (home / "project").mkdir()
    (home / "project" / "main.py").write_text("print(1)\n")
    for var in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.chdir(home / "project")

    w = World(tmp_path / "w")
    w.journal = home / "data" / "lifecycle" / "journal.json"
    before = _digest(home)

    w.upgrade_to("0.9.10")  # applied
    assert w.go().exit_code is ExitCode.OK

    w.state = report()  # uv does nothing
    w.on_uv = lambda: None
    assert w.go().exit_code is ExitCode.ERROR

    w.state = report()  # uv damages the install; the journal is kept
    w.on_uv = lambda: setattr(w, "state", report(OwnershipKind.INDETERMINATE, version=None))
    w.go()
    assert w.journal.exists()
    w.journal.unlink()
    w.journal.parent.rmdir()

    assert _digest(home) == before
    assert not (home / ".git").exists()


def test_the_only_subprocess_is_the_injected_runner(world, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("must go through the injected runner")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    world.upgrade_to("0.9.10")
    assert world.go().exit_code is ExitCode.OK


def test_source_has_no_shell_or_pip_or_detached_helper():
    source = Path(apply_mod.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[2]
    for forbidden in ("shell=True", "os.system", "pip", "creationflags", "DETACHED"):
        assert forbidden not in body, forbidden
    # the one process start is the argument-list runner, with all three std streams discarded
    assert body.count("Popen(") == 1 and body.count("subprocess.DEVNULL") >= 3


def test_mutation_gates_are_used_in_order():
    source = Path(apply_mod.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[2]
    assert body.index("require_uv_tool(") < body.index("check_not_self_mutation(")
    assert body.index("check_not_self_mutation(") < body.index("write_journal(")
    assert body.index("write_journal(") < body.index("[uv, ")
