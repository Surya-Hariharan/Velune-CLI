"""Phase 4E-8 Stage C: ``velune native --plan-review`` through the real CLI entrypoint.

Fake providers only; no network, no credential, no live call. Every secret is a made-up canary.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.test_engine_acting_reliability import CANARY
from tests.test_engine_cli_integration import (
    _invoke,
)
from tests.test_engine_plan_review_session import APPROVE, Timed, good_run
from tests.test_engine_provider_integration import ScriptedProvider, read_call, reply
from velune.core.errors.provider import InferenceError
from velune.orchestration.engine.execcontrol import ExecutionBudget
from velune.orchestration.engine_adapters import native_session

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "a.txt").write_text("alpha contents" + chr(10), encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta contents" + chr(10), encoding="utf-8")
    return tmp_path


ON = {"VELUNE_ORCHESTRATION__ENGINE": "on"}
PR = ["native", "Read a.txt and say what it says", "--provider", "scripted", "--model", "m"]
PLAN = [*PR, "--plan-review"]


def run_pr(monkeypatch, workspace, provider, *, args=None, env=ON, **kw):
    return _invoke(monkeypatch, workspace, args or PLAN, provider=provider, env=env, **kw)


# ── flag and mode selection ───────────────────────────────────────────────────


def test_plan_review_with_the_flag_off_says_so_and_calls_nothing(monkeypatch, workspace):
    provider = good_run()
    result, lookups = run_pr(monkeypatch, workspace, provider, env={})
    assert result.exit_code == 1 and "off" in result.output.lower()
    assert lookups == [] and provider.requests == []


def test_the_plan_review_option_is_not_available_on_other_commands(monkeypatch, workspace):
    for command in ("chat", "ask", "run"):
        result, lookups = _invoke(
            monkeypatch, workspace, [command, "hello", "--plan-review"], provider=good_run(), env=ON
        )
        assert result.exit_code == 2, command
        assert lookups == []


def test_without_the_option_the_single_agent_mode_is_unchanged(monkeypatch, workspace):
    provider = ScriptedProvider(reply("", [read_call()]), reply("It says alpha."))
    result, lookups = run_pr(monkeypatch, workspace, provider, args=PR, tty=True, approve=True)
    assert result.exit_code == 0, result.output
    assert "It says alpha." in result.output and "Plan/review" not in result.output
    assert len(provider.requests) == 2  # one agent, two turns: no planner, no reviewer
    assert lookups == ["scripted"]


def test_with_the_option_the_plan_review_mode_runs_and_says_how_it_was_reviewed(
    monkeypatch, workspace
):
    provider = good_run()
    result, lookups = run_pr(monkeypatch, workspace, provider, tty=True, approve=True)
    assert result.exit_code == 0, result.output
    assert "It says alpha." in result.output
    assert "Plan/review completed" in result.output
    assert "not an independent check" in " ".join(result.output.split())
    assert len(provider.requests) == 4 and lookups == ["scripted"]


def test_the_plan_and_the_critique_are_not_printed(monkeypatch, workspace):
    provider = ScriptedProvider(
        reply("PLAN-MARKER-123 read a.txt"),
        reply("", [read_call()]),
        reply("It says alpha."),
        reply("VERDICT: APPROVE\nCRITIQUE-MARKER-456"),
    )
    result, _ = run_pr(monkeypatch, workspace, provider, tty=True, approve=True)
    assert result.exit_code == 0
    assert "PLAN-MARKER-123" not in result.output and "CRITIQUE-MARKER-456" not in result.output


# ── provider and model ────────────────────────────────────────────────────────


def test_a_missing_model_is_a_usage_error_and_nothing_is_looked_up(monkeypatch, workspace):
    provider = good_run()
    args = ["native", "task", "--provider", "scripted", "--plan-review"]
    from velune.cli import model_prefs

    monkeypatch.setattr(model_prefs, "load_active_model", lambda path=None: None)
    result, lookups = run_pr(monkeypatch, workspace, provider, args=args)
    assert result.exit_code == 2 and "Choose a provider and model" in result.output
    assert lookups == [] and provider.requests == []


def test_a_saved_model_of_another_provider_is_never_paired_with_the_chosen_one(
    monkeypatch, workspace
):
    from velune.cli import model_prefs

    monkeypatch.setattr(
        model_prefs,
        "load_active_model",
        lambda path=None: model_prefs.ModelPref(provider_id="other", model_id="other-model"),
    )
    provider = good_run()
    args = ["native", "task", "--provider", "scripted", "--plan-review"]
    result, lookups = run_pr(monkeypatch, workspace, provider, args=args)
    assert result.exit_code == 2 and lookups == [] and provider.requests == []


def test_an_unavailable_provider_fails_clearly_and_nothing_else_is_tried(monkeypatch, workspace):
    result, lookups = run_pr(monkeypatch, workspace, None)
    assert result.exit_code == 1 and "not available" in result.output
    assert "replaced" in " ".join(result.output.split()) and lookups == ["scripted"]
    assert "Traceback" not in result.output


# ── approval ──────────────────────────────────────────────────────────────────


def test_a_no_ends_the_run_with_a_denied_status_and_exit_one(monkeypatch, workspace):
    provider = good_run()
    result, _ = run_pr(monkeypatch, workspace, provider, tty=True, approve=False)
    assert result.exit_code == 1 and "not approved" in result.output
    assert "Plan/review did not complete" in result.output
    assert len(provider.requests) == 2
    assert all("alpha contents" not in str(r.messages) for r in provider.requests)


def test_the_global_yes_flag_does_not_approve_a_tool_call(monkeypatch, workspace):
    provider = good_run()
    result, _ = run_pr(
        monkeypatch, workspace, provider, args=["--yes", *PLAN], tty=True, approve=False
    )
    assert result.exit_code == 1 and "not approved" in result.output
    assert all("alpha contents" not in str(r.messages) for r in provider.requests)


def test_without_a_terminal_every_tool_call_is_denied_and_the_user_is_told(monkeypatch, workspace):
    provider = good_run()
    result, _ = run_pr(monkeypatch, workspace, provider, tty=False)
    assert result.exit_code == 1 and "No interactive terminal" in result.output
    assert len(provider.requests) == 2


def test_json_mode_never_prompts_and_reports_a_denied_status(monkeypatch, workspace):
    from velune.orchestration.engine_adapters import approval_port

    def must_not_prompt():
        raise AssertionError("JSON mode must not prompt")

    monkeypatch.setattr(approval_port, "confirm_ask", must_not_prompt)
    provider = good_run()
    result, _ = run_pr(monkeypatch, workspace, provider, args=["--json", *PLAN], tty=True)
    assert result.exit_code == 1
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["mode"] == "plan_review" and payload["status"] == "denied"
    assert payload["ok"] is False and payload["answer"] is None
    assert payload["failure_kind"] == "acting_approval_denied"


def test_json_mode_reports_a_completed_run_without_internal_text(monkeypatch, workspace):
    # A task that needs no tool: plan, draft, approve. JSON mode never prompts, so none is needed.
    provider = ScriptedProvider(reply("1. answer"), reply("The answer."), reply(APPROVE))
    result, _ = run_pr(monkeypatch, workspace, provider, args=["--json", *PLAN])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["ok"] is True and payload["status"] == "completed"
    assert payload["answer"] == "The answer." and payload["same_model_review"] is True
    assert payload["model_calls"] == 3 and payload["tool_calls"] == 0
    assert set(payload) >= {"provider", "model", "review_rounds", "provider_attempts"}


# ── cancellation, deadline, budget: distinct results ──────────────────────────


def test_ctrl_c_at_the_prompt_is_a_cancellation_with_exit_130(monkeypatch, workspace):
    from velune.cli.interactive.result import CANCEL

    provider = good_run()
    result, _ = run_pr(monkeypatch, workspace, provider, tty=True, approve=CANCEL)
    assert result.exit_code == 130 and "Cancelled" in result.output
    assert len(provider.requests) == 2 and "Traceback" not in result.output


def test_the_deadline_is_a_failure_with_its_own_message_not_a_cancellation(monkeypatch, workspace):
    provider = Timed((30.0, reply("never")))
    result, _ = run_pr(monkeypatch, workspace, provider, args=[*PLAN, "--timeout", "1"])
    assert result.exit_code == 1 and "time limit" in result.output
    assert "Cancelled" not in result.output and "Traceback" not in result.output
    assert len(provider.requests) == 1


def test_budget_exhaustion_is_a_failure_with_its_own_message(monkeypatch, workspace):
    monkeypatch.setattr(
        native_session,
        "ExecutionBudget",
        lambda *, wall_clock_s: ExecutionBudget(wall_clock_s=wall_clock_s, max_model_calls=1),
    )
    provider = good_run()
    result, _ = run_pr(monkeypatch, workspace, provider, tty=True, approve=True)
    assert result.exit_code == 1 and "request budget" in " ".join(result.output.split())
    assert "Cancelled" not in result.output and len(provider.requests) == 1


def test_the_three_endings_have_three_different_messages(monkeypatch, workspace):
    texts = set()
    deadline = Timed((30.0, reply("never")))
    texts.add(run_pr(monkeypatch, workspace, deadline, args=[*PLAN, "--timeout", "1"])[0].output)
    denied = run_pr(monkeypatch, workspace, good_run(), tty=True, approve=False)[0].output
    texts.add(denied)
    assert len(texts) == 2 and "time limit" in "".join(texts) and "not approved" in "".join(texts)


# ── provider errors and malformed responses are sanitized ─────────────────────


def test_a_provider_error_is_a_fixed_line_with_no_secret_or_traceback(monkeypatch, workspace):
    provider = ScriptedProvider(InferenceError("500 internal " + CANARY))
    result, _ = run_pr(monkeypatch, workspace, provider)
    assert result.exit_code == 1 and "Plan/review did not complete" in result.output
    assert CANARY not in result.output and "Traceback" not in result.output
    assert len(provider.requests) == 1  # no retry loop of the CLI's own, no fallback


@pytest.mark.parametrize(
    "script",
    [
        (reply(""),),  # an empty plan
        (reply("1. plan"), reply("a draft"), reply("no verdict line here")),
        (reply("1. plan"), reply("a draft"), reply(f"VERDICT: ???\n{CANARY}")),
    ],
)
def test_empty_or_malformed_model_output_fails_the_run_cleanly(monkeypatch, workspace, script):
    result, _ = run_pr(monkeypatch, workspace, ScriptedProvider(*script))
    assert result.exit_code == 1 and "Plan/review did not complete" in result.output
    assert CANARY not in result.output and "Traceback" not in result.output


def test_an_unforeseen_error_is_one_fixed_line(monkeypatch, workspace):
    async def boom(*args, **kwargs):
        raise RuntimeError(f"internal detail {CANARY}")

    monkeypatch.setattr(native_session, "run_native_plan_review", boom)
    result, _ = run_pr(monkeypatch, workspace, good_run())
    assert result.exit_code == 1 and "failed unexpectedly" in result.output
    assert CANARY not in result.output and "Traceback" not in result.output


# ── tool permissions ──────────────────────────────────────────────────────────


def test_a_file_outside_the_workspace_is_not_read_even_when_approved(monkeypatch, workspace):
    outside = workspace.parent / "outside_secret.txt"
    outside.write_text("OUTSIDE-CONTENT", encoding="utf-8")
    provider = ScriptedProvider(
        reply("1. read it"),
        reply("", [read_call("../outside_secret.txt")]),
        reply("could not read"),
        reply(APPROVE),
    )
    result, _ = run_pr(monkeypatch, workspace, provider, tty=True, approve=True)
    assert "OUTSIDE-CONTENT" not in result.output
    assert all("OUTSIDE-CONTENT" not in str(r.messages) for r in provider.requests)


def test_a_tool_other_than_read_file_is_never_offered_or_run(monkeypatch, workspace):
    provider = ScriptedProvider(
        reply("1. plan"),
        reply("", [read_call("a.txt")]),
        reply("done"),
        reply(APPROVE),
    )
    run_pr(monkeypatch, workspace, provider, tty=True, approve=True)
    offered = {t["function"]["name"] for r in provider.requests for t in (r.tools or [])}
    assert offered == {"read_file"}


# ── termination: nothing more happens ─────────────────────────────────────────


def test_no_provider_request_is_made_after_the_run_ends(monkeypatch, workspace):
    provider = good_run()
    result, _ = run_pr(monkeypatch, workspace, provider, tty=True, approve=False)
    assert result.exit_code == 1
    count = len(provider.requests)
    import time

    time.sleep(0.2)
    assert len(provider.requests) == count == 2


# ── isolation ─────────────────────────────────────────────────────────────────


def _loaded(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=120
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


NATIVE_MODULES = (
    "velune.orchestration.engine_adapters.native_session",
    "velune.orchestration.engine_adapters.reasoning_invoker",
    "velune.orchestration.engine.limits",
    "velune.orchestration.engine.driver",
    "velune.orchestration.engine_adapters.acting_stack",
)


def test_chat_ask_run_and_the_council_never_load_the_native_modules():
    out = _loaded(
        "import sys\n"
        "import velune.cli.commands.chat, velune.cli.commands.ask, velune.cli.commands.run\n"
        "import velune.cognition.orchestrator\n"
        f"bad = {NATIVE_MODULES!r}\n"
        "print('LOADED', sorted(m for m in bad if m in sys.modules))\n"
    )
    assert out == "LOADED []", out


def test_importing_the_native_command_does_not_load_the_engine_until_it_runs():
    out = _loaded(
        "import sys\n"
        "import velune.cli.commands.native\n"
        f"bad = {NATIVE_MODULES!r}\n"
        "print('LOADED', sorted(m for m in bad if m in sys.modules))\n"
    )
    assert out == "LOADED []", out


def test_only_the_native_command_and_the_session_use_the_plan_review_pieces():
    allowed = {
        "velune/cli/commands/native.py",
        "velune/orchestration/engine_adapters/native_session.py",
        "velune/orchestration/engine_adapters/reasoning_invoker.py",
        "velune/orchestration/engine/limits.py",
        "velune/orchestration/engine/native_task.py",
    }
    offenders = []
    for path in (ROOT / "velune").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in allowed or "/orchestration/engine/" in rel:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "run_native_plan_review" in text or "ProviderReasoningInvoker" in text:
            offenders.append(rel)
    assert offenders == []


def test_the_default_of_the_flag_is_still_off():
    from velune.kernel.config import VeluneConfig

    assert VeluneConfig().orchestration.engine == "off"


def test_the_option_has_a_default_of_false_in_the_command_signature():
    import inspect

    from velune.cli.commands.native import native_command

    param = inspect.signature(native_command).parameters["plan_review"]
    assert param.default.default is False
    assert "--plan-review" in param.default.param_decls
    assert CliRunner is not None
