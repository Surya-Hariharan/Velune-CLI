"""Cross-command lifecycle contract (P2.7): update, repair, uninstall and purge agree.

These tests do not re-test each command; they pin the rules the commands must share,
so a change to one cannot quietly weaken another.
"""

from __future__ import annotations

import ast
import json
from dataclasses import fields
from pathlib import Path

import pytest

from velune.lifecycle import apply as apply_mod
from velune.lifecycle import journal as journal_mod
from velune.lifecycle import purge as purge_mod
from velune.lifecycle import repair as repair_mod
from velune.lifecycle import uninstall as uninstall_mod
from velune.lifecycle.errors import ExitCode
from velune.lifecycle.integrity import Integrity
from velune.lifecycle.ownership import OwnershipKind, OwnershipReport

LIFECYCLE = Path(journal_mod.__file__).parent
PREFIX = "/tools/velune-cli"
UV = "/usr/bin/uv"


def rep(kind=OwnershipKind.UV_TOOL, **kw) -> OwnershipReport:
    base = {
        "kind": kind,
        "reasons": ("ok",),
        "prefix": PREFIX,
        "version": "0.9.9",
        "specifier": None,
        "extras": ("rag",),
    }
    return OwnershipReport(**(base | kw))


class Spy:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))


def common(tmp_path, report, spy):
    return {
        "detect": lambda: report,
        "run": spy,
        "which": lambda name: UV,
        "journal_path": tmp_path / "lifecycle" / "journal.json",
        "python": (3, 12, 4),
        "platform": "linux",
        "current_prefix": "/somewhere/else",
    }


def invoke(op, tmp_path, report, spy, **extra):
    kw = common(tmp_path, report, spy)
    if op == "update":
        kw["fetch"] = lambda: {"0.9.9": [{"yanked": False}], "0.9.10": [{"yanked": False}]}
        kw["integrity"] = lambda: Integrity("intact", (), 1, (("velune-cli", "0.9.9"),))
        kw["environ"] = {}
        return apply_mod.run_update(**(kw | extra))
    if op == "repair":
        kw["integrity"] = lambda: Integrity("intact", (), 1, (("velune-cli", "0.9.9"),))
        kw["entry_point"] = lambda prefix, plat: (True, None)
        kw["environ"] = {}
        return repair_mod.run_repair(**(kw | extra))
    kw["assume_yes"] = True
    kw["observer"] = lambda prefix: uninstall_mod.Observed(True, True, True, frozenset(), True)
    kw["integrity"] = lambda: Integrity("intact", (), 1, None)
    return uninstall_mod.run_uninstall(**(kw | extra))


OPS = ["update", "repair", "uninstall"]


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize(
    "kind",
    [
        OwnershipKind.NOT_UV,
        OwnershipKind.EDITABLE,
        OwnershipKind.AMBIGUOUS,
        OwnershipKind.INDETERMINATE,
    ],
)
def test_every_mutation_refuses_every_unconfirmed_ownership(tmp_path, op, kind):
    spy = Spy()
    result = invoke(op, tmp_path, rep(kind), spy)
    assert result.exit_code is ExitCode.REFUSED
    assert spy.calls == [] and not (tmp_path / "lifecycle").exists()


@pytest.mark.parametrize("op", OPS)
def test_windows_running_installation_is_manual_only_for_every_mutation(tmp_path, op):
    spy = Spy()
    result = invoke(op, tmp_path, rep(), spy, platform="win32", current_prefix=PREFIX)
    assert result.exit_code is ExitCode.REFUSED
    assert result.result["reason"] == "windows_running_installation"
    assert result.manual_command and "fresh terminal" in (result.recovery or "")
    assert spy.calls == [] and not (tmp_path / "lifecycle").exists()


@pytest.mark.parametrize("op", OPS)
def test_existing_journal_is_never_overwritten_by_any_mutation(tmp_path, op):
    journal = tmp_path / "lifecycle" / "journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_text("earlier operation", encoding="utf-8")
    spy = Spy()
    result = invoke(op, tmp_path, rep(), spy)
    assert result.exit_code is ExitCode.REFUSED and result.result["reason"] == "journal_exists"
    assert journal.read_text(encoding="utf-8") == "earlier operation" and spy.calls == []


