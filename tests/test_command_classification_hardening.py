"""A command is "read-only" only if its arguments are too, and naming a secret file is a secret read.

``classify_command`` matched a *prefix* ("find ", "git log", "git fetch", "git branch", ...). A
read-only command is never shown to the user in MANUAL mode and is even allowed in PLAN mode, so
``find . -delete`` deleted files, ``git log --output=f`` overwrote any file in the workspace
(bypassing the conflict checks, the journal and /rewind), ``git branch -f``/``git tag`` changed
refs, and ``git fetch --upload-pack=CMD`` ran CMD, all without a prompt. Separately, ``cat .env``
was a plain read while ``read_file .env`` asks first.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from velune.permissions import ExecutionMode
from velune.permissions.boundary import Boundary, command_paths_secret
from velune.permissions.commands import CommandClass, classify
from velune.permissions.gate import Approval
from velune.tools.base.tool import authorize_and_execute
from velune.tools.safety import ApprovalMode, classify_command

from ._fs_registry import gated_ctx, production_registry

NEEDS_APPROVAL = [
    # find: every predicate that deletes, writes a file or runs a program
    "find . -delete",
    "find . -exec rm -rf {} +",
    "find . -name x -exec sh -c id sh {} +",
    "find . -execdir touch x {} +",
    "find . -ok rm {} ;",
    "find . -fprint out.txt",
    "find . -fprintf out.txt %p",
    "find . -fls out.txt",
    "FIND . -DELETE",
    "find.exe . -delete",
    "./find . -delete",
    # git: options that write a file or run a program, whatever the spelling
    "git log --output=keep.txt",
    "git diff --output keep.txt",
    "git show --output=keep.txt",
    "git log --outp=keep.txt",
    'git fetch --upload-pack="touch x" .',
    "git log --upload-pack=x",
    "git log --receive-pack=x",
    "git log --exec=x",
    # git: network and ref-changing forms of subcommands that are read-only when listing
    "git fetch",
    "git fetch origin",
    "git branch newbranch",
    "git branch -f b HEAD",
    "git branch -d old",
    "git branch -m a b",
    "git branch --set-upstream-to=origin/main",
    "git tag poc-tag",
    "git tag -d v1",
    "git tag -a v1 -m message",
    "git tag -l -d v1",
    "git remote add x http://example.test/r.git",
    "git remote set-url origin http://example.test/r.git",
    "git remote remove origin",
    "git remote show origin",
    "git stash pop",
    "git stash drop",
    # other programs that can run something per file
    "rg --pre=cmd foo",
    "rg --pre cmd foo",
    "fd -x rm",
    "fd --exec rm",
    "fd pattern -xtouch marker.txt",
    "fd pattern -Xtouch marker.txt",
    "fd pattern -Hxtouch marker.txt",
    # quotes inside a token are removed by the executor: the classifier must see what it sees
    'find . -de"lete"',
    "find . -dele'te'",
    'find . -"exec" rm {} +',
    'git log --out"put"=keep.txt',
    # helpers a repository can configure
    "git log -p --ext-diff",
    "git diff --textconv",
    "git show --ext-diff",
]

READ_ONLY = [
    "ls -la",
    "cat README.md",
    "echo hello",
    "grep -rn foo .",
    "rg foo",
    "fd pattern",
    "find . -name *.py",
    "find src -type f -newer x",
    "git status",
    "git log --oneline -5",
    "git log --output-indicator-new=x",
    "git diff HEAD~1",
    "git diff --stat",
    "git show HEAD",
    "git rev-parse HEAD",
    "git shortlog -sn",
    "git stash list",
    "git branch",
    "git branch -a",
    "git branch -r",
    "git branch -vv",
    "git branch --show-current",
    "git tag",
    "git tag -l",
    "git tag --list v*",
    "git remote",
    "git remote -v",
    "git remote get-url origin",
    "fd -H pattern",
    "fd -e py -E node_modules foo",
    'git log --format="%h %s"',
]


@pytest.mark.parametrize("command", NEEDS_APPROVAL)
def test_a_command_with_a_writing_or_executing_option_is_not_read_only(command):
    assert classify_command(command).mode is not ApprovalMode.SAFE, command
    assert classify(command)[0] is not CommandClass.READ_ONLY, command


@pytest.mark.parametrize(
    "command",
    [
        c
        for c in NEEDS_APPROVAL
        if classify(c)[0] is not CommandClass.BLOCKED
        and classify(c)[0] is not CommandClass.HIGH_RISK
    ],
)
def test_the_rest_ask_as_ordinary_mutating_commands(command):
    assert classify_command(command).mode is ApprovalMode.ASK, command
    assert classify(command)[0] is CommandClass.MUTATING, command


@pytest.mark.parametrize("command", READ_ONLY)
def test_genuinely_read_only_commands_stay_read_only(command):
    assert classify_command(command).mode is ApprovalMode.SAFE, command
    assert classify(command)[0] is CommandClass.READ_ONLY, command


def test_dangerous_commands_are_still_blocked_before_anything_else():
    assert classify("find . -exec rm -rf {} +")[0] is CommandClass.BLOCKED
    assert classify("git status; sudo id")[0] is CommandClass.BLOCKED


def test_the_classifier_sees_the_arguments_the_executor_runs():
    """The reviewer's bypass: quotes inside a token vanish at execution, so judge the result."""
    from velune.core.argv import split_command
    from velune.execution.command_spec import CommandSpec

    for command in (
        'find . -de"lete"',
        "find . -dele'te'",
        'cat .e"nv"',
        "git log --format='%h %s' -n 3",
        r"python C:\work\run.py --flag # not a comment",
        "grep -r 'a b' src",
    ):
        spec = CommandSpec.from_string(command, Path("."))
        assert split_command(command) == [command.split()[0], *spec.args], command


