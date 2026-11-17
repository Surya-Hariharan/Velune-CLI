"""A shell command that *changes* a credentials file is sensitive, whatever it is classified as.

``ExecuteCommand.describe_actions`` set ``secret`` only for commands classified read-only
(``cat .env``). A command that writes the same file (``echo x > .env``, ``Set-Content .env``,
``cp .env.example .env``) is not read-only, so it was never checked: in AUTO it ran unattended,
although ``write_file`` on ``.env`` always asks. The policy already turns ``secret`` on a
state-changing action into DENY in PLAN and ASK in MANUAL and AUTO (never covered by a session
grant); these tests pin that the *command* path now reaches it, and only for real writes.

Secret files are whatever ``velune.permissions.boundary.is_secret_path`` recognises; templates
(``.env.example`` ...) are deliberately not secret.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from velune.permissions import ExecutionMode
from velune.permissions.boundary import Boundary, secret_among
from velune.permissions.commands import command_write_targets
from velune.permissions.policy import PolicyState, Verdict, authorize
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


def _actions(command: str, workspace: Path, directory: str | None = None):
    tool = ExecuteCommand(sandbox=object(), workspace_path=str(workspace))
    args = {"command": command}
    if directory:
        args["directory"] = directory
    return tool.describe_actions(args, Boundary(workspace))


def _verdict(command: str, mode: ExecutionMode, workspace: Path) -> Verdict:
    return authorize(_actions(command, workspace), PolicyState(mode=mode)).verdict


SECRET_WRITES = [
    # redirection: truncate, append, glued, descriptor-prefixed, combined, spaced and quoted target
    "echo x > .env",
    "echo x >> .env",
    "echo x >.env",
    "echo x >>.env",
    "echo x 2> .env",
    "echo x 2>.env",
    "echo x &> .env",
    "echo x >| .env",
    'echo x > ".env"',
    "echo x > '.env'",
    "cat a.txt > .env",
    "git log > .env",
    # the variants the security policy already treats as secret
    "echo x > .env.local",
    "echo x > .env.production",
    "echo x > deploy.key",
    "echo x > server.pem",
    "echo x > .npmrc",
    "echo x > .netrc",
    "echo x > .ssh/config",
    "echo x > sub/dir/.env",
    "echo x > ./.env",
    "echo X > .ENV",
    # PowerShell
    "Set-Content .env hi",
    "Set-Content -Path .env -Value x",
    "Set-Content -LiteralPath .env -Value x",
    "set-content .env hi",
    "Add-Content .env x",
    "echo x | Out-File .env",
    "Out-File -FilePath .env -InputObject x",
    "echo x | Tee-Object .env",
    "New-Item .env",
    "Clear-Content .env",
    "Remove-Item .env",
    "Export-Csv .env",
    # POSIX / cmd write commands
    "echo x | tee .env",
    "tee -a .env",
    "touch .env",
    "truncate -s 0 .env",
    "rm .env",
    "rm -f .env",
    "del .env",
    "erase .env",
    "sed -i s/a/b/ .env",
    "sed -i.bak s/a/b/ .env",
    "sed --in-place s/a/b/ .env",
    "perl -pi -e s/a/b/ .env",
    "dd of=.env",
    # copy: the destination
    "cp .env.example .env",
    "cp a.txt .env",
    "cp -f a.txt .env",
    "cp -t .env a.txt",
    "cp --target-directory=.env a.txt",
    "copy a.txt .env",
    "Copy-Item a.txt .env",
    "Copy-Item -Path a.txt -Destination .env",
    "install a.txt .env",
    # move: source and destination both change
    "mv a.txt .env",
    "mv .env old.txt",
    "mv -f a.txt .env",
    "move a.txt .env",
    "move .env old.txt",
    "Move-Item a.txt .env",
    "Move-Item .env old.txt",
    "ren .env old.txt",
    "Rename-Item .env old.txt",
    # git commands that overwrite or remove working-tree files
    "git checkout -- .env",
    "git restore .env",
    "git rm .env",
    "git mv .env old.txt",
    # chaining: the write need not be the first command
    "echo a; echo x > .env",
    "ls && echo x > .env",
    "ls | tee .env",
    "echo a || rm .env",
    "echo a & del .env",
    "echo a\nrm .env",
    # wrappers and nested shells
    "env FOO=1 tee .env",
    "env FOO=1 cp a.txt .env",
    "nohup rm .env",
    "timeout 10 tee .env",
    "time cp a.txt .env",
    'cmd /c "echo x > .env"',
    "cmd /c echo x > .env",
    'cmd /c "del .env"',
    'powershell -Command "Set-Content .env x"',
    'pwsh -c "echo x | Out-File .env"',
    "bash -c 'echo x > .env'",
    "sh -c 'cp a .env'",
    'bash -c "cd sub && tee .env"',
]


@pytest.mark.parametrize("command", SECRET_WRITES)
def test_a_secret_file_write_is_flagged_secret(command: str, tmp_path: Path) -> None:
    (action,) = _actions(command, tmp_path)
    assert action.secret, command


@pytest.mark.parametrize("command", SECRET_WRITES)
def test_a_secret_file_write_is_denied_in_plan_and_asks_in_manual_and_auto(
    command: str, tmp_path: Path
) -> None:
    assert _verdict(command, ExecutionMode.PLAN, tmp_path) is Verdict.DENY, command
    assert _verdict(command, ExecutionMode.MANUAL, tmp_path) is Verdict.ASK, command
    assert _verdict(command, ExecutionMode.AUTO, tmp_path) in _auto_gate_verdicts(command), command


def test_auto_asks_for_a_secret_write_that_is_otherwise_ordinary(tmp_path: Path) -> None:
    """The exact gap: the same redirect to an ordinary file is allowed in AUTO, to .env it asks."""
    assert _verdict("echo x > notes.txt", ExecutionMode.AUTO, tmp_path) is Verdict.ALLOW
    assert _verdict("echo x > .env", ExecutionMode.AUTO, tmp_path) is Verdict.ASK


def test_a_secret_write_is_never_covered_by_a_session_grant(tmp_path: Path) -> None:
    """``allow for this task`` must not let a later .env write through."""
    from velune.permissions.gate import Approval, PermissionGate

    async def scenario() -> list[str]:
        answers: list[str] = []

        async def ask(tool_name, decision) -> Approval:
            answers.append(tool_name)
            return Approval.ALLOW_TASK

        gate = PermissionGate(
            state=PolicyState(mode=ExecutionMode.AUTO), boundary=Boundary(tmp_path), ask=ask
        )
        for _ in range(2):
            await gate.check("execute_command", _actions("echo x > .env", tmp_path))
        return answers

    import asyncio

    assert asyncio.run(scenario()) == ["execute_command", "execute_command"]


def test_dd_into_a_secret_file_is_blocked_outright(tmp_path: Path) -> None:
    """The existing safety patterns already refuse ``dd if=`` in every mode (stricter than ask)."""
    for mode in ExecutionMode:
        assert _verdict("dd if=a of=.env", mode, tmp_path) is Verdict.DENY, mode


def test_the_directory_argument_is_honoured(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (action,) = _actions("echo x > .env", tmp_path, directory="app")
    assert action.secret


# ── Harmless commands that merely mention a secret file are not flagged ───────────────────────

NOT_SECRET_WRITES = [
    # reads that name the file as an input
    "pytest --env-file .env",
    "pytest --env-file=.env",
    "git add .env",
    "git diff .env",
    "git log -- .env",
    "git status .env",
    "diff .env .env.example",
    "echo .env",
    'echo ".env"',
    "echo '.env'",
    "ls .env",
    "wc -l .env",
    # copy/move *from* a secret into an ordinary file only reads it (cp) ...
    "cp .env backup.txt",
    "cp .env sub/",
    "cp -t out .env",
    "Copy-Item .env backup.txt",
    # ... redirects whose *target* is ordinary and whose text mentions the file
    'echo ".env" > notes.txt',
    "echo .env > notes.txt",
    "cat .env.example > copy.txt",
    "cat a.txt > b.txt",
    # an operator inside quotes is text, not a redirect
    'echo "x > .env"',
    "echo 'rm .env'",
    "grep 'a>.env' notes.txt",
    # descriptor duplication names no file
    "pytest 2>&1",
    "pytest > out.txt 2>&1",
    # templates are not secrets
    "echo x > .env.example",
    "echo x > .env.sample",
    "cp a.txt .env.template",
    "rm .env.example",
    # an in-place editor without -i only prints
    "sed s/a/b/ .env",
    "perl -ne print .env",
    # unrelated files and commands
    "echo x > notes.txt",
    "echo x >> log.txt",
    "Set-Content notes.txt hi",
    "echo x | Out-File out.txt",
    "tee out.txt",
    "rm old.txt",
    "mv a.txt b.txt",
    "cp a.txt b.txt",
    "git checkout -b feature",
    "git checkout main",
    "git restore notes.txt",
    "sed -i s/a/b/ notes.txt",
    "touch notes.txt",
    "python script.py",
    "npm test",
]


@pytest.mark.parametrize("command", NOT_SECRET_WRITES)
def test_a_command_that_only_mentions_a_secret_file_is_not_flagged_by_the_write_check(
    command: str, tmp_path: Path
) -> None:
    assert not secret_among(command_write_targets(command), Boundary(tmp_path), tmp_path), command


@pytest.mark.parametrize(
    "command",
    [
        "pytest --env-file .env",
        "git add .env",
        "cp .env backup.txt",
        'echo ".env" > notes.txt',
        'echo "x > .env"',
        "echo x > .env.example",
        "echo x > notes.txt",
        "sed s/a/b/ .env",
    ],
)
def test_such_a_command_keeps_its_previous_verdicts(command: str, tmp_path: Path) -> None:
    (action,) = _actions(command, tmp_path)
    assert not action.secret, command
    # AUTO still allows an ordinary change; only a secret write was promoted to "ask".
    if action.changes_state:
        assert _verdict(command, ExecutionMode.AUTO, tmp_path) is Verdict.ALLOW, command
        assert _verdict(command, ExecutionMode.MANUAL, tmp_path) is Verdict.ASK, command
        assert _verdict(command, ExecutionMode.PLAN, tmp_path) is Verdict.DENY, command


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", Verdict.ALLOW),
        ("git log --oneline", Verdict.ALLOW),
        ("ls -la", Verdict.ALLOW),
        ("cat README.md", Verdict.ALLOW),
        ("cat .env.example", Verdict.ALLOW),
        ("echo hello", Verdict.ALLOW),
    ],
)
def test_read_only_commands_are_still_allowed_in_every_mode(
    command: str, expected: Verdict, tmp_path: Path
) -> None:
    for mode in ExecutionMode:
        assert _verdict(command, mode, tmp_path) is expected, (command, mode)


@pytest.mark.parametrize("command", ["cat .env", "grep KEY .env", "type .env", "head .env"])
def test_reading_a_secret_file_still_asks_as_before(command: str, tmp_path: Path) -> None:
    for mode in (ExecutionMode.MANUAL, ExecutionMode.AUTO):
        assert _verdict(command, mode, tmp_path) is Verdict.ASK, (command, mode)


# ── The extractor itself ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("echo x > a.txt", ["a.txt"]),
        ("echo x >>a.txt", ["a.txt"]),
        ("echo x 2>err.txt", ["err.txt"]),
        ("echo x &>all.txt", ["all.txt"]),
        ("echo x >| a.txt", ["a.txt"]),
        ('echo x > "my file.txt"', ["my file.txt"]),
        ("echo x > a.txt > b.txt", ["a.txt", "b.txt"]),
        ("pytest 2>&1", []),
        ("echo x >&2", []),
        ('echo "a > b"', []),
        ("echo 'a > b'", []),
        ("cat a.txt", []),
        ("cp a.txt b.txt", ["b.txt"]),
        ("cp -t dest a.txt", ["dest"]),
        ("cp --target-directory=dest a.txt", ["dest"]),
        ("dd if=a of=b.img", ["b.img"]),
        ("mv a.txt b.txt", ["a.txt", "b.txt"]),
        ("rm a.txt b.txt", ["a.txt", "b.txt"]),
        ("sed -i s/a/b/ f.txt", ["s/a/b/", "f.txt"]),
        ("sed s/a/b/ f.txt", []),
        ("git restore a.txt", ["a.txt"]),
        ("git status", []),
        ("env X=1 tee f.txt", ["f.txt"]),
    ],
)
def test_write_targets(command: str, expected: list[str]) -> None:
    assert command_write_targets(command) == expected


def test_wrapper_nesting_is_bounded() -> None:
    nested = "env " * 50 + "tee .env"
    assert isinstance(command_write_targets(nested), list)  # terminates, never recurses forever


def test_a_relative_secret_target_resolves_against_the_run_directory(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    boundary = Boundary(tmp_path)
    assert secret_among([".env"], boundary, tmp_path / "app") == [
        (tmp_path / "app" / ".env").resolve()
    ]
    assert secret_among(["notes.txt", ".env.example"], boundary, tmp_path) == []
