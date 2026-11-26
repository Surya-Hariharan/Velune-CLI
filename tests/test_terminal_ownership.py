"""The full-screen REPL must be the only thing reading the keyboard.

A raw stdin read (``Prompt.ask``, ``Confirm.ask``, ``console.input``) while the
prompt_toolkit app runs makes two readers share one terminal: the UI freezes or
the prompt bar is pushed out of place. The same goes for a child process that
inherits the terminal's stdin.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from velune.cli import interactive
from velune.execution.sandbox import SubprocessSandbox
from velune.tools.safety import ApprovalMode
from velune.tools.terminal.execute import ExecuteCommand

ROOT = Path(__file__).resolve().parent.parent / "velune"

# Code that runs while the REPL owns the terminal.
REPL_CODE = [
    ROOT / "cli" / "repl.py",
    *(ROOT / "cli" / "handlers").glob("*.py"),
]
PROMPT_CLASSES = {"Prompt", "Confirm", "IntPrompt", "FloatPrompt"}


def _raw_reads(source: str) -> list[int]:
    """Line numbers of calls that read stdin behind the UI's back."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "input":
            lines.append(node.lineno)
        elif isinstance(func, ast.Attribute):
            owner = func.value
            if func.attr == "ask" and isinstance(owner, ast.Name) and owner.id in PROMPT_CLASSES:
                lines.append(node.lineno)
            elif func.attr == "input" and ast.unparse(owner).endswith("console"):
                lines.append(node.lineno)
            elif (
                func.attr == "to_thread"
                and node.args
                and ast.unparse(node.args[0]).endswith(".ask")
            ):
                lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("path", REPL_CODE, ids=lambda p: p.name)
def test_repl_code_never_reads_stdin_directly(path):
    hits = _raw_reads(path.read_text(encoding="utf-8"))
    assert hits == [], f"{path.name} lines {hits}: use velune.cli.interactive.ask/confirm"


def test_the_guard_catches_each_raw_read():
    code = (
        "Prompt.ask('a')\nConfirm.ask('b')\nrepl.console.input('c')\ninput('d')\n"
        "asyncio.to_thread(Prompt.ask, 'e')\n"
    )
    assert _raw_reads(code) == [1, 2, 3, 4, 5]


async def test_ask_returns_none_when_cancelled(monkeypatch):
    async def cancelled(*args, **kwargs):
        return interactive.CANCEL

    monkeypatch.setattr(interactive, "text_input", cancelled)
    assert await interactive.ask("Name") is None


async def test_ask_returns_the_text(monkeypatch):
    async def typed(*args, **kwargs):
        return "hello"

    monkeypatch.setattr(interactive, "text_input", typed)
    assert await interactive.ask("Name") == "hello"


@pytest.mark.usefixtures("interpreter_under_test_on_path")
@pytest.mark.timeout(20)
async def test_a_command_run_by_a_tool_gets_no_keyboard(tmp_path):
    (tmp_path / "reads.py").write_text(
        "import sys\nprint('EOF' if sys.stdin.read() == '' else 'GOT INPUT')\n", encoding="utf-8"
    )
    tool = ExecuteCommand(
        sandbox=SubprocessSandbox(tmp_path),
        workspace_path=str(tmp_path),
        approval_mode=ApprovalMode.SAFE,
    )
    result = await tool.execute("python reads.py", directory=str(tmp_path))
    assert result["exit_code"] == 0
    assert "EOF" in result["stdout"]