def test_an_unbalanced_quote_is_not_a_read():
    from velune.core.argv import split_command

    with pytest.raises(ValueError):
        split_command('git log --format="oops')
    assert classify_command('git log --format="oops').mode is ApprovalMode.ASK


# ── naming a secret file ───────────────────────────────────────────────────────────────────────


def test_command_paths_secret_finds_the_files_a_command_names(tmp_path):
    boundary = Boundary(tmp_path)
    names = lambda command: [p.name for p in command_paths_secret(command, boundary)]  # noqa: E731
    assert names("cat .env") == [".env"]
    assert names("cat config/.env.production") == [".env.production"]
    assert names('cat "id_rsa"') == ["id_rsa"]
    assert names("grep --file=.npmrc x") == [".npmrc"]
    assert names("cat .ssh/config") == ["config"]
    assert names('cat .e"nv"') == [".env"]  # quotes vanish at execution
    assert names("cat README.md src/app.py") == []
    assert names("cat .env.example") == []  # a template that only looks like a secret
    assert names("git log --oneline -5") == []
    assert names("cat https://example.test/.env") == []  # not a local file
    assert names("cat " + "x" * 5000) == []  # an invalid name does not raise


def test_a_read_command_naming_a_secret_file_asks_like_read_file_does(tmp_path):
    tool = production_registry(tmp_path).get("execute_command")
    boundary = Boundary(tmp_path)
    [plain] = tool.describe_actions({"command": "cat README.md"}, boundary)
    [secret] = tool.describe_actions({"command": "cat .env"}, boundary)
    assert not plain.secret
    assert secret.secret


# ── the proof of concept, end to end through the real gate ─────────────────────────────────────


class _Approver:
    """The user's approval prompt; records every time it is shown and answers *answer*."""

    def __init__(self, answer: Approval = Approval.DENY):
        self.answer = answer
        self.prompts: list[str] = []

    async def __call__(self, tool_name, decision):
        self.prompts.append(decision.reason)
        return self.answer


async def _run(tmp_path: Path, command: str, mode=ExecutionMode.MANUAL, approver=None):
    approver = approver or _Approver()
    tool = production_registry(tmp_path).get("execute_command")
    ctx = gated_ctx(tmp_path, mode, ask=approver)
    ctx.permissions = tool.get_required_permissions()
    try:
        return await authorize_and_execute(tool, ctx, command=command), approver
    except Exception as exc:  # the gate refused, or the command failed
        return exc, approver


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git_repo(path: Path) -> None:
    path.mkdir(exist_ok=True)
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
            cwd=path,
            check=True,
            capture_output=True,
        )


