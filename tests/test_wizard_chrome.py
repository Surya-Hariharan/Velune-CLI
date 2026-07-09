"""Tests for WizardController: sidebar/header rendering, and real
key-driven end-to-end interaction via prompt_toolkit's pipe-input test
harness (no mocking of the widget or the key-binding layer).

The Esc-resolves-to-BACK test for TextInputWidget is a direct regression
test for the chrome.py bug fixed alongside this: `_get_key_bindings()` used
to hard-code `on_back=None` whenever the active widget was a
TextInputWidget, so Esc silently did nothing on every API-key screen.
"""

from __future__ import annotations

import asyncio

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from velune.cli.interactive.chrome import StageInfo, WizardController
from velune.cli.interactive.result import BACK
from velune.cli.interactive.widgets import Option, SelectWidget, TextInputWidget

# ── Chrome rendering (no event loop needed) ──────────────────────────────────


def test_sidebar_marks_completed_current_and_pending():
    stages = [StageInfo("a", "Stage A"), StageInfo("b", "Stage B"), StageInfo("c", "Stage C")]
    controller = WizardController("Test", stages)
    controller.mark_complete("a")
    controller.current_index = 1

    text = "".join(t for _style, t in controller._render_sidebar())
    assert "✓" in text  # Stage A, completed
    assert "❯" in text  # Stage B, current
    assert "○" in text  # Stage C, not yet reached
    assert "Stage A" in text and "Stage B" in text and "Stage C" in text


def test_header_shows_step_count_and_current_title():
    stages = [StageInfo("a", "Stage A"), StageInfo("b", "Stage B")]
    controller = WizardController("Velune Setup", stages)
    controller.current_index = 1

    text = "".join(t for _style, t in controller._render_header())
    assert "Velune Setup" in text
    assert "Step 2 / 2" in text
    assert "Stage B" in text


# ── Real key-driven interaction (prompt_toolkit pipe input) ─────────────────


async def test_wizard_controller_text_input_esc_resolves_back():
    with create_pipe_input() as pipe_input:
        stages = [StageInfo("key", "API Key")]
        controller = WizardController("Test", stages, input=pipe_input, output=DummyOutput())

        async def body():
            widget = TextInputWidget(title="Enter key", password=True, optional=True)
            return await controller.run_widget(widget, stage_index=0)

        task = asyncio.ensure_future(controller.run(body))
        await asyncio.sleep(0.05)
        pipe_input.send_text("\x1b")  # Esc
        result = await asyncio.wait_for(task, timeout=3)

    assert result is BACK


async def test_wizard_controller_checklist_arrow_space_enter():
    with create_pipe_input() as pipe_input:
        stages = [StageInfo("providers", "Providers")]
        controller = WizardController("Test", stages, input=pipe_input, output=DummyOutput())

        async def body():
            widget = SelectWidget(
                title="Select Providers",
                options=[Option("a", "Alpha"), Option("b", "Beta")],
                multiple=True,
            )
            return await controller.run_widget(widget, stage_index=0)

        task = asyncio.ensure_future(controller.run(body))
        await asyncio.sleep(0.05)
        pipe_input.send_text(" ")  # Space -> toggle Alpha
        await asyncio.sleep(0.05)
        pipe_input.send_text("\x1b[B")  # Down -> Beta
        await asyncio.sleep(0.05)
        pipe_input.send_text(" ")  # Space -> toggle Beta
        await asyncio.sleep(0.05)
        pipe_input.send_text("\r")  # Enter -> submit
        result = await asyncio.wait_for(task, timeout=3)

    assert result == ["a", "b"]
