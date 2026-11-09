"""`velune repair` (P2.4): reinstalling the current version of a confirmed uv tool.

No real uv and no real repair: ``run`` is a fake that edits a fake installation.
The user's data is simulated by a sentinel tree that must never change.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from velune.lifecycle import journal as journal_mod
from velune.lifecycle import repair as repair_mod
from velune.lifecycle.errors import ExitCode
from velune.lifecycle.integrity import Integrity
from velune.lifecycle.ownership import OwnershipKind, OwnershipReport

UV = "/usr/bin/uv"
DISTS = (("velune-cli", "0.9.9"), ("click", "8"))
MANUAL = 'uv tool install --force "velune-cli==0.9.9"'


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
    def __init__(self, tmp_path: Path, **kw):
        self.state = report(**kw)
        self.journal = tmp_path / "lifecycle" / "journal.json"
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.on_uv = lambda: None
        self.uv_result = SimpleNamespace(returncode=0)
        self.uv_raises: BaseException | None = None
        self.record, self.problems, self.dists = "intact", (), DISTS
        self.entry = (True, None)
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

    def detect(self):
        return self.state

    def integrity(self):
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

    def go(self, **kw):
        return repair_mod.run_repair(**(self.defaults() | kw))

    def defaults(self):
        return {
            "detect": self.detect,
            "run": self.run,
            "integrity": self.integrity,
            "entry_point": lambda prefix, plat: self.entry,
            "which": lambda name: UV if name == "uv" else None,
            "journal_path": self.journal,
            "python": (3, 12, 4),
            "platform": "linux",
            "current_prefix": "/somewhere/else",
            "environ": {},
        }


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def untouched(w: World) -> bool:
    return w.snapshot() == w.before


# ---- success ---------------------------------------------------------------


def test_repairs_with_an_argument_list_pinning_the_installed_version(world):
    result = world.go()
    assert result.exit_code is ExitCode.OK
    assert result.result["outcome"] == "repaired_verified" and result.result["repaired"] is True
    assert world.calls == [[UV, "tool", "install", "--force", "velune-cli==0.9.9"]]
    assert world.kwargs == [{"timeout": repair_mod.UV_TIMEOUT}]  # no shell, no index options
    assert not world.journal.exists()
    assert "not run" in " ".join(result.warnings)  # honest about what was verified
    assert untouched(world)


def test_extras_are_preserved(tmp_path):
    w = World(tmp_path, extras=("rag", "git"))
    result = w.go()
    assert w.calls == [[UV, "tool", "install", "--force", "velune-cli[rag,git]==0.9.9"]]
    assert result.exit_code is ExitCode.OK


def test_exact_pin_equal_to_installed_version_is_repaired(tmp_path):
    w = World(tmp_path, specifier="==0.9.9")
    result = w.go()
    assert result.exit_code is ExitCode.OK
    assert not any("pins this installation" in x for x in result.warnings)


def test_unpinned_install_is_told_it_becomes_pinned(world):
    result = world.go()
    assert any("pins this installation to 0.9.9" in x for x in result.warnings)


def test_journal_is_applying_during_uv_then_removed(world):
    world.go()
    assert world.journal_seen == ["applying"]
    assert not world.journal.exists()


def test_planned_phase_precedes_applying(world, monkeypatch):
    phases = []
    real = journal_mod.advance_phase

    def spy(entry, phase, path=None):
        phases.append((journal_mod.read_journal(path).entry.phase, phase))
        return real(entry, phase, path)

    monkeypatch.setattr(journal_mod, "advance_phase", spy)
    world.go()
    assert phases == [("planned", "applying")]


def test_journal_records_operation_version_and_extras(tmp_path):
    w = World(tmp_path, extras=("rag",), specifier="==0.9.9")
    w.record, w.problems = "damaged", ("1 file(s) missing",)  # leaves the journal behind
    w.go()
    entry = journal_mod.read_journal(w.journal).entry
    assert entry.operation == "repair" and entry.phase == "applying"
    assert entry.from_version == entry.to_version == "0.9.9"
    assert entry.extras == ("rag",) and entry.specifier == "==0.9.9"


def test_changed_dependency_versions_are_reported(world):
    world.on_uv = lambda: setattr(world, "dists", (("velune-cli", "0.9.9"), ("click", "9")))
    result = world.go()
    assert result.exit_code is ExitCode.OK
    assert any("some installed packages differ" in x for x in result.warnings)


def test_index_override_is_warned_about_without_its_value(world):
    result = world.go(environ={"UV_INDEX_URL": "https://user:pw@example.test/simple"})
    assert any("UV_INDEX_URL" in x for x in result.warnings)
    assert "pw@" not in json.dumps(result.to_dict())


# ---- refusals: all before mutation ------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        OwnershipKind.NOT_UV,
        OwnershipKind.EDITABLE,
        OwnershipKind.AMBIGUOUS,
        OwnershipKind.INDETERMINATE,
    ],
)
def test_unconfirmed_ownership_is_refused(tmp_path, kind):
    w = World(tmp_path, kind=kind)
    result = w.go()
    assert result.exit_code is ExitCode.REFUSED
    assert w.calls == [] and not w.journal.exists() and untouched(w)


def test_windows_running_installation_is_refused_with_manual_command(world):
    result = world.go(platform="win32", current_prefix="/t/velune-cli")
    assert result.exit_code is ExitCode.REFUSED
    assert result.manual_command == MANUAL
    assert "fresh terminal" in (result.recovery or "")
    assert world.calls == [] and not world.journal.exists()


def test_windows_different_environment_is_allowed(world):
    assert world.go(platform="win32", current_prefix="/other").exit_code is ExitCode.OK


@pytest.mark.parametrize("version", [None, "", "1.0 && rm -rf /", "$(x)"])
def test_unreadable_version_is_refused_without_a_guess(tmp_path, version):
    w = World(tmp_path, version=version)
    result = w.go()
    assert result.exit_code is ExitCode.REFUSED
    assert result.result["reason"] == "version_unreadable"
    assert w.calls == [] and not w.journal.exists()


def test_unsafe_extras_are_refused(tmp_path):
    w = World(tmp_path, extras=("rag", "x]; evil"))
    result = w.go()
    assert result.result["reason"] == "extras_unreadable" and w.calls == []


def test_local_file_install_is_refused(tmp_path):
    w = World(tmp_path, installed_from_local_file=True, direct_url="archive")
    result = w.go()
    assert result.exit_code is ExitCode.REFUSED and result.result["reason"] == "non_index_source"
    assert w.calls == [] and not w.journal.exists()


@pytest.mark.parametrize("direct", ["vcs", "archive", "directory", "malformed"])
def test_direct_url_sources_are_refused(tmp_path, direct):
    w = World(tmp_path, direct_url=direct)
    assert w.go().result["reason"] == "non_index_source"


@pytest.mark.parametrize("spec", [">=0.9,<1", "==0.9.8", "~=0.9", "!=0.9.0"])
def test_non_exact_constraints_are_refused(tmp_path, spec):
    w = World(tmp_path, specifier=spec)
    result = w.go()
    assert result.exit_code is ExitCode.REFUSED
    assert result.result["reason"] == "constraint_not_reproducible"
    assert w.calls == [] and not w.journal.exists()


def test_existing_journal_blocks_and_is_not_overwritten(world):
    world.journal.parent.mkdir(parents=True)
    world.journal.write_text("keep me", encoding="utf-8")
    result = world.go()
    assert result.exit_code is ExitCode.REFUSED and result.result["reason"] == "journal_exists"
    assert world.journal.read_text(encoding="utf-8") == "keep me"
    assert world.calls == []


def test_missing_uv_refuses_before_any_journal(world):
    result = world.go(which=lambda name: None)
    assert result.exit_code is ExitCode.REFUSED and result.result["reason"] == "uv_not_found"
    assert result.manual_command == MANUAL
    assert not world.journal.exists() and world.calls == []


def test_journal_write_failure_means_uv_never_runs(world, monkeypatch):
    def boom(entry, path=None):
        raise OSError("C:\\Users\\bob\\secret disk full")

    monkeypatch.setattr(journal_mod, "write_journal", boom)
    result = world.go()
    assert result.exit_code is ExitCode.ERROR
    assert world.calls == []
    assert "bob" not in json.dumps(result.to_dict())


def test_detection_failure_before_mutation_is_sanitised(world):
    def bad():
        raise RuntimeError("C:\\Users\\bob\\token=abc")

    result = world.go(detect=bad)
    assert result.exit_code is ExitCode.ERROR and "bob" not in json.dumps(result.to_dict())
    assert world.calls == []


# ---- failure during / after mutation ---------------------------------------


def test_uv_failure_with_verified_intact_installation(world):
    world.uv_result = SimpleNamespace(returncode=2)
    result = world.go()
    assert result.exit_code is ExitCode.ERROR
    assert result.result["outcome"] == "failed_intact" and result.result["repaired"] is False
    assert "appears intact" in result.message
    assert not world.journal.exists()  # positive evidence nothing changed
    assert result.manual_command == MANUAL


def test_uv_success_exit_code_is_not_trusted_when_files_are_damaged(world):
    world.record, world.problems = "damaged", ("3 file(s) missing (e.g. a.py)",)
    result = world.go()
    assert result.exit_code is ExitCode.UNCERTAIN
    assert result.result["outcome"] == "uncertain" and result.result["repaired"] is None
    assert world.journal.exists()
    assert "3 file(s) missing" in result.message


def test_uv_failure_that_also_changed_packages_is_uncertain(world):
    world.uv_result = SimpleNamespace(returncode=1)
    world.on_uv = lambda: setattr(world, "dists", (("velune-cli", "0.9.9"),))
    result = world.go()
    assert result.result["outcome"] == "uncertain" and world.journal.exists()


def test_uv_failure_without_a_baseline_is_uncertain(world):
    world.uv_result = SimpleNamespace(returncode=1)
    world.dists = None
    result = world.go()
    assert result.result["outcome"] == "uncertain" and world.journal.exists()


def test_uv_crash_mid_mutation_keeps_the_journal(world):
    def wreck():
        world.state = report(OwnershipKind.INDETERMINATE, version=None)

    world.on_uv, world.uv_raises = wreck, OSError("C:\\Users\\bob")
    result = world.go()
    assert result.exit_code is ExitCode.UNCERTAIN and result.result["outcome"] == "uncertain"
    assert world.journal.exists() and len(world.calls) == 1  # no automatic retry
    assert "bob" not in json.dumps(result.to_dict())


def test_timeout_with_damage_is_uncertain(world):
    world.uv_raises = subprocess.TimeoutExpired("uv", 1)
    world.record, world.problems = "damaged", ("x",)
    result = world.go()
    assert result.result["outcome"] == "uncertain" and world.journal.exists()


def test_version_drift_after_uv_is_uncertain(world):
    world.on_uv = lambda: setattr(world, "state", report(version="0.9.10"))
    result = world.go()
    assert result.result["outcome"] == "uncertain" and world.journal.exists()
    assert "0.9.10" in result.message


def test_unchanged_version_alone_never_clears_the_journal(world):
    world.uv_result = SimpleNamespace(returncode=1)
    world.record, world.problems = "unverifiable", ("no RECORD",)
    result = world.go()
    assert result.result["outcome"] == "uncertain" and world.journal.exists()


def test_extras_lost_by_uv_are_detected(tmp_path):
    w = World(tmp_path, extras=("rag",))
    w.on_uv = lambda: setattr(w, "state", report(extras=()))
    result = w.go()
    assert result.result["outcome"] == "uncertain" and w.journal.exists()
    assert "extras" in result.message


def test_missing_launcher_is_uncertain(world):
    world.entry = (False, "the 'velune' launcher is missing from the environment")
    result = world.go()
    assert result.result["outcome"] == "uncertain" and world.journal.exists()
    assert "launcher is missing" in result.message


def test_ownership_lost_after_uv_is_uncertain(world):
    world.on_uv = lambda: setattr(world, "state", report(OwnershipKind.AMBIGUOUS))
    assert world.go().result["outcome"] == "uncertain"


def test_post_check_crash_is_uncertain_and_sanitised(world):
    def boom(prefix, plat):
        raise RuntimeError("C:\\Users\\bob\\secret")

    result = world.go(entry_point=boom)
    assert result.result["outcome"] == "uncertain" and world.journal.exists()
    assert "bob" not in json.dumps(result.to_dict())


# ---- cancellation ------------------------------------------------------------


def test_cancel_with_verified_intact_installation(world):
    world.uv_raises = KeyboardInterrupt()
    result = world.go()
    assert result.exit_code is ExitCode.CANCELLED
    assert result.result["outcome"] == "failed_intact" and not world.journal.exists()


def test_cancel_that_left_damage_keeps_the_journal(world):
    world.uv_raises = KeyboardInterrupt()
    world.record, world.problems = "damaged", ("x",)
    result = world.go()
    assert result.exit_code is ExitCode.UNCERTAIN and result.result["outcome"] == "uncertain"
    assert world.journal.exists()


def test_cancel_is_never_reported_as_repaired(world):
    world.uv_raises = KeyboardInterrupt()
    world.on_uv = lambda: setattr(world, "dists", (("velune-cli", "0.9.9"),))
    assert world.go().result["outcome"] == "uncertain"


# ---- output + isolation ------------------------------------------------------


def test_no_real_subprocess_and_only_the_journal_is_deleted(world, monkeypatch):
    removed: list[str] = []
    real_unlink = Path.unlink

    def spy(self, *args, **kwargs):
        removed.append(self.name)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", spy)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("real subprocess"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("real subprocess"))
    world.go()
    assert all(n.startswith(".journal-") or n == "journal.json" for n in removed)
    assert untouched(world)


def test_every_outcome_leaves_user_data_alone(tmp_path):
    tweaks = [
        lambda w: None,
        lambda w: setattr(w, "uv_raises", OSError("x")),
        lambda w: setattr(w, "record", "damaged"),
        lambda w: setattr(w, "uv_raises", KeyboardInterrupt()),
    ]
    for i, tweak in enumerate(tweaks):
        w = World(tmp_path / str(i))
        tweak(w)
        w.go()
        assert untouched(w)


def test_json_output_is_sanitised_and_consistent(tmp_path):
    w = World(tmp_path, prefix="C:\\Users\\bob\\AppData\\uv\\tools\\velune-cli")
    w.record, w.problems = "damaged", ("1 file(s) missing",)
    result = w.go()
    data = result.to_dict()
    text = result.render_text()
    assert data["command"] == "repair" and data["status"] == "uncertain"
    assert data["result"]["outcome"] == "uncertain"
    assert "bob" not in json.dumps(data) and "bob" not in text


# ---- entry point verification (real files) -------------------------------------


def test_verify_entry_point_on_a_synthetic_environment(tmp_path):
    env = tmp_path / "env"
    assert repair_mod.verify_entry_point(env, "linux")[0] is False
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "velune").write_text("#!", encoding="utf-8")
    assert repair_mod.verify_entry_point(env, "linux") == (True, None)
    (env / "Scripts").mkdir()
    assert repair_mod.verify_entry_point(env, "win32")[0] is False
    (env / "Scripts" / "velune.exe").write_bytes(b"MZ")
    assert repair_mod.verify_entry_point(env, "win32") == (True, None)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_launcher_pointing_outside_the_environment_is_not_ok(tmp_path):
    env, outside = tmp_path / "env", tmp_path / "other"
    (env / "bin").mkdir(parents=True)
    outside.mkdir()
    (outside / "velune").write_text("#!", encoding="utf-8")
    (env / "bin" / "velune").symlink_to(outside / "velune")
    ok, problem = repair_mod.verify_entry_point(env, "linux")
    assert ok is False and "outside" in (problem or "")


# ---- CLI -------------------------------------------------------------------------


def _app():
    import typer

    from velune.cli.commands import repair as cmd

    app = typer.Typer()
    app.command()(cmd.repair_cmd)
    return app


def test_cli_text_and_json_exit_codes(monkeypatch, world):
    real = repair_mod.run_repair
    monkeypatch.setattr(
        repair_mod, "run_repair", lambda: real(**(world.defaults() | {"platform": "linux"}))
    )
    out = CliRunner().invoke(_app(), [])
    assert out.exit_code == 0 and "Repaired Velune 0.9.9" in out.output

    world.state = report(OwnershipKind.NOT_UV)
    out = CliRunner().invoke(_app(), ["--json"])
    assert out.exit_code == 3 and json.loads(out.output)["status"] == "refused"


def test_registered_in_the_command_table():
    import importlib

    from velune.cli.registry import COMMAND_SPECS

    (spec,) = [s for s in COMMAND_SPECS if s.name == "repair"]
    assert spec.kind == "command" and spec.bootstrap == "light"
    assert callable(getattr(importlib.import_module(spec.module), spec.attr))


def test_real_cli_help_runs_without_uv_or_changes():
    done = subprocess.run(
        [sys.executable, "-m", "velune", "repair", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "NO_COLOR": "1", "COLUMNS": "200", "PYTHONIOENCODING": "utf-8"},
        timeout=120,
    )
    assert done.returncode == 0 and "--json" in done.stdout
