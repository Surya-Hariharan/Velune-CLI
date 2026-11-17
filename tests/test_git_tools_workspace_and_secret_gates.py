"""Git tools act on the opened workspace, report it truthfully, and never commit secrets unasked.

Regression tests for the v0.9.9 acceptance findings:

* the git tools resolved ``directory`` against the process working directory, so
  ``velune --workspace X`` started from somewhere else broke every one of them (and a start
  from a subfolder silently returned empty blame/diff results);
* ``git_status`` cut the first character off the first path and reported a git failure as a
  clean tree;
* ``git_commit`` staged everything, credential files included, with no question asked;
* ``git -c core.fsmonitor=CMD ...`` / ``git config core.sshCommand CMD`` ran a program that
  is not on the executable allowlist, unprompted in AUTO;
* ``git grep --no-index`` and ``grep_files`` printed credential files ``read_file`` asks about.

Every repository here is a throwaway one under ``tmp_path``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests._fs_registry import gated_ctx, production_registry
from velune.permissions import ExecutionMode
from velune.permissions.boundary import _SECRET_DIR_NAMES, _SECRET_NAME_PATTERNS, Boundary
from velune.permissions.commands import CommandClass, classify
from velune.permissions.gate import ActionDeniedError, Approval
from velune.permissions.policy import PolicyState, Verdict, authorize
from velune.tools.base.tool import authorize_and_execute
from velune.tools.filesystem.ignore import DEFAULT_IGNORE_RULES
from velune.tools.git.operations import command_stages_secrets, stageable_secret_files


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return done.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "sub" / "x.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


async def _call(repo: Path, name: str, mode: ExecutionMode = ExecutionMode.AUTO, ask=None, **kw):
    registry = production_registry(repo)
    tool = registry.get(name)
    ctx = gated_ctx(repo, mode, ask=ask)
    ctx.permissions = set(tool.get_required_permissions())
    return await authorize_and_execute(tool, ctx, **kw)


# ── the workspace, not the process working directory ────────────────────────────


@pytest.mark.parametrize("where", ["outside", "subfolder"])
async def test_git_tools_use_the_workspace_whatever_the_process_cwd_is(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere if where == "outside" else repo / "sub")
    (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")

    status = await _call(repo, "git_status")
    assert status["modified"] == ["app.py"]
    assert "print('changed')" in await _call(repo, "git_diff")
    assert "print('changed')" in await _call(repo, "git_diff", file_path="app.py")
    assert [c["message"] for c in await _call(repo, "git_log", limit=5)] == ["init"]
    assert (await _call(repo, "git_branch"))["current"] == "main"
    blame = await _call(repo, "git_blame", file_path="app.py")
    assert [line["content"] for line in blame] == ["print('hi')"]

    await _call(repo, "git_checkout", branch="feat/x", create=True)
    assert _git(repo, "branch", "--show-current").strip() == "feat/x"
    await _call(repo, "git_commit", message="change")
    assert _git(repo, "log", "-1", "--format=%s").strip() == "change"
    assert not (elsewhere / ".git").exists()


async def test_git_tools_still_refuse_a_directory_outside_the_workspace(
    repo: Path, tmp_path: Path
) -> None:
    for directory in ("..", str(tmp_path)):
        with pytest.raises(ValueError, match="outside workspace"):
            await _call(repo, "git_status", directory=directory)
        with pytest.raises(ValueError, match="outside workspace"):
            await _call(repo, "git_commit", message="m", directory=directory)


# ── git_status tells the truth ───────────────────────────────────────────────────


async def test_git_status_keeps_the_whole_path_of_an_unstaged_change(repo: Path) -> None:
    (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")  # " M app.py"
    (repo / "new file.txt").write_text("n\n", encoding="utf-8")
    (repo / "staged.py").write_text("s = 1\n", encoding="utf-8")
    _git(repo, "add", "staged.py")
    (repo / "sub" / "x.py").unlink()

    status = await _call(repo, "git_status")
    assert status == {
        "modified": ["app.py"],
        "added": ["staged.py"],
        "deleted": ["sub/x.py"],
        "untracked": ["new file.txt"],
    }


async def test_git_status_reports_a_rename_by_its_new_name(repo: Path) -> None:
    _git(repo, "mv", "app.py", "main.py")
    assert (await _call(repo, "git_status"))["modified"] == ["main.py"]


async def test_git_status_raises_instead_of_reporting_a_clean_tree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from velune.tools.git import state

    real = state._git_run

    def failing(cwd: Path, *args: str, **kw):
        if args[:1] == ("status",):
            raise RuntimeError("Git error: index file corrupt")
        return real(cwd, *args, **kw)

    monkeypatch.setattr(state, "_git_run", failing)
    with pytest.raises(RuntimeError, match="index file corrupt"):
        await _call(repo, "git_status")


# ── commits never pick up a credentials file without a question ──────────────────


def _add_secrets(repo: Path) -> None:
    (repo / ".env").write_text("TOKEN=plain-secret\n", encoding="utf-8")
    (repo / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")


def test_stageable_secret_files_lists_only_unignored_credential_files(repo: Path) -> None:
    assert stageable_secret_files(repo) == []
    _add_secrets(repo)
    (repo / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
    assert sorted(stageable_secret_files(repo)) == [".env", "id_rsa"]
    (repo / ".gitignore").write_text(".env\nid_rsa\n", encoding="utf-8")
    assert stageable_secret_files(repo) == []


@pytest.mark.parametrize("mode", [ExecutionMode.AUTO, ExecutionMode.MANUAL])
async def test_git_commit_asks_before_staging_a_credentials_file(
    repo: Path, mode: ExecutionMode
) -> None:
    _add_secrets(repo)
    prompts: list[str] = []

    async def deny(tool: str, decision) -> Approval:
        prompts.append(" | ".join(a.reason for a in decision.actions))
        return Approval.DENY

    with pytest.raises(ActionDeniedError):
        await _call(repo, "git_commit", mode, ask=deny, message="feat")
    assert len(prompts) == 1 and ".env" in prompts[0] and "id_rsa" in prompts[0]
    assert _git(repo, "log", "-1", "--format=%s").strip() == "init"
    assert _git(repo, "diff", "--cached", "--name-only").strip() == ""


async def test_git_commit_never_reaches_a_secret_with_a_session_grant(repo: Path) -> None:
    """ "Always allow git_commit" must not cover a commit that adds a credentials file."""
    _add_secrets(repo)
    ctx = gated_ctx(repo, ExecutionMode.MANUAL)
    ctx.gate.session_grants.add("git_commit")
    tool = production_registry(repo).get("git_commit")
    ctx.permissions = set(tool.get_required_permissions())
    with pytest.raises(ActionDeniedError, match="no one can be asked"):
        await authorize_and_execute(tool, ctx, message="feat")


async def test_git_commit_without_secrets_is_an_ordinary_change(repo: Path) -> None:
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=plain-secret\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    await _call(repo, "git_commit", message="feat")  # AUTO, nobody to ask: must not need to
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(committed) == [".gitignore", "feature.py"]


async def test_git_commit_of_what_is_already_staged_does_not_ask(repo: Path) -> None:
    _add_secrets(repo)
    _git(repo, "add", "feature.py")
    await _call(repo, "git_commit", message="feat", add_all=False)
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["feature.py"]


@pytest.mark.parametrize(
    ("command", "stages"),
    [
        ("git add .", True),
        ("git add -A", True),
        ("git add -u", True),  # a broad form: judged by what the repository would stage
        ("git commit -am msg", True),
        ("git commit --all -m msg", True),
        ("git add feature.py", False),
        ("git add .env", True),
        ("git commit -m msg", False),
        ("git status", False),
        ("python feature.py", False),
    ],
)
def test_shell_git_add_is_judged_like_the_commit_tool(
    repo: Path, command: str, stages: bool
) -> None:
    _add_secrets(repo)
    found = command_stages_secrets(command, repo)
    assert bool(found) is stages, found


async def test_shell_git_add_asks_in_auto_when_it_would_stage_a_secret(repo: Path) -> None:
    _add_secrets(repo)
    tool = production_registry(repo).get("execute_command")
    boundary = Boundary(repo)
    state = PolicyState(mode=ExecutionMode.AUTO)
    risky = authorize(tool.describe_actions({"command": "git add ."}, boundary), state)
    assert risky.verdict is Verdict.ASK and "credentials" in risky.reason
    fine = authorize(tool.describe_actions({"command": "git add feature.py"}, boundary), state)
    assert fine.verdict is Verdict.ALLOW


# ── git must not be a way to run a program that is not on the allowlist ──────────


@pytest.mark.parametrize(
    "command",
    [
        'git -c "core.fsmonitor=whoami > f.txt" status',
        "git -c core.sshCommand=evil fetch origin",
        "git -c core.pager=evil -p log -1",
        "git -c credential.helper=!evil fetch",
        "git -c credential.https://x.helper=evil fetch",
        "git -c diff.external=evil diff",
        "git -c core.hooksPath=/tmp/h commit -m x",
        "git -c url.https://evil/.insteadOf=https://github.com/ fetch",
        "git -c protocol.ext.allow=always clone ext::sh",
        "git --config-env=core.fsmonitor=EVIL status",
        "git -C sub -c CORE.FSMONITOR=evil status",
        "git config core.fsmonitor evil",
        "git config --local core.sshCommand evil",
        "git config --add core.hooksPath hooks",
        "git config set core.pager evil",
        "git config remote.origin.url https://evil.invalid/x.git",
        "git config filter.x.clean evil",
        "git config --global user.name x",
        "git config --system core.autocrlf false",
        "git config -f other.cfg user.name x",
        "git config --edit",
        "git grep --no-index API_KEY",
        "git grep --no-ind API_KEY",
        "git grep --untracked API_KEY",
    ],
)
def test_git_settings_that_run_a_program_are_high_risk(command: str) -> None:
    klass, why = classify(command)
    assert klass is CommandClass.HIGH_RISK, (command, klass, why)


@pytest.mark.parametrize(
    "command",
    [
        "git -c user.name=me commit -m x",
        "git -c color.ui=never diff",
        "git -c core.quotepath=off status",
        "git config user.name",
        "git config --get core.fsmonitor",
        "git config --list",
        "git config list",
        "git config get remote.origin.url",
        "git config user.email me@example.com",
        "git config --local core.autocrlf false",
        "git grep API_KEY",
        "git fetch origin",
        "git pull",
        "git commit -m x",
    ],
)
def test_ordinary_git_commands_are_not_made_high_risk(command: str) -> None:
    klass, why = classify(command)
    assert klass is not CommandClass.HIGH_RISK, (command, klass, why)
    assert klass is not CommandClass.BLOCKED


@pytest.mark.parametrize("mode", [ExecutionMode.AUTO, ExecutionMode.MANUAL, ExecutionMode.PLAN])
async def test_fsmonitor_command_never_runs_without_approval(
    repo: Path, mode: ExecutionMode
) -> None:
    command = 'git -c "core.fsmonitor=git init planted" status'
    with pytest.raises(ActionDeniedError):
        await _call(repo, "execute_command", mode, command=command)
    assert not (repo / "planted").exists()


# ── one list of secret files, for reading and for searching ──────────────────────


def test_every_file_read_file_asks_about_is_hidden_from_search() -> None:
    hidden = {pattern for pattern, _ in DEFAULT_IGNORE_RULES}
    missing = [p for p in _SECRET_NAME_PATTERNS if p not in hidden]
    missing += [d for d in _SECRET_DIR_NAMES if f"{d}/" not in hidden]
    assert not missing, f"read gate covers these but search does not: {missing}"


async def test_grep_and_find_do_not_expose_credential_files(repo: Path) -> None:
    secret_files = {
        "id_rsa": "ZZ-id-rsa",
        "id_ed25519": "ZZ-ed",
        ".npmrc": "//registry/:_authToken=ZZ-npm",
        ".pypirc": "password = ZZ-pypi",
        ".netrc": "password ZZ-netrc",
        ".git-credentials": "https://u:ZZ-gitcred@example.com",
        ".aws/credentials": "aws_secret_access_key=ZZ-aws",
        ".ssh/config": "IdentityFile ZZ-ssh",
        ".env": "ZZ-env",
        ".env.production": "ZZ-envp",
        "server.key": "ZZ-key",
        "secrets.json": "ZZ-secjson",
    }
    for rel, content in secret_files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n", encoding="utf-8")
    (repo / "notes.txt").write_text("ZZ-ordinary\n", encoding="utf-8")

    result = await _call(repo, "grep_files", pattern="ZZ-")
    assert "ZZ-ordinary" in str(result)
    for content in secret_files.values():
        marker = content[content.index("ZZ-") :]
        assert marker not in str(result), marker
    found = await _call(repo, "find_files", pattern="*")
    names = {Path(p).name for p in found}
    assert "notes.txt" in names
    assert not names & {"id_rsa", ".npmrc", ".git-credentials", "credentials", ".env"}


async def test_read_file_asks_about_git_credentials(repo: Path) -> None:
    (repo / ".git-credentials").write_text("https://u:p@example.com\n", encoding="utf-8")
    with pytest.raises(ActionDeniedError, match="no one can be asked"):
        await _call(repo, "read_file", file_path=".git-credentials")


# ── provider tools ───────────────────────────────────────────────────────────────


async def test_git_push_tool_refuses_an_option_as_the_remote(repo: Path) -> None:
    from velune.tools.git.providers import GitPushTool

    with pytest.raises(ValueError, match="Invalid remote name"):
        await GitPushTool(workspace=repo).execute(remote="--receive-pack=evil")


def test_github_403_with_a_non_json_body_is_a_clean_error() -> None:
    import httpx

    from velune.integrations.base import AuthenticationError
    from velune.integrations.github import GitHubProvider

    response = httpx.Response(
        403, content=b"<html>blocked</html>", request=httpx.Request("GET", "https://x.invalid/")
    )
    with pytest.raises(AuthenticationError, match="Forbidden"):
        GitHubProvider(token="t")._handle(response)
