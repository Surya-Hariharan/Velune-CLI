"""A git subcommand is judged by its exact name, and ``REV:path`` names a file too.

Two gaps left after the argument-based classification:

* ``classify_command`` matched the *prefix* "git diff", which is also the start of ``git difftool``
  (``-x/--extcmd CMD`` runs CMD: ``git difftool -y -x cp`` overwrote a modified file) and
  "git show" / "git log" / "git branch" are prefixes of other subcommands too. A read-only command
  is never shown to the user in MANUAL mode and is allowed in PLAN mode, so this was a way to run
  a program without being asked.
* ``git show HEAD:.env`` and ``git show :.env`` print a committed / staged secret file but name
  it only inside one argument ("HEAD:.env"), so the secret-read check did not see it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from velune.permissions import ExecutionMode
from velune.permissions.boundary import Boundary, _git_object_path, command_paths_secret
from velune.permissions.commands import CommandClass, classify
from velune.tools.safety import ApprovalMode, classify_command

from ._fs_registry import production_registry
from .test_command_classification_hardening import _Approver, _run, needs_git

# ── git subcommands: exact names, through the real classifiers ─────────────────────────────────

NOT_READ_ONLY = [
    # difftool / mergetool run a program (-x / --extcmd / --tool)
    "git difftool",
    "git difftool -y -x cp",
    "git difftool --no-prompt --extcmd=cp",
    "git difftool --extcmd cp -y",
    "git difftool -x touch",
    "git difftool --tool=vimdiff",
    "git difftool HEAD~1 -- f.txt",
    "git mergetool",
    "git mergetool -y",
    "git mergetool --tool=x",
    # spelling and case variants of the same thing
    "GIT DIFFTOOL -y -x cp",
    "git.exe difftool -y -x cp",
    "./git difftool -y -x cp",
    # other subcommands that merely begin with a safe one
    "git diff-tree HEAD",
    "git diff-index HEAD",
    "git diff-files",
    "git show-branch",
    "git show-ref",
    "git show-index",
    "git logx",
    "git statusx",
    "git branchx",
    "git tagx",
    "git remote-ext",
    "git shortlogx",
    "git rev-parsex HEAD",
    # still not read-only: reaches the network and writes refs
    "git fetch",
    "git fetch origin",
]

STAYS_READ_ONLY = [
    "git diff",
    "git diff --stat",
    "git diff HEAD~1",
    "git diff --cached --name-only",
    "git diff main...feature",
    "git show",
    "git show HEAD",
    "git show --stat HEAD~2",
    "git status",
    "git status -sb",
    "git log",
    "git log --oneline -5",
    "git log -p -- src/app.py",
    "git branch",
    "git branch -a",
    "git branch --show-current",
    "git remote -v",
    "git stash list",
    "git tag",
    "git tag -l",
    "git rev-parse HEAD",
    "git shortlog -sn",
]


@pytest.mark.parametrize("command", NOT_READ_ONLY)
def test_a_look_alike_git_subcommand_is_not_read_only(command):
    assert classify(command)[0] is not CommandClass.READ_ONLY, command
    assert classify_command(command).mode is not ApprovalMode.SAFE, command


@pytest.mark.parametrize("command", STAYS_READ_ONLY)
def test_the_real_read_only_git_subcommands_stay_read_only(command):
    assert classify(command)[0] is CommandClass.READ_ONLY, command
    assert classify_command(command).mode is ApprovalMode.SAFE, command


def _commit_file(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    for args in (["add", name], ["commit", "-q", "-m", f"add {name}"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )


def _init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)


@needs_git
@pytest.mark.parametrize("mode", [ExecutionMode.MANUAL, ExecutionMode.PLAN])
async def test_difftool_cannot_overwrite_a_modified_file_without_asking(tmp_path, mode):
    """The reproduction: ``git difftool -y -x cp`` copied the committed file over the edit."""
    _init(tmp_path)
    _commit_file(tmp_path, "f.txt", "committed\n")
    target = tmp_path / "f.txt"
    target.write_text("UNCOMMITTED WORK\n", encoding="utf-8")
    result, approver = await _run(tmp_path, "git difftool -y -x cp", mode=mode)
    # MANUAL asks (and the answer is no); PLAN is read-only, so it refuses without asking.
    assert approver.prompts or (mode is ExecutionMode.PLAN and isinstance(result, Exception))
    assert target.read_text(encoding="utf-8") == "UNCOMMITTED WORK\n"


@needs_git
async def test_difftool_extcmd_and_mergetool_ask(tmp_path):
    _init(tmp_path)
    _commit_file(tmp_path, "f.txt", "committed\n")
    (tmp_path / "f.txt").write_text("edit\n", encoding="utf-8")
    for command in ("git difftool --extcmd=touch -y", "git mergetool"):
        _, approver = await _run(tmp_path, command)
        assert approver.prompts, command
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "edit\n"


@needs_git
async def test_plain_git_diff_and_show_still_run_without_a_prompt(tmp_path):
    _init(tmp_path)
    _commit_file(tmp_path, "f.txt", "committed\n")
    (tmp_path / "f.txt").write_text("edit\n", encoding="utf-8")
    for command in ("git diff", "git diff --stat", "git show HEAD --stat", "git status"):
        result, approver = await _run(tmp_path, command)
        assert not approver.prompts, command
        assert not isinstance(result, Exception), (command, result)


# ── REV:path names a file ──────────────────────────────────────────────────────────────────────


def _secret_names(command: str, root: Path) -> list[str]:
    return [p.name for p in command_paths_secret(command, Boundary(root))]


def test_a_git_object_name_that_points_at_a_secret_file_is_a_secret_read(tmp_path):
    names = lambda c: _secret_names(c, tmp_path)  # noqa: E731
    assert names("git show HEAD:.env") == [".env"]
    assert names("git show :.env") == [".env"]  # the staged copy
    assert names("git show :0:.env") == [".env"]  # stage number spelled out
    assert names("git show :2:.env") == [".env"]  # a merge's "ours"
    assert names("git show HEAD:config/.env.production") == [".env.production"]
    assert names("git show HEAD~2:./.env") == [".env"]
    assert names("git show main:.ssh/config") == ["config"]
    assert names("git show abc1234:certs/id_rsa") == ["id_rsa"]
    assert names("git show --format=%h HEAD:.env") == [".env"]
    assert names("git show --textconv HEAD:.env") == [".env"]
    assert names("git show --pretty=raw --no-patch HEAD:.env") == [".env"]
    assert names("git diff HEAD:.env HEAD~1:.env") == [".env", ".env"]
    assert names("git diff HEAD:.env HEAD:README.md") == [".env"]
    assert names("git diff --no-index HEAD:.env x") == [".env"]
    assert names("git.exe show HEAD:.env") == [".env"]
    assert names("git show @:.env") == [".env"]
    assert names("git show @{u}:.env") == [".env"]
    assert names("git show HEAD@{10:30}:.env") == [".env"]  # a ":" inside the revision
    assert names("git show HEAD:.ENV") == [".ENV"]
    assert names("git show HEAD:../.env") == [".env"]
    assert names("git show 'HEAD:.env'") == [".env"]  # quotes are gone by execution time
    assert names('git show HEAD:.e"nv"') == [".env"]


def test_the_checks_that_already_worked_still_work(tmp_path):
    names = lambda c: _secret_names(c, tmp_path)  # noqa: E731
    assert names("cat .env") == [".env"]
    assert names("git log -p -- .env") == [".env"]
    assert names("git diff -- config/.env.production") == [".env.production"]
    assert names("git show HEAD -- .env") == [".env"]


def test_ordinary_blob_specs_and_revisions_are_not_secret_reads(tmp_path):
    names = lambda c: _secret_names(c, tmp_path)  # noqa: E731
    assert names("git show HEAD:README.md") == []
    assert names("git show HEAD:src/app.py") == []
    assert names("git show :src/app.py") == []
    assert names("git show HEAD:.env.example") == []  # a template, as for cat
    assert names("git show v1.2:docs/guide.md") == []
    assert names("git show HEAD") == []
    assert names("git show HEAD~1") == []
    assert names("git show stash@{0}") == []
    assert names("git log main..feature") == []
    assert names("git log --since=10:30 --oneline") == []
    assert names("git log --format=%H:%s -3") == []
    assert names("git diff HEAD:src/a.py HEAD~1:src/a.py") == []
    assert names("git show HEAD:") == []  # a tree, no path
    assert names("git show :") == []
    assert names("git show ::") == []
    assert names("git show :0:") == []


def test_only_git_commands_read_a_colon_as_an_object_name(tmp_path):
    # On POSIX "HEAD:.env" is an ordinary (odd) file name, which is not a secret file.
    assert _secret_names("cat HEAD:.env", tmp_path) == []
    assert _secret_names("echo HEAD:.env", tmp_path) == []


def test_a_windows_drive_path_is_not_taken_for_an_object_name():
    assert _git_object_path("C:\\work\\.env") is None
    assert _git_object_path("c:/work/.env") is None
    assert _git_object_path("C:") is None
    assert _git_object_path("HEAD:.env") == ".env"
    assert _git_object_path(":.env") == ".env"
    assert _git_object_path(":1:a/.env") == "a/.env"
    assert _git_object_path("plain") is None
    assert _git_object_path("a:b:c") == "b:c"  # lenient: the path half is looked at, whatever it is


def test_a_malformed_expression_does_not_raise(tmp_path):
    boundary = Boundary(tmp_path)
    for command in (
        "git show :::",
        "git show HEAD::.env",
        'git show "HEAD:.env',
        "git show \x00:.env",
    ):
        command_paths_secret(command, boundary)  # no exception
    # ambiguous but suspicious: the path half is the secret file, so it asks
    assert _secret_names("git show HEAD::.env", tmp_path) == [".env"]


def test_describe_actions_marks_the_blob_read_secret(tmp_path):
    tool = production_registry(tmp_path).get("execute_command")
    boundary = Boundary(tmp_path)
    for command in ("git show HEAD:.env", "git show :.env", "git diff HEAD:.env HEAD~1:.env"):
        [action] = tool.describe_actions({"command": command}, boundary)
        assert action.secret, command
    [action] = tool.describe_actions({"command": "git show HEAD:README.md"}, boundary)
    assert not action.secret


@needs_git
@pytest.mark.parametrize("mode", [ExecutionMode.MANUAL, ExecutionMode.PLAN])
async def test_a_committed_secret_is_not_shown_without_asking(tmp_path, mode):
    _init(tmp_path)
    _commit_file(tmp_path, ".env", "DB_PASSWORD=hunter2-not-real\n")
    result, approver = await _run(tmp_path, "git show HEAD:.env", mode=mode)
    assert approver.prompts, "the secret was read without asking"
    assert "hunter2" not in str(result)


@needs_git
@pytest.mark.parametrize("mode", [ExecutionMode.MANUAL, ExecutionMode.PLAN])
async def test_a_staged_secret_is_not_shown_without_asking(tmp_path, mode):
    _init(tmp_path)
    _commit_file(tmp_path, "README.md", "hello\n")
    (tmp_path / ".env").write_text("TOKEN=staged-not-real\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env"], cwd=tmp_path, check=True, capture_output=True)
    result, approver = await _run(tmp_path, "git show :.env", mode=mode)
    assert approver.prompts
    assert "staged-not-real" not in str(result)


@needs_git
async def test_a_diff_of_two_secret_blobs_asks(tmp_path):
    _init(tmp_path)
    _commit_file(tmp_path, ".env", "A=first-not-real\n")
    _commit_file(tmp_path, ".env", "A=second-not-real\n")
    result, approver = await _run(tmp_path, "git diff HEAD:.env HEAD~1:.env")
    assert approver.prompts
    assert "not-real" not in str(result)


@needs_git
async def test_an_approved_blob_read_still_works(tmp_path):
    _init(tmp_path)
    _commit_file(tmp_path, ".env", "DB_PASSWORD=hunter2-not-real\n")
    from velune.permissions.gate import Approval

    result, approver = await _run(
        tmp_path, "git show HEAD:.env", approver=_Approver(Approval.ALLOW_ONCE)
    )
    assert approver.prompts
    assert "hunter2" in str(result)  # approval, not refusal


@needs_git
async def test_an_ordinary_blob_read_does_not_ask(tmp_path):
    _init(tmp_path)
    _commit_file(tmp_path, "README.md", "hello from the repo\n")
    result, approver = await _run(tmp_path, "git show HEAD:README.md")
    assert not approver.prompts
    assert "hello from the repo" in str(result)