@pytest.mark.skipif(shutil.which("find") is None, reason="find is not installed")
async def test_find_delete_is_no_longer_run_without_asking(tmp_path):
    victim = tmp_path / "victim.tmp"
    victim.write_text("junk", encoding="utf-8")
    result, approver = await _run(tmp_path, "find . -name victim.tmp -delete")
    assert approver.prompts, "the user was never asked"
    assert victim.exists(), "the file was deleted although the user said no"
    assert "rejected" in str(result)


@needs_git
async def test_git_log_output_cannot_overwrite_a_file_without_asking(tmp_path):
    _git_repo(tmp_path)
    keep = tmp_path / "keep.txt"
    keep.write_text("important", encoding="utf-8")
    result, approver = await _run(tmp_path, "git log --output=keep.txt")
    assert approver.prompts
    assert keep.read_text(encoding="utf-8") == "important"


@needs_git
async def test_git_fetch_upload_pack_cannot_run_a_program_without_asking(tmp_path):
    _git_repo(tmp_path)
    result, approver = await _run(tmp_path, 'git fetch --upload-pack="touch marker.txt" .')
    assert approver.prompts
    assert not (tmp_path / "marker.txt").exists()


@needs_git
async def test_git_branch_and_tag_changes_ask(tmp_path):
    _git_repo(tmp_path)
    for command in ("git branch -f poc HEAD", "git tag poc-tag"):
        _, approver = await _run(tmp_path, command)
        assert approver.prompts, command
    refs = subprocess.run(
        ["git", "for-each-ref", "refs/tags", "refs/heads/poc"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    ).stdout
    assert "poc" not in refs


@needs_git
async def test_an_approved_command_still_runs(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "keep.txt").write_text("important", encoding="utf-8")
    result, approver = await _run(
        tmp_path, "git log --output=log.txt", approver=_Approver(Approval.ALLOW_ONCE)
    )
    assert approver.prompts
    assert (tmp_path / "log.txt").exists()  # approval, not refusal: the command is still usable


@needs_git
async def test_plan_mode_refuses_a_writing_command_it_used_to_allow(tmp_path):
    """PLAN mode is read-only; a "read" that writes must not slip through it."""
    _git_repo(tmp_path)
    (tmp_path / "keep.txt").write_text("important", encoding="utf-8")
    result, approver = await _run(tmp_path, "git log --output=keep.txt", mode=ExecutionMode.PLAN)
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "important"
    assert "important" not in str(result) or isinstance(result, Exception)


@needs_git
async def test_genuine_reads_still_run_without_a_prompt(tmp_path):
    _git_repo(tmp_path)
    for command in ("git status", "git log --oneline", "git branch", "git tag"):
        result, approver = await _run(tmp_path, command)
        assert not approver.prompts, command
        assert not isinstance(result, Exception), (command, result)


async def test_cat_of_a_secret_file_asks_but_cat_of_a_normal_file_does_not(tmp_path):
    (tmp_path / ".env").write_text("DB_PASSWORD=hunter2-not-real\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    secret, approver = await _run(tmp_path, "cat .env")
    assert approver.prompts, "the secret file was read without asking"
    assert "hunter2" not in str(secret)
    normal, approver = await _run(tmp_path, "cat README.md")
    assert not approver.prompts
    assert "hello" in str(normal)


def _symlink_to_secret(tmp_path: Path) -> Path:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".env").write_text("DB_PASSWORD=not-real", encoding="utf-8")
    try:
        (sub / "public.txt").symlink_to(sub / ".env")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    return sub


def test_a_link_to_a_secret_is_seen_from_the_folder_the_command_runs_in(tmp_path):
    sub = _symlink_to_secret(tmp_path)
    boundary = Boundary(tmp_path)
    assert command_paths_secret("cat public.txt", boundary, cwd=sub)  # sub/public.txt -> .env
    assert not command_paths_secret("cat public.txt", boundary)  # tmp_path/public.txt: nothing


def test_the_action_resolves_the_command_in_its_directory(tmp_path):
    _symlink_to_secret(tmp_path)
    tool = production_registry(tmp_path).get("execute_command")
    boundary = Boundary(tmp_path)
    [action] = tool.describe_actions({"command": "cat public.txt", "directory": "sub"}, boundary)
    assert action.secret
    [action] = tool.describe_actions({"command": "cat public.txt"}, boundary)
    assert not action.secret  # a different, ordinary path when run from the workspace root
