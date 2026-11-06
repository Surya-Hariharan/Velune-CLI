"""Phase 4E-6: the opt-in ``velune native`` command. Fake providers and fake prompts only.

No network, no real credential, no live provider. Every secret here is a made-up canary.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.test_engine_acting_reliability import CANARY
from tests.test_engine_provider_integration import ScriptedProvider, read_call, reply
from velune.core.errors.provider import InferenceError
from velune.orchestration.engine.errors import EngineDisabledError
from velune.orchestration.engine_adapters import approval_port
from velune.orchestration.engine_adapters.approval_port import ApprovalPrompt
from velune.orchestration.engine_adapters.native_session import (
    NativeSessionError,
    run_native_task,
    safe_ask,
    sanitize_text,
)
from velune.orchestration.engine_adapters.secrets import SecretScrubber, static_source

ROOT = Path(__file__).resolve().parents[1]
ON = SimpleNamespace(orchestration=SimpleNamespace(engine="on"))
OFF = SimpleNamespace(orchestration=SimpleNamespace(engine="off"))


class Providers:
    """Stands in for the provider registry; records every lookup so a fallback would be visible."""

    def __init__(self, provider: Any = None) -> None:
        self.provider = provider
        self.lookups: list[str] = []

    def get_or_raise(self, name: str) -> Any:
        self.lookups.append(name)
        if self.provider is None:
            raise KeyError(f"no provider {name} {CANARY}")
        return self.provider


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "a.txt").write_text("alpha contents\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta contents\n", encoding="utf-8")
    return tmp_path


def asker(verdict: Any, seen: list | None = None):
    async def ask(prompt: ApprovalPrompt) -> Any:
        if seen is not None:
            seen.append(prompt)
        if isinstance(verdict, BaseException):
            raise verdict
        return verdict

    return ask


async def go(workspace: Path, provider: Any, ask: Any, *, config: Any = ON, **kw: Any):
    providers = kw.pop("providers", None) or Providers(provider)
    return await run_native_task(
        "Read a.txt and tell me what it says.",
        config=config,
        providers=providers,
        provider_id=kw.pop("provider_id", "scripted"),
        model_id=kw.pop("model_id", "some-model"),
        workspace=workspace,
        ask=ask,
        **kw,
    )


def read_then_answer(path: str = "a.txt") -> ScriptedProvider:
    return ScriptedProvider(reply("", [read_call(path)]), reply("It says alpha.", stop="stop"))


# ── the flag ──────────────────────────────────────────────────────────────────


async def test_a_disabled_flag_builds_and_calls_nothing(workspace):
    providers = Providers(ScriptedProvider())
    with pytest.raises(EngineDisabledError):
        await go(workspace, None, asker(True), config=OFF, providers=providers)
    assert providers.lookups == [] and providers.provider.requests == []


async def test_a_config_without_the_section_is_off(workspace):
    with pytest.raises(EngineDisabledError):
        await go(workspace, ScriptedProvider(), asker(True), config=SimpleNamespace())


# ── explicit provider and model, no fallback ──────────────────────────────────


async def test_an_unavailable_provider_is_an_error_not_a_different_provider(workspace):
    providers = Providers(None)
    with pytest.raises(NativeSessionError) as info:
        await go(workspace, None, asker(True), providers=providers, provider_id="groq")
    assert providers.lookups == ["groq"]  # asked for once, by the chosen id only
    assert CANARY not in str(info.value) and info.value.__cause__ is None


@pytest.mark.parametrize("field", ["provider_id", "model_id"])
async def test_a_blank_provider_or_model_is_refused(workspace, field):
    provider = ScriptedProvider()
    with pytest.raises(NativeSessionError):
        await go(workspace, provider, asker(True), **{field: "  "})
    assert provider.requests == []


@pytest.mark.parametrize("task", ["", "   ", "x" * 5000])
async def test_an_unusable_task_is_refused_before_any_lookup(workspace, task):
    providers = Providers(ScriptedProvider())
    with pytest.raises(NativeSessionError):
        await run_native_task(
            task,
            config=ON,
            providers=providers,
            provider_id="p",
            model_id="m",
            workspace=workspace,
            ask=asker(True),
        )
    assert providers.lookups == []


# ── approval ──────────────────────────────────────────────────────────────────


async def test_an_explicit_yes_runs_exactly_that_call(workspace):
    seen: list[ApprovalPrompt] = []
    provider = read_then_answer()
    result = await go(workspace, provider, asker(True, seen))
    assert result.ok and result.text == "It says alpha."
    assert result.tools == (("read_file", "ok"),) and result.turns == 2
    assert len(seen) == 1 and "read_file" in seen[0].title and "a.txt" in seen[0].detail
    assert "single call" in seen[0].hint and "digest" in seen[0].hint
    assert any("alpha contents" in str(r.messages) for r in provider.requests[1:])


@pytest.mark.parametrize(
    "verdict",
    [False, None, "", "yes", "y", 1, asyncio.TimeoutError(), EOFError(), RuntimeError("boom")],
)
async def test_anything_but_a_literal_true_denies_and_the_tool_never_runs(workspace, verdict):
    provider = read_then_answer()
    result = await go(workspace, provider, asker(verdict))
    assert not result.ok and result.failure_kind == "acting_approval_denied"
    assert len(provider.requests) == 1  # no second model turn after the denial
    assert all("alpha contents" not in str(r.messages) for r in provider.requests)


async def test_no_prompt_at_all_denies_everything(workspace):
    provider = read_then_answer()
    result = await go(workspace, provider, None)
    assert not result.ok and result.failure_kind == "acting_approval_denied"


async def test_a_prompt_that_never_answers_is_a_timeout_denial_and_does_not_hang(workspace):
    async def never(prompt: ApprovalPrompt) -> Any:
        await asyncio.sleep(3600)

    result = await asyncio.wait_for(
        go(workspace, read_then_answer(), never, prompt_timeout_s=0.05), timeout=5
    )
    assert not result.ok and result.failure_kind == "acting_approval_denied"


async def test_approval_is_not_remembered_between_calls(workspace):
    seen: list[ApprovalPrompt] = []
    answers = iter([True, False])

    async def ask(prompt: ApprovalPrompt) -> Any:
        seen.append(prompt)
        return next(answers)

    provider = ScriptedProvider(
        reply("", [read_call("a.txt", "c1")]),
        reply("", [read_call("b.txt", "c2")]),
        reply("done", stop="stop"),
    )
    result = await go(workspace, provider, ask)
    assert len(seen) == 2  # the second, different call was asked about again
    assert not result.ok and result.failure_kind == "acting_approval_denied"
    assert result.tools == (("read_file", "ok"), ("read_file", "refused"))
    assert all("beta contents" not in str(r.messages) for r in provider.requests)


async def test_the_same_call_proposed_again_needs_a_fresh_decision(workspace):
    calls = 0

    async def ask(prompt: ApprovalPrompt) -> Any:
        nonlocal calls
        calls += 1
        return calls == 1  # yes once, then no

    provider = ScriptedProvider(
        reply("", [read_call("a.txt", "c1")]),
        reply("", [read_call("a.txt", "c2")]),
        reply("done", stop="stop"),
    )
    result = await go(workspace, provider, ask)
    assert calls == 2  # the identical second call was asked about again, not covered by the first
    assert result.tools == (("read_file", "ok"), ("read_file", "refused"))
    assert not result.ok and result.failure_kind == "acting_approval_denied"
    assert len(provider.requests) == 2  # nothing was sent to the model after the refusal


# ── display safety ────────────────────────────────────────────────────────────


def test_sanitize_removes_escape_control_and_bidi_characters():
    hostile = "ok\x1b[31mred\x07\x00‮rtl line\x9b"
    cleaned = sanitize_text(hostile)
    assert "\x1b" not in cleaned and "\x07" not in cleaned and "‮" not in cleaned
    assert all(ord(c) >= 32 or c == "\n" for c in cleaned)
    assert sanitize_text("a" * 1000, 10).endswith("…") and len(sanitize_text("a" * 1000, 10)) <= 11


async def test_the_ui_only_ever_receives_sanitized_text():
    seen: list[ApprovalPrompt] = []
    wrapped = safe_ask(asker(True, seen))
    await wrapped(ApprovalPrompt(title="t\x1b[2J", detail="d\x00‮", hint="h\x07"))
    got = seen[0]
    for text in (got.title, got.detail, got.hint):
        assert "\x1b" not in text and "\x00" not in text and "\x07" not in text
        assert "‮" not in text
    assert safe_ask(None) is None


async def test_a_secret_in_the_proposed_arguments_never_reaches_the_prompt(workspace):
    (workspace / f"{CANARY}.txt").write_text("x", encoding="utf-8")
    seen: list[ApprovalPrompt] = []
    provider = ScriptedProvider(reply("", [read_call(f"{CANARY}.txt")]), reply("ok", stop="stop"))
    await go(workspace, provider, asker(False, seen), scrub=SecretScrubber(static_source(CANARY)))
    shown = " ".join(f"{p.title} {p.detail} {p.hint}" for p in seen)
    assert CANARY not in shown


# ── failures and cancellation ─────────────────────────────────────────────────


async def test_a_provider_failure_is_a_fixed_message_with_no_secret(workspace):
    provider = ScriptedProvider(InferenceError(f"upstream said {CANARY}"))
    result = await go(workspace, provider, asker(True), scrub=SecretScrubber(static_source(CANARY)))
    assert not result.ok and result.text is None
    assert CANARY not in repr(result) and result.message


async def test_cancelling_during_approval_propagates_and_starts_nothing_more(workspace):
    started = asyncio.Event()

    async def hangs(prompt: ApprovalPrompt) -> Any:
        started.set()
        await asyncio.sleep(3600)

    provider = read_then_answer()
    task = asyncio.ensure_future(go(workspace, provider, hangs, prompt_timeout_s=60))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(provider.requests) == 1  # no further provider request after the cancel
    await asyncio.sleep(0.05)
    assert len(provider.requests) == 1


async def test_a_failed_run_does_not_leave_the_scrubber_or_tools_half_built(workspace):
    # A scrubber that cannot initialise stops the run before the provider is ever called.
    def broken_store():
        raise RuntimeError(f"locked {CANARY}")

    provider = ScriptedProvider()
    from velune.orchestration.engine_adapters import secrets

    original = secrets.credential_store_source
    secrets.credential_store_source = lambda: broken_store  # type: ignore[assignment]
    try:
        with pytest.raises(NativeSessionError) as info:
            await go(workspace, provider, asker(True))
    finally:
        secrets.credential_store_source = original  # type: ignore[assignment]
    assert provider.requests == [] and CANARY not in str(info.value)


# ── the real CLI entrypoint ───────────────────────────────────────────────────


def _invoke(monkeypatch, tmp_path, args, *, provider=None, env=None, tty=False, approve=None):
    from velune.cli.app import create_app
    from velune.cli.commands import native
    from velune.providers.registry import ProviderRegistry

    for key in ("VELUNE_ORCHESTRATION__ENGINE",):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    lookups: list[str] = []

    def fake_get_or_raise(self, name):
        lookups.append(name)
        if provider is None:
            raise KeyError(name)
        return provider

    monkeypatch.setattr(ProviderRegistry, "get_or_raise", fake_get_or_raise)
    monkeypatch.setattr(native, "_is_tty", lambda: tty)
    if approve is not None:
        monkeypatch.setattr(approval_port, "confirm_ask", lambda: asker(approve))
    result = CliRunner().invoke(
        create_app(register="__all__"),
        ["--workspace", str(tmp_path), *args],
    )
    return result, lookups


NATIVE = ["native", "Read a.txt and say what it says", "--provider", "scripted", "--model", "m"]


def test_the_command_is_off_by_default_and_says_so_without_calling_anything(monkeypatch, workspace):
    provider = read_then_answer()
    result, lookups = _invoke(monkeypatch, workspace, NATIVE, provider=provider)
    assert result.exit_code == 1 and "off" in result.output.lower()
    assert lookups == [] and provider.requests == []  # no provider, no fallback, nothing run


def test_enabled_through_the_real_entrypoint_a_yes_reads_the_file(monkeypatch, workspace):
    provider = read_then_answer()
    result, lookups = _invoke(
        monkeypatch,
        workspace,
        NATIVE,
        provider=provider,
        env={"VELUNE_ORCHESTRATION__ENGINE": "on"},
        tty=True,
        approve=True,
    )
    assert result.exit_code == 0, result.output
    assert "It says alpha." in result.output
    assert lookups == ["scripted"]
    assert len(provider.requests) == 2


def test_enabled_but_a_no_stops_the_run_and_exits_nonzero(monkeypatch, workspace):
    provider = read_then_answer()
    result, _ = _invoke(
        monkeypatch,
        workspace,
        NATIVE,
        provider=provider,
        env={"VELUNE_ORCHESTRATION__ENGINE": "on"},
        tty=True,
        approve=False,
    )
    assert result.exit_code == 1 and "not approved" in result.output
    assert len(provider.requests) == 1


def test_without_an_interactive_terminal_every_call_is_denied(monkeypatch, workspace):
    provider = read_then_answer()
    result, _ = _invoke(
        monkeypatch,
        workspace,
        NATIVE,
        provider=provider,
        env={"VELUNE_ORCHESTRATION__ENGINE": "on"},
        tty=False,
    )
    assert result.exit_code == 1 and "No interactive terminal" in result.output
    assert len(provider.requests) == 1


def test_the_global_yes_flag_never_approves_a_tool_call(monkeypatch, workspace):
    provider = read_then_answer()
    result, _ = _invoke(
        monkeypatch,
        workspace,
        ["--yes", *NATIVE],
        provider=provider,
        env={"VELUNE_ORCHESTRATION__ENGINE": "on"},
        tty=False,
    )
    assert result.exit_code == 1 and len(provider.requests) == 1


def test_no_model_chosen_is_a_usage_error_not_a_guess(monkeypatch, workspace):
    from velune.cli import model_prefs

    monkeypatch.setattr(model_prefs, "load_active_model", lambda path=None: None)
    provider = ScriptedProvider()
    result, lookups = _invoke(
        monkeypatch,
        workspace,
        ["native", "do it"],
        provider=provider,
        env={"VELUNE_ORCHESTRATION__ENGINE": "on"},
    )
    assert result.exit_code == 2 and "Choose a provider and model" in result.output
    assert lookups == [] and provider.requests == []


def test_a_model_saved_for_another_provider_is_not_paired_with_this_one(monkeypatch, workspace):
    from velune.cli import model_prefs

    monkeypatch.setattr(
        model_prefs,
        "load_active_model",
        lambda path=None: model_prefs.ModelPref(provider_id="other", model_id="their-model"),
    )
    result, lookups = _invoke(
        monkeypatch,
        workspace,
        ["native", "do it", "--provider", "scripted"],
        provider=ScriptedProvider(),
        env={"VELUNE_ORCHESTRATION__ENGINE": "on"},
    )
    assert result.exit_code == 2 and lookups == []


def test_an_unknown_provider_fails_cleanly_without_a_traceback_or_substitute(
    monkeypatch, workspace
):
    result, lookups = _invoke(
        monkeypatch,
        workspace,
        NATIVE,
        provider=None,
        env={"VELUNE_ORCHESTRATION__ENGINE": "on"},
    )
    assert result.exit_code == 1 and "not available" in result.output
    assert "Traceback" not in result.output and lookups == ["scripted"]


def test_json_mode_reports_a_structured_result_and_never_prompts(monkeypatch, workspace):
    provider = read_then_answer()
    result, _ = _invoke(
        monkeypatch,
        workspace,
        ["--json", *NATIVE],
        provider=provider,
        env={"VELUNE_ORCHESTRATION__ENGINE": "on"},
        tty=True,
    )
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["ok"] is False and payload["failure_kind"] == "acting_approval_denied"


# ── nothing else changed ──────────────────────────────────────────────────────


def test_chat_ask_and_run_do_not_import_or_route_to_the_native_engine():
    code = (
        "import sys\n"
        "import velune.cli.commands.chat, velune.cli.commands.ask, velune.cli.commands.run\n"
        "bad = sorted(m for m in sys.modules if m.startswith("
        "('velune.cli.commands.native', 'velune.orchestration.engine_adapters.native_session')))\n"
        "print('BAD', bad)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("BAD []"), result.stdout


def test_the_existing_commands_keep_their_modules():
    from velune.cli.registry import COMMAND_SPECS

    by_name = {spec.name: spec for spec in COMMAND_SPECS}
    assert by_name["chat"].module == "velune.cli.commands.chat"
    assert by_name["ask"].module == "velune.cli.commands.ask"
    assert by_name["run"].module == "velune.cli.commands.run"
    assert by_name["native"].module == "velune.cli.commands.native"
    assert by_name["native"].bootstrap == "light"


def test_the_flag_default_is_still_off(monkeypatch):
    from velune.kernel.config import VeluneConfig

    monkeypatch.delenv("VELUNE_ORCHESTRATION__ENGINE", raising=False)
    assert VeluneConfig().orchestration.engine == "off"


# ── Phase 4E-6 review: the real approval widget, cancellation, alias pairing ──────────────────────
#
# These drive the real ``confirm_ask()`` widget through prompt_toolkit's pipe input and a dummy
# output. That proves the key mapping and that the Application is torn down (no app left running,
# the awaiting task is not stuck) on yes, no, Enter, Esc, Ctrl-C, timeout and task cancellation.
# It does NOT prove raw-mode restoration of a real console: a pipe input has no tty mode to restore.

ENTER, CTRL_C, ESC = "\r", "\x03", "\x1b"


async def real_widget(keys: str, *, wrap: bool = False, timeout: float = 10.0):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from velune.cli.commands.native import cancel_aware

    ask = approval_port.confirm_ask()
    if wrap:
        ask = cancel_aware(ask)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        if keys:
            pipe.send_text(keys)
        return await asyncio.wait_for(ask(ApprovalPrompt("Run it?", "detail", "hint")), timeout)


@pytest.mark.parametrize(
    ("keys", "expected"),
    [("y", True), ("Y", True), ("n", False), ("N", False), (ENTER, False)],
)
async def test_the_real_widget_approves_only_on_an_explicit_yes(keys, expected):
    assert await real_widget(keys) is expected  # Enter alone is the default: No


async def test_the_real_widget_reports_ctrl_c_and_esc_as_non_approval():
    from velune.cli.interactive.result import BACK, CANCEL

    assert await real_widget(CTRL_C) is CANCEL
    assert await real_widget(ESC) is BACK


async def test_ctrl_c_at_the_prompt_is_a_cancellation_that_denies_and_stops_the_run(workspace):
    from velune.cli.commands.native import cancel_aware
    from velune.cli.interactive.result import CANCEL

    provider = read_then_answer()
    with pytest.raises(asyncio.CancelledError):
        await go(workspace, provider, cancel_aware(asker(CANCEL)))
    assert len(provider.requests) == 1  # the tool never ran and no second model call was made


async def test_esc_at_the_prompt_is_an_ordinary_denial_not_a_cancellation(workspace):
    from velune.cli.commands.native import cancel_aware
    from velune.cli.interactive.result import BACK

    result = await go(workspace, read_then_answer(), cancel_aware(asker(BACK)))
    assert not result.ok and result.failure_kind == "acting_approval_denied"


async def test_a_real_widget_that_times_out_denies_and_leaves_no_application_running(workspace):
    from prompt_toolkit.application import create_app_session, get_app_or_none
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from velune.cli.commands.native import cancel_aware

    provider = read_then_answer()
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        result = await asyncio.wait_for(
            go(
                workspace,
                provider,
                cancel_aware(approval_port.confirm_ask()),
                prompt_timeout_s=0.3,
            ),
            timeout=10,
        )
        assert get_app_or_none() is None  # the widget's Application was torn down
    assert not result.ok and result.failure_kind == "acting_approval_denied"  # timeout, not cancel
    assert len(provider.requests) == 1


async def test_cancelling_the_task_while_the_real_widget_is_open_unwinds_cleanly(workspace):
    from prompt_toolkit.application import create_app_session, get_app_or_none
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    provider = read_then_answer()
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        task = asyncio.ensure_future(go(workspace, provider, approval_port.confirm_ask()))
        for _ in range(100):  # let the widget start
            await asyncio.sleep(0.02)
            if get_app_or_none() is not None:
                break
        assert get_app_or_none() is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        await asyncio.sleep(0.05)
        assert get_app_or_none() is None
    assert len(provider.requests) == 1


async def test_cancelling_during_a_provider_call_stops_everything(workspace):
    class Slow:
        provider_id = "slow"

        def __init__(self) -> None:
            self.requests: list = []

        async def infer(self, request):
            self.requests.append(request)
            await asyncio.sleep(3600)

    provider = Slow()
    task = asyncio.ensure_future(go(workspace, provider, asker(True)))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if provider.requests:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert len(provider.requests) == 1


def test_a_deadline_is_a_failure_not_a_cancellation(monkeypatch, workspace):
    class Slow:
        provider_id = "slow"

        async def infer(self, request):
            await asyncio.sleep(3600)

    result, _ = _invoke(
        monkeypatch,
        workspace,
        [*NATIVE, "--timeout", "1"],
        provider=Slow(),
        env={"VELUNE_ORCHESTRATION__ENGINE": "on"},
        tty=True,
        approve=True,
    )
    assert result.exit_code == 1 and "Cancelled" not in result.output
    assert "Traceback" not in result.output


def test_ctrl_c_at_the_prompt_through_the_cli_exits_130_and_stops(monkeypatch, workspace):
    from velune.cli.interactive.result import CANCEL

    provider = read_then_answer()
    result, _ = _invoke(
        monkeypatch,
        workspace,
        NATIVE,
        provider=provider,
        env={"VELUNE_ORCHESTRATION__ENGINE": "on"},
        tty=True,
        approve=CANCEL,
    )
    assert result.exit_code == 130 and "Cancelled" in result.output
    assert len(provider.requests) == 1 and "Traceback" not in result.output


def test_the_global_yes_flag_cannot_approve_even_with_a_terminal(monkeypatch, workspace):
    provider = read_then_answer()
    result, _ = _invoke(
        monkeypatch,
        workspace,
        ["--yes", *NATIVE],
        provider=provider,
        env={"VELUNE_ORCHESTRATION__ENGINE": "on"},
        tty=True,
        approve=False,
    )
    assert result.exit_code == 1 and "not approved" in result.output
    assert len(provider.requests) == 1


def test_an_unforeseen_error_shows_a_fixed_line_and_no_secret(monkeypatch, workspace):
    from velune.orchestration.engine_adapters import native_session

    async def boom(*args, **kwargs):
        raise RuntimeError(f"internal detail {CANARY}")

    monkeypatch.setattr(native_session, "run_native_task", boom)
    result, _ = _invoke(
        monkeypatch,
        workspace,
        NATIVE,
        provider=ScriptedProvider(),
        env={"VELUNE_ORCHESTRATION__ENGINE": "on"},
    )
    assert result.exit_code == 1 and "failed unexpectedly" in result.output
    assert CANARY not in result.output and "Traceback" not in result.output


def test_the_disabled_message_gives_a_working_powershell_example(monkeypatch, workspace):
    result, _ = _invoke(monkeypatch, workspace, NATIVE, provider=ScriptedProvider())
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert '$env:VELUNE_ORCHESTRATION__ENGINE = "on"' in flat


def test_a_legacy_alias_of_the_saved_provider_pairs_with_its_own_model(monkeypatch):
    from velune.cli import model_prefs
    from velune.cli.commands.native import resolve_choice

    monkeypatch.setattr(
        model_prefs,
        "load_active_model",
        lambda path=None: model_prefs.ModelPref(provider_id="nvidia", model_id="saved-model"),
    )
    assert resolve_choice("nvidia_nim", None) == ("nvidia_nim", "saved-model")
    assert isinstance(resolve_choice("groq", None), str)  # a different provider: never paired
    assert resolve_choice("groq", "explicit") == ("groq", "explicit")
    assert resolve_choice(None, None) == ("nvidia", "saved-model")
