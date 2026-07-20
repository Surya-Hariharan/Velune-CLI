"""Tests for InlineFlow — /connect and friends running inside the REPL's own
Application, above the prompt box, instead of in throwaway Applications below it.

These drive a real ``prompt_toolkit`` Application over a pipe input, with a real
``Buffer`` playing the REPL's prompt box, because the whole point of the class is
plumbing between those two things: which binding wins on Enter, what the buffer's
text means at each step, and whether the panel closes when a step resolves.
Calling the methods directly would test none of it.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output import DummyOutput

from velune.cli.inline_flow import FlowCancelled, InlineFlow
from velune.cli.interactive.result import BACK
from velune.cli.interactive.widgets import Option


async def _teardown(flow, task):
    """Close whatever step is open and absorb the abort it raises.

    Several tests below only need a step *open* in order to inspect it; how it
    ends is not what they are asserting. ``cancel()`` is the way to end one, and
    it raises ``FlowCancelled`` by design, so they swallow it here.
    """
    flow.cancel()
    with contextlib.suppress(FlowCancelled):
        await task


OPTIONS = [
    Option("openai", "OpenAI", "GPT models", group="Cloud", badge="verified"),
    Option("anthropic", "Anthropic", "Claude models", group="Cloud"),
    Option("ollama", "Ollama", "Local models", group="Local"),
]


def _text(fragments) -> str:
    return "".join(t for _style, t in fragments)


def _drive(step, keys: str, *, flow: InlineFlow | None = None):
    """Run *step(flow)* to completion while *keys* are fed to a live Application.

    Mirrors the real wiring: the flow's bindings are registered first and the
    REPL's own non-eager Enter handler after, so the test also covers the
    binding-precedence question that decides whether Enter reaches the flow.
    """
    flow = flow or InlineFlow()

    async def _run():
        with create_pipe_input() as pipe:
            buffer = Buffer(multiline=True)
            kb = KeyBindings()
            flow.add_bindings(kb)

            submitted: list[str] = []

            @kb.add("enter")
            def _repl_enter(event) -> None:
                # Stand-in for FullscreenREPLUI's own Enter handler.
                submitted.append(event.current_buffer.text)

            app = Application(
                layout=Layout(Window(BufferControl(buffer=buffer)), focused_element=buffer),
                key_bindings=kb,
                full_screen=False,
                input=pipe,
                output=DummyOutput(),
            )
            flow.bind(buffer, app.invalidate)

            async def _act():
                result = await step(flow)
                app.exit()
                return result

            # Open the step *before* any key is written into the pipe. Sending
            # first races: the Application starts draining input as soon as
            # run_async() is awaited, and a Down arrow that lands before the
            # flow is active falls through to the buffer's own cursor motion.
            task = asyncio.ensure_future(_act())
            await asyncio.sleep(0)
            assert flow.is_active(), "step did not open"
            pipe.send_text(keys)

            await app.run_async()
            return await task, submitted, buffer

    return asyncio.run(_run())


# ── Selection ────────────────────────────────────────────────────────────────


def test_enter_reaches_the_flow_not_the_repls_own_submit_handler():
    result, submitted, _buffer = _drive(lambda f: f.select("Connect provider", OPTIONS), "\r")

    assert result == "openai"
    # The REPL's Enter handler must never have run — otherwise the keystroke
    # would also have been sent to the model as a prompt.
    assert submitted == []


def test_arrow_keys_move_the_selection():
    result, _submitted, _buffer = _drive(
        lambda f: f.select("Connect provider", OPTIONS), "\x1b[B\r"
    )

    assert result == "anthropic"


def test_typing_in_the_prompt_box_filters_the_list():
    result, _submitted, _buffer = _drive(lambda f: f.select("Connect provider", OPTIONS), "olla\r")

    assert result == "ollama"


def test_escape_backs_out_of_a_step():
    result, _submitted, _buffer = _drive(lambda f: f.select("Connect provider", OPTIONS), "\x1b")

    assert result is BACK


def test_the_prompt_box_is_left_empty_when_a_step_resolves():
    _result, _submitted, buffer = _drive(lambda f: f.select("Connect provider", OPTIONS), "olla\r")

    # The filter text was consumed by the palette; it must not be sitting in the
    # prompt box afterwards, where the next Enter would send it to the model.
    assert buffer.text == ""


# ── Key entry in the same prompt box ─────────────────────────────────────────


def test_text_step_reads_the_prompt_box():
    result, submitted, buffer = _drive(
        lambda f: f.prompt_text("Anthropic API key", password=True), "sk-ant-123\r"
    )

    assert result == "sk-ant-123"
    assert submitted == []
    assert buffer.text == ""


def test_a_failed_validation_keeps_the_step_open_and_reports_why():
    flow = InlineFlow()

    def _no_spaces(value: str) -> str | None:
        return "That key contains spaces." if " " in value else None

    # First Enter fails validation and must not resolve. The rejected text is
    # deliberately left in the box to be corrected rather than wiped, so the
    # retry backspaces over it instead of typing on top of it.
    result, _submitted, _buffer = _drive(
        lambda f: f.prompt_text("API key", validate=_no_spaces),
        "bad key\r" + "\x7f" * len("bad key") + "goodkey\r",
        flow=flow,
    )

    assert result == "goodkey"


def test_password_steps_mask_the_prompt_box_and_others_do_not():
    flow = InlineFlow()
    assert flow.is_masked() is False

    async def _check():
        buffer = Buffer(multiline=True)
        flow.bind(buffer, lambda: None)
        task = asyncio.ensure_future(flow.prompt_text("key", password=True))
        await asyncio.sleep(0)
        masked = flow.is_masked()
        await _teardown(flow, task)
        return masked

    assert asyncio.run(_check()) is True
    # ...and the panel closes again once the step is done.
    assert flow.is_masked() is False
    assert flow.is_active() is False


def test_the_prompt_caret_names_the_step_during_text_entry():
    flow = InlineFlow()

    async def _check():
        flow.bind(Buffer(multiline=True), lambda: None)
        task = asyncio.ensure_future(flow.prompt_text("Anthropic API key"))
        await asyncio.sleep(0)
        label = flow.prompt_label()
        await _teardown(flow, task)
        return label

    assert asyncio.run(_check()) == "Anthropic API key"


def test_selection_steps_do_not_label_the_caret():
    flow = InlineFlow()

    async def _check():
        flow.bind(Buffer(multiline=True), lambda: None)
        task = asyncio.ensure_future(flow.select("Pick", OPTIONS))
        await asyncio.sleep(0)
        label = flow.prompt_label()
        await _teardown(flow, task)
        return label

    assert asyncio.run(_check()) == ""


# ── Panel lifecycle ──────────────────────────────────────────────────────────


def test_the_panel_is_closed_while_no_step_is_running():
    flow = InlineFlow()
    assert flow.is_active() is False
    assert flow.frame_title() == ""


def test_consecutive_steps_reuse_one_panel_without_closing_it():
    """The whole promise of the rework: /connect's pick-then-paste is one panel.

    If the panel ever went inactive between the two steps, the float would blink
    out and back in — exactly the "UI shifting" this replaced.
    """
    flow = InlineFlow()
    seen: list[bool] = []

    async def _check():
        buffer = Buffer(multiline=True)
        flow.bind(buffer, lambda: seen.append(flow.is_active()))

        pick = asyncio.ensure_future(flow.select("Provider", OPTIONS))
        await asyncio.sleep(0)
        flow._resolve("anthropic")
        chosen = await pick

        key = asyncio.ensure_future(flow.prompt_text("Anthropic API key"))
        await asyncio.sleep(0)
        active_during_key = flow.is_active()
        await _teardown(flow, key)
        return chosen, active_during_key

    chosen, active_during_key = asyncio.run(_check())
    assert chosen == "anthropic"
    assert active_during_key is True


def test_frame_title_tracks_the_current_step():
    flow = InlineFlow()

    async def _check():
        flow.bind(Buffer(multiline=True), lambda: None)
        task = asyncio.ensure_future(
            flow.select("Provider", OPTIONS, frame_title="Connect provider")
        )
        await asyncio.sleep(0)
        title = flow.frame_title()
        await _teardown(flow, task)
        return title

    assert asyncio.run(_check()) == "Connect provider"


# ── Status step ──────────────────────────────────────────────────────────────


def test_run_status_returns_the_value_and_closes_the_panel():
    flow = InlineFlow()

    async def _work():
        return ["a", "b"]

    async def _check():
        flow.bind(Buffer(multiline=True), lambda: None)
        return await flow.run_status(
            _work(), pending="Verifying…", ok=lambda r: f"{len(r)} models", fail="no"
        )

    assert asyncio.run(_check()) == ["a", "b"]
    assert flow.is_active() is False


def test_run_status_reraises_so_a_crash_is_not_read_as_a_clean_failure():
    flow = InlineFlow()

    async def _boom():
        raise RuntimeError("network exploded")

    async def _check():
        flow.bind(Buffer(multiline=True), lambda: None)
        await flow.run_status(_boom(), pending="Verifying…", ok="ok", fail="bad")

    with pytest.raises(RuntimeError, match="network exploded"):
        asyncio.run(_check())
    assert flow.is_active() is False


# ── Confirm ──────────────────────────────────────────────────────────────────


def test_confirm_maps_the_two_rows_onto_a_bool():
    assert _drive(lambda f: f.confirm("Remove the key?"), "\r")[0] is True
    assert _drive(lambda f: f.confirm("Remove the key?"), "\x1b[B\r")[0] is False


def test_confirm_defaults_to_no_when_asked_to():
    result, _submitted, _buffer = _drive(
        lambda f: f.confirm("Remove the key?", default=False), "\r"
    )
    assert result is False


def test_confirm_still_distinguishes_cancellation_from_no():
    result, _submitted, _buffer = _drive(lambda f: f.confirm("Remove?"), "\x1b")
    assert result is BACK
    assert result is not False


# ── Rendering ────────────────────────────────────────────────────────────────


def test_the_panel_renders_the_current_steps_content():
    flow = InlineFlow()

    async def _check():
        flow.bind(Buffer(multiline=True), lambda: None)
        task = asyncio.ensure_future(flow.select("Provider", OPTIONS))
        await asyncio.sleep(0)
        panes = (_text(flow._render_results()), _text(flow._render_details()))
        await _teardown(flow, task)
        return panes

    results, details = asyncio.run(_check())
    assert "OpenAI" in results
    assert "3 results" in results
    assert "GPT models" in details


def test_a_text_step_surfaces_its_validation_error_in_the_panel():
    flow = InlineFlow()

    async def _check():
        buffer = Buffer(multiline=True)
        flow.bind(buffer, lambda: None)
        task = asyncio.ensure_future(flow.prompt_text("API key", validate=lambda v: "Too short."))
        await asyncio.sleep(0)
        flow._stage.submit("x")
        rendered = _text(flow._render_results())
        await _teardown(flow, task)
        return rendered

    assert "Too short." in asyncio.run(_check())


def test_ctrl_c_aborts_the_command_rather_than_resolving_the_step():
    """Esc and Ctrl+C must not share a channel.

    Esc resolves the step with BACK, which a caller reads as "go up a level"
    and may answer by showing another menu. Ctrl+C has to unwind the whole
    command instead, so it raises rather than returning a value the caller
    could interpret as a smaller navigation event.
    """
    flow = InlineFlow()

    async def _check():
        flow.bind(Buffer(multiline=True), lambda: None)
        task = asyncio.ensure_future(flow.select("Provider", OPTIONS))
        await asyncio.sleep(0)
        flow.cancel()
        await task

    with pytest.raises(FlowCancelled):
        asyncio.run(_check())
    assert flow.is_active() is False


def test_ctrl_c_is_inert_when_no_step_is_running():
    """A stray Ctrl+C at the idle prompt must fall through to the REPL."""
    flow = InlineFlow()
    flow.bind(Buffer(multiline=True), lambda: None)
    flow.cancel()  # must not raise, must not leave state behind
    assert flow.is_active() is False


def test_ctrl_c_aborts_work_that_is_not_waiting_on_a_keystroke():
    """The gap a status step would otherwise leave: Ctrl+C during verification.

    ``run_status`` awaits its own driver task, so cancelling the *step* is not
    enough — the in-flight work has to be reachable too, or Ctrl+C does nothing
    for as long as the provider takes to answer.
    """
    flow = InlineFlow()
    finished = []

    async def _slow():
        await asyncio.sleep(5)
        finished.append("ran to completion")
        return "value"

    async def _check():
        flow.bind(Buffer(multiline=True), lambda: None)
        task = asyncio.ensure_future(
            flow.run_status(_slow(), pending="Verifying…", ok="ok", fail="no")
        )
        await asyncio.sleep(0.05)
        assert flow.is_active(), "status step should be on screen"
        flow.cancel()
        await task

    with pytest.raises(FlowCancelled):
        asyncio.run(_check())
    assert finished == [], "the underlying work kept running after Ctrl+C"
    assert flow.is_active() is False


def test_a_shutdown_cancellation_is_not_disguised_as_a_user_abort():
    """Only Ctrl+C becomes FlowCancelled; a real cancellation stays one.

    Converting every CancelledError would make a genuine shutdown look like the
    user backing out of a menu, and the REPL would swallow it and carry on.
    """
    flow = InlineFlow()

    async def _slow():
        await asyncio.sleep(5)

    async def _check():
        flow.bind(Buffer(multiline=True), lambda: None)
        task = asyncio.ensure_future(
            flow.run_status(_slow(), pending="Verifying…", ok="ok", fail="no")
        )
        await asyncio.sleep(0.05)
        task.cancel()  # shutdown, not Ctrl+C
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_check())