@pytest.mark.parametrize("op", OPS)
def test_uv_is_always_called_with_an_argument_list_and_no_extra_options(tmp_path, op):
    spy = Spy()
    invoke(op, tmp_path, rep(), spy)
    assert len(spy.calls) <= 1
    for argv in spy.calls:
        assert argv[0] == UV and argv[1] == "tool"
        assert all(isinstance(a, str) and " " not in a for a in argv)
        assert not any(a.startswith("--index") or a in ("--pre", "--python") for a in argv)


@pytest.mark.parametrize("op", OPS)
def test_journal_during_uv_has_only_known_non_secret_fields(tmp_path, op):
    seen = {}
    journal = tmp_path / "lifecycle" / "journal.json"

    def spy(argv, **kwargs):
        seen.update(json.loads(journal.read_text(encoding="utf-8")))

    invoke(op, tmp_path, rep(), spy)
    assert seen["operation"] == op and seen["phase"] == "applying"
    assert set(seen) <= {f.name for f in fields(journal_mod.JournalEntry)}
    blob = json.dumps(seen)
    assert "sk-" not in blob and str(tmp_path) not in blob  # no secrets, no local paths


@pytest.mark.parametrize("op", OPS)
def test_results_share_one_json_shape_and_exit_codes(tmp_path, op):
    result = invoke(op, tmp_path, rep(OwnershipKind.NOT_UV), Spy())
    data = result.to_dict()
    assert {
        "command",
        "status",
        "exit_code",
        "message",
        "installation",
        "plan",
        "result",
        "warnings",
    } <= set(data)
    assert data["exit_code"] in {int(c) for c in ExitCode}
    assert data["command"] == op or op == "update"
    assert result.render_text().startswith(data["message"].split("\n")[0][:20])


def test_purge_result_uses_the_same_shape(tmp_path):
    roots = purge_mod.Roots(tmp_path / "h", tmp_path / "c", tmp_path / "d")
    data = purge_mod.run_purge((), roots=roots).to_dict()
    assert {"command", "status", "exit_code", "message", "result", "warnings"} <= set(data)
    assert data["command"] == "purge-data" and data["exit_code"] == 0


# ---- journal wording matches the operation that wrote it ------------------------------


@pytest.mark.parametrize(
    ("op", "needle", "forbidden"),
    [
        ("update", "partly updated", "partly removed"),
        ("repair", "still damaged", "partly updated"),
        ("uninstall", "partly removed", "partly updated"),
    ],
)
def test_retained_journal_message_describes_its_own_operation(tmp_path, op, needle, forbidden):
    report = rep()
    entry = journal_mod.new_entry(op, report, python_version="3.12.4")
    path = tmp_path / "j.json"
    journal_mod.write_journal(entry, path)
    journal_mod.advance_phase(entry, "applying", path)
    err = journal_mod.retained_error(path)
    assert needle in err.message and forbidden not in err.message
    assert err.details["operation"] == op and err.details["phase"] == "applying"


def test_uninstall_journal_guidance_does_not_tell_people_to_reinstall(tmp_path):
    entry = journal_mod.new_entry("uninstall", rep(), python_version="3.12.4")
    path = tmp_path / "j.json"
    journal_mod.write_journal(entry, path)
    text = journal_mod.retained_error(path).recovery or ""
    assert "uv tool uninstall velune-cli" in text and "install --force" not in text
    assert "Do not delete the journal before checking" in text


def test_a_journal_is_not_cleared_by_another_operations_entry(tmp_path):
    a = journal_mod.new_entry("repair", rep(), python_version="3.12.4")
    path = tmp_path / "j.json"
    journal_mod.write_journal(a, path)
    other = journal_mod.new_entry("uninstall", rep(version="0.9.8"), python_version="3.12.4")
    assert journal_mod.clear_journal(other, path) is False and path.exists()


# ---- static audit of the lifecycle package ----------------------------------------------


