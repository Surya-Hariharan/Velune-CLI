"""The agent manipulates a real repository through its own execution loop.

Everything here is production code: the registry the REPL builds
(velune/tools/subsystems.py), the permission gate, ``authorize_and_execute``
and :class:`ToolLoopRunner`. Only the model is scripted — each turn decides its
next call from the tool results the loop fed back, the way a real model does.

The workflow: inspect → create → write → read → edit (a wrong edit first, whose
error goes back to the model, then the corrected one) → verify → move → verify
→ delete → verify → summary. Assertions are about the files on disk.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests._fs_registry import gated_ctx, production_registry
from velune.core.types.inference import InferenceRequest, InferenceResponse, ToolCall
from velune.orchestration.tool_loop import ToolLoopRunner
from velune.permissions import ExecutionMode
from velune.permissions.gate import Approval
from velune.tools.filesystem import mutation as mut

GREET = 'def hello():\n    return "hi"\n\n\ndef bye():\n    return "hi"\n'

Step = Callable[[str], "list[ToolCall] | str"]


class ScriptedModel:
    """Plays a list of steps; each sees the previous tool result (or results)."""

    def __init__(self, steps: list[Step]) -> None:
        self._steps = steps
        self.requests: list[InferenceRequest] = []

    def get_capabilities(self):
        from types import SimpleNamespace

        return SimpleNamespace(supports_function_calling=True, supports_streaming=False)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.requests.append(request)
        tool_results = []
        for message in reversed(request.messages):
            if message["role"] != "tool":
                break
            tool_results.insert(0, message["content"])
        step = self._steps[len(self.requests) - 1]
        outcome = step("\n".join(tool_results))
        if isinstance(outcome, str):
            return InferenceResponse(
                content=outcome,
                model_id="scripted",
                finish_reason="stop",
                tokens_used=1,
                latency_ms=0.1,
            )
        return InferenceResponse(
            content="",
            model_id="scripted",
            finish_reason="tool_calls",
            tokens_used=1,
            latency_ms=0.1,
            tool_calls=outcome,
        )


def call(name: str, **arguments: Any) -> list[ToolCall]:
    return [
        ToolCall(
            id=f"{name}-{json.dumps(arguments, sort_keys=True)[:40]}",
            name=name,
            arguments=arguments,
        )
    ]


def _workflow() -> list[Step]:
    def inspect(_: str):
        return call("read_directory", directory_path=".")

    def create(listing: str):
        assert "legacy.py" in listing and "README.md" in listing, listing
        return call("create_file", file_path="pkg/__init__.py")

    def write(result: str):
        assert result.startswith("create_file OK pkg/__init__.py"), result
        return call("write_file", file_path="pkg/greet.py", content=GREET)

    def read(result: str):
        assert result.startswith("write_file OK pkg/greet.py"), result
        return call("read_file", file_path="pkg/greet.py")

    def bad_edit(content: str):
        assert content == GREET, content
        # Ambiguous: `return "hi"` appears twice. The tool must refuse, not guess.
        return call(
            "edit_file",
            file_path="pkg/greet.py",
            old_string='return "hi"',
            new_string='return "bye"',
        )

    def good_edit(error: str):
        assert error.startswith("Error:"), error
        assert "matches 2 places (lines 2, 6)" in error, error
        return call(
            "edit_file",
            file_path="pkg/greet.py",
            old_string='def bye():\n    return "hi"',
            new_string='def bye():\n    return "bye"',
        )

    def verify_edit(result: str):
        assert result.startswith("edit_file OK pkg/greet.py (+1 -1) verified"), result
        assert '+    return "bye"' in result, result  # the diff came back to the model
        return call("read_file", file_path="pkg/greet.py")

    def move(content: str):
        assert 'def bye():\n    return "bye"' in content and content.count('"hi"') == 1
        return call("move_path", source="pkg/greet.py", destination="src/app/greeting.py")

    def verify_move(result: str):
        assert result.startswith("Moved") and "(verified)" in result, result
        return call("find_files", pattern="greeting.py")

    def delete(found: str):
        assert "greeting.py" in found, found
        return call("delete_file", file_path="legacy.py")

    def verify_delete(result: str):
        assert result.startswith("delete_file OK legacy.py"), result
        return call("read_directory", directory_path=".")

    def summarize(listing: str):
        assert "legacy.py" not in listing, listing
        return "Created pkg/, wrote and fixed greet.py, moved it to src/app/, removed legacy.py."

    return [
        inspect,
        create,
        write,
        read,
        bad_edit,
        good_edit,
        verify_edit,
        move,
        verify_move,
        delete,
        verify_delete,
        summarize,
    ]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_bytes(b"# demo\r\n")
    (root / "legacy.py").write_bytes(b"OLD = True\n")
    return root


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


async def _run(repo: Path, mode: ExecutionMode, ask=None, journal: mut.TurnJournal | None = None):
    model = ScriptedModel(_workflow())
    runner = ToolLoopRunner(
        model,  # type: ignore[arg-type]
        production_registry(repo),
        approver=None,  # production-equivalent: local tools are decided by the gate
        ctx=gated_ctx(repo, mode, ask=ask),
        max_turns=20,
    )

    # The REPL's approver returns True for local tools and lets the gate decide.
    async def gate_decides(name, arguments, permissions):
        return True

    runner._approver = gate_decides
    token = mut.set_active_journal(journal) if journal is not None else None
    try:
        result = await runner.run(
            InferenceRequest(
                model_id="scripted",
                messages=[{"role": "user", "content": "Restructure this repo."}],
            )
        )
    finally:
        if token is not None:
            mut.reset_active_journal(token)
    return model, result


EXPECTED_TREE = {
    "README.md": b"# demo\r\n",
    "pkg/__init__.py": b"",
    "src/app/greeting.py": GREET.replace(
        'def bye():\n    return "hi"', 'def bye():\n    return "bye"'
    ).encode(),
}


async def test_agent_completes_a_multi_step_repository_change_in_auto_mode(repo):
    model, result = await _run(repo, ExecutionMode.AUTO)

    assert result.stop_reason == "completed", result
    assert result.content.startswith("Created pkg/")
    names = [inv.call.name for inv in result.invocations]
    assert names == [
        "read_directory",
        "create_file",
        "write_file",
        "read_file",
        "edit_file",
        "edit_file",
        "read_file",
        "move_path",
        "find_files",
        "delete_file",
        "read_directory",
    ]
    errors = [inv.error for inv in result.invocations]
    assert errors == [False, False, False, False, True, False, False, False, False, False, False]
    # The failed edit was reported to the model as an error tool result.
    bad = model.requests[5].messages[-1]
    assert bad["role"] == "tool" and bad.get("is_error") is True

    assert _tree(repo) == EXPECTED_TREE
    assert not (repo / "pkg" / "greet.py").exists()
    assert not list(repo.rglob(".velune-tmp-*"))


async def test_manual_mode_asks_for_each_change_and_then_applies_it(repo):
    prompts: list[str] = []

    async def approve(tool_name, decision):
        prompts.append(tool_name)
        return Approval.ALLOW_ONCE

    _, result = await _run(repo, ExecutionMode.MANUAL, ask=approve)
    assert result.stop_reason == "completed"
    # Reads never ask; every change does (the rejected ambiguous edit is asked too:
    # approval happens before the tool can tell the edit is ambiguous).
    assert prompts == [
        "create_file",
        "write_file",
        "edit_file",
        "edit_file",
        "move_path",
        "delete_file",
    ]
    assert _tree(repo) == EXPECTED_TREE


async def test_manual_denial_reaches_the_model_and_changes_nothing(repo):
    async def deny(tool_name, decision):
        return Approval.DENY

    model = ScriptedModel(
        [
            lambda _: call("write_file", file_path="new.py", content="x = 1\n"),
            lambda result: (
                "The user declined." if "the user rejected it" in result else f"bad: {result}"
            ),
        ]
    )
    runner = ToolLoopRunner(
        model,
        production_registry(repo),  # type: ignore[arg-type]
        ctx=gated_ctx(repo, ExecutionMode.MANUAL, ask=deny),
    )

    async def gate_decides(name, arguments, permissions):
        return True

    runner._approver = gate_decides
    result = await runner.run(InferenceRequest(model_id="s", messages=[]))
    assert result.content == "The user declined."
    assert not (repo / "new.py").exists()


async def test_the_whole_turn_can_be_rewound(repo):
    before = _tree(repo)
    journal = mut.TurnJournal()
    await _run(repo, ExecutionMode.AUTO, journal=journal)
    assert _tree(repo) == EXPECTED_TREE

    report = journal.rewind()
    assert report.skipped == []
    assert _tree(repo) == before
    assert not (repo / "pkg").exists()


async def test_a_concurrent_user_edit_is_caught_and_the_agent_recovers(repo):
    (repo / "conf.py").write_bytes(b"DEBUG = False\n")

    def read(_: str):
        return call("read_file", file_path="conf.py")

    def edit_after_user_change(content: str):
        assert content == "DEBUG = False\n"
        # The user saves the file in their editor while the agent is thinking.
        (repo / "conf.py").write_bytes(b"DEBUG = False\nPORT = 8080\n")
        return call(
            "edit_file", file_path="conf.py", old_string="DEBUG = False", new_string="DEBUG = True"
        )

    def reread(error: str):
        assert "Conflict" in error and "changed on disk" in error, error
        return call("read_file", file_path="conf.py")

    def retry(content: str):
        assert "PORT = 8080" in content
        return call(
            "edit_file", file_path="conf.py", old_string="DEBUG = False", new_string="DEBUG = True"
        )

    def done(result: str):
        assert result.startswith("edit_file OK"), result
        return "done"

    model = ScriptedModel([read, edit_after_user_change, reread, retry, done])
    runner = ToolLoopRunner(
        model,
        production_registry(repo),  # type: ignore[arg-type]
        ctx=gated_ctx(repo),
        max_turns=10,
    )

    async def gate_decides(name, arguments, permissions):
        return True

    runner._approver = gate_decides
    result = await runner.run(InferenceRequest(model_id="s", messages=[]))
    assert result.content == "done"
    assert (repo / "conf.py").read_bytes() == b"DEBUG = True\nPORT = 8080\n"