def _calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def test_no_shell_execution_or_recursive_deletion_in_the_lifecycle_package():
    offenders = []
    for source in sorted(LIFECYCLE.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for call in _calls(tree):
            name = ast.unparse(call.func)
            if name in {"os.system", "os.popen", "shutil.rmtree", "os.removedirs"}:
                offenders.append((source.name, name))
            for kw in call.keywords:
                if kw.arg == "shell" and not (
                    isinstance(kw.value, ast.Constant) and kw.value.value is False
                ):
                    offenders.append((source.name, "shell=..."))
    assert offenders == []


def test_only_known_modules_start_processes_or_delete_files():
    starts, deletes = set(), set()
    for source in sorted(LIFECYCLE.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for call in _calls(tree):
            name = ast.unparse(call.func)
            if name in {
                "subprocess.Popen",
                "subprocess.run",
                "subprocess.check_output",
                "subprocess.check_call",
                "subprocess.call",
            }:
                starts.add(source.name)
            if name in {"os.unlink", "os.remove", "os.rmdir"} or name.endswith(".unlink"):
                deletes.add(source.name)
        text = source.read_text(encoding="utf-8")
        if "os.unlink" in text or "os.rmdir" in text:
            deletes.add(source.name)
    # uv launches: apply.py (shared runner) and the read-only 'uv tool dir' in ownership.py.
    assert starts <= {"apply.py", "ownership.py"}, starts
    # deletion: the journal's own file, and the purge command's validated plan.
    assert deletes <= {"journal.py", "purge.py"}, deletes


def test_uv_is_never_resolved_with_shutil_which():
    """F1: shutil.which can return ./uv.bat from the cwd on Windows (Python 3.10/3.11)."""
    offenders = []
    for source in sorted(LIFECYCLE.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "shutil.which":
                args = [a.value for a in node.args if isinstance(a, ast.Constant)]
                if "uv" in args:
                    offenders.append((source.name, node.lineno))
            # also a bare reference used as a default (e.g. `which=shutil.which`)
            if isinstance(node, ast.Attribute) and ast.unparse(node) == "shutil.which":
                if source.name != "uninstall.py":  # only used there to *locate* the launcher
                    offenders.append((source.name, node.lineno))
    assert offenders == []


def test_no_pip_is_ever_invoked_by_lifecycle_code():
    for source in LIFECYCLE.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert '"pip"' not in text and "'pip'" not in text, source.name
        assert "-m pip" not in text.replace("pip install ", ""), source.name


def test_uninstall_has_no_deletion_primitives_at_all():
    text = (LIFECYCLE / "uninstall.py").read_text(encoding="utf-8")
    for needle in ("unlink", "rmdir", "rmtree", "os.remove", "purge_mod", "lifecycle.purge"):
        assert needle not in text, needle


def test_lifecycle_modules_stay_importable_without_third_party_modules():
    """The commands must work in a damaged environment, so they may not need heavy deps."""
    allowed_third_party = {"httpx", "packaging", "platformdirs", "tomli", "typer"}
    stdlib = __import__("sys").stdlib_module_names
    for source in LIFECYCLE.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                assert mod in stdlib or mod in allowed_third_party or mod == "velune", (
                    source.name,
                    mod,
                )


# ---- L2: one exit-code contract for every command ---------------------------------------------
#
# 1 = failed, state verified unchanged;  3/4/8 = nothing was touched;  9 = a change was started
# and the outcome is unverified or partial (journal kept);  130 = cancelled with the state verified.


def _crash(argv, **kwargs):
    raise RuntimeError("boom C:\\Users\\bob\\secret")


def _interrupt(argv, **kwargs):
    raise KeyboardInterrupt()


def _damaged_after_the_first_look(first_intact: bool = True):
    """Integrity that is intact before uv runs (when there is a "before" look) and damaged after."""
    calls = {"n": 0}

    def integrity():
        calls["n"] += 1
        intact = first_intact and calls["n"] == 1
        record = "intact" if intact else "damaged"
        problems = () if intact else ("1 file(s) missing",)
        return Integrity(record, problems, 1, (("velune-cli", "0.9.9"),))

    return integrity


@pytest.mark.parametrize("op", OPS)
def test_missing_uv_is_a_refusal_before_any_change(tmp_path, op):
    spy = Spy()
    result = invoke(op, tmp_path, rep(), spy, which=lambda name: None)
    assert result.exit_code is ExitCode.REFUSED and result.exit_code == 3
    assert result.result["reason"] == "uv_not_found" and result.to_dict()["status"] == "refused"
    assert spy.calls == [] and not (tmp_path / "lifecycle").exists()


@pytest.mark.parametrize("op", OPS)
def test_a_lost_journal_race_is_busy_and_changes_nothing(tmp_path, op, monkeypatch):
    def lose(tmp, target):
        raise FileExistsError(target)

    monkeypatch.setattr(journal_mod, "_publish_exclusive", lose)
    spy = Spy()
    result = invoke(op, tmp_path, rep(), spy)
    assert result.exit_code is ExitCode.BUSY and result.exit_code == 8
    assert result.result["reason"] == "journal_race" and result.to_dict()["status"] == "busy"
    assert spy.calls == [] and list((tmp_path / "lifecycle").iterdir()) == []


@pytest.mark.parametrize("op", OPS)
def test_failure_with_the_state_verified_unchanged_is_exit_1_and_clears_the_journal(tmp_path, op):
    result = invoke(op, tmp_path, rep(), Spy())  # uv "runs" and changes nothing
    assert result.exit_code is ExitCode.ERROR and result.to_dict()["status"] == "error"
    assert not (tmp_path / "lifecycle" / "journal.json").exists()


@pytest.mark.parametrize("op", OPS)
def test_cancel_with_the_state_verified_is_130_and_clears_the_journal(tmp_path, op):
    result = invoke(op, tmp_path, rep(), _interrupt)
    assert result.exit_code is ExitCode.CANCELLED and result.exit_code == 130
    assert not (tmp_path / "lifecycle" / "journal.json").exists()


@pytest.mark.parametrize("runner", [Spy(), _interrupt], ids=["uv-returned", "cancelled"])
@pytest.mark.parametrize("op", OPS)
def test_an_unverifiable_result_is_9_even_when_cancelled_and_keeps_the_journal(
    tmp_path, op, runner
):
    integrity = _damaged_after_the_first_look(first_intact=op != "uninstall")
    result = invoke(op, tmp_path, rep(), runner, integrity=integrity)
    assert result.exit_code is ExitCode.UNCERTAIN and result.exit_code == 9
    assert result.to_dict()["status"] == "uncertain" and result.manual_command
    assert (tmp_path / "lifecycle" / "journal.json").exists()


@pytest.mark.parametrize("op", OPS)
def test_an_unexpected_error_after_uv_started_is_9_not_1(tmp_path, op):
    result = invoke(op, tmp_path, rep(), _crash)
    assert result.exit_code is ExitCode.UNCERTAIN
    assert (tmp_path / "lifecycle" / "journal.json").exists()  # kept: a person must look
    assert "bob" not in json.dumps(result.to_dict()) and "bob" not in result.render_text()


@pytest.mark.parametrize("op", OPS)
def test_an_unexpected_error_before_any_change_stays_exit_1(tmp_path, op):
    def broken_detect():
        raise RuntimeError("C:\\Users\\bob\\secret")

    result = invoke(op, tmp_path, rep(), Spy(), detect=broken_detect)
    assert result.exit_code is ExitCode.ERROR and not (tmp_path / "lifecycle").exists()


def test_the_documented_exit_code_table_matches_the_enum():
    import re

    from velune.lifecycle.errors import category_for

    text = (LIFECYCLE.parents[1] / "docs" / "lifecycle.md").read_text(encoding="utf-8")
    rows = {
        int(m.group(1)): m.group(2)
        for m in re.finditer(r"^\| (\d+) \| ([a-z_]+) \|", text, flags=re.MULTILINE)
    }
    used = {c for c in ExitCode if c.value not in (5, 6, 7)}
    assert set(rows) == {int(c) for c in used}
    for code in used:
        assert rows[int(code)] == category_for(code).value
    assert "5, 6 and 7" in text and "reserved" in text


def test_reserved_codes_are_never_returned_by_lifecycle_code():
    source = "\n".join(p.read_text(encoding="utf-8") for p in LIFECYCLE.glob("*.py"))
    for name in ("VALIDATION_FAILED", "APPLY_FAILED_ROLLED_BACK", "ROLLBACK_FAILED"):
        assert source.count(f"ExitCode.{name}") == 0


def test_cli_help_documents_code_9_and_the_scripted_confirmation_caveat():
    from velune.cli.commands import purge_data, repair, uninstall, update

    def words(fn) -> str:  # docstring indentation differs by Python version: compare words
        return " ".join((fn.__doc__ or "").split())

    for command in (update.update_cmd, repair.repair_cmd, uninstall.uninstall_cmd):
        assert " 9 " in words(command)
    assert "scriptable" in words(uninstall.uninstall_cmd)
    purge_help = words(purge_data.purge_data_cmd)
    assert "scriptable" in purge_help and "not the same as an interactive" in purge_help
