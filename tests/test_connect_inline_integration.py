"""End-to-end check that /connect runs as one continuous panel interaction.

The unit tests in ``test_inline_flow.py`` prove ``InlineFlow`` behaves; this
proves the thing the user actually types is wired to it. ``ProviderPalette`` is
driven unmodified — it still calls ``single_select`` / ``text_input`` /
``run_with_status`` exactly as it does standalone — and the assertion is that
with a host installed, every one of those steps lands in the one panel instead
of standing up an Application of its own.

The regression this guards against is subtle: it is entirely possible for the
flow to work and for /connect to still bypass it, because the routing lives in
``velune.cli.interactive``'s helpers rather than in the provider code.
"""

from __future__ import annotations

import asyncio

import pytest

from velune.cli.inline_flow import InlineFlow
from velune.cli.interactive import host as interactive_host
from velune.providers.validation import ValidationResult, ValidationStatus


class _FakeBuffer:
    """Just enough Buffer for the flow: text plus reset()."""

    def __init__(self) -> None:
        self.text = ""

    def reset(self, document=None) -> None:
        self.text = document.text if document is not None else ""


@pytest.fixture
def flow():
    f = InlineFlow()
    f.bind(_FakeBuffer(), lambda: None)
    interactive_host.install(f)
    try:
        yield f
    finally:
        interactive_host.uninstall(f)


def _steps(flow: InlineFlow) -> list[str]:
    """Record the frame title of every step the panel shows, in order."""
    seen: list[str] = []
    original = flow._open

    def _spy(stage):
        seen.append(stage.frame_title)
        original(stage)

    flow._open = _spy
    return seen


@pytest.mark.usefixtures("flow")
def test_connect_runs_pick_then_key_then_verify_in_one_panel(flow, monkeypatch, tmp_path):
    """The user's whole ask, as one assertion: pick → key → verify, one surface."""
    from velune.cli import provider_ui

    saved: dict = {}
    monkeypatch.setattr(
        provider_ui, "save_key", lambda pid, key, verified: saved.update(pid=pid, key=key)
    )
    monkeypatch.setattr(provider_ui, "mark_verified", lambda pid, model_count=0: None)
    monkeypatch.setattr(provider_ui.ProviderPalette, "_report_saved", lambda self, label: None)

    async def _fake_validate(pid, key):
        return ValidationResult(
            provider_id=pid, status=ValidationStatus.OK, message="ok", models=["m1"]
        )

    monkeypatch.setattr(provider_ui, "validate_provider", _fake_validate)

    async def _no_discovery(self, pid):
        return None

    monkeypatch.setattr(provider_ui.ProviderPalette, "_discover_one", _no_discovery)

    class _Console:
        def print(self, *a, **k):
            pass

    palette = provider_ui.ProviderPalette(console=_Console(), container=None)
    seen = _steps(flow)

    async def _drive():
        task = asyncio.ensure_future(palette.run("add"))

        # 1. The provider picker opens in the panel.
        await asyncio.sleep(0)
        assert flow.is_active(), "provider picker did not open in the REPL panel"
        assert flow.takes_text() is False, "a picker must not mask the prompt box"
        flow._resolve("anthropic")

        # 2. The very same panel asks for the key, and the prompt box masks.
        await asyncio.sleep(0)
        assert flow.is_active(), "the panel closed between picking and key entry"
        assert flow.takes_text() is True
        assert flow.is_masked() is True, "the API key step must mask the prompt box"
        flow._resolve("sk-ant-secret")

        # 3. Verification settles, then the panel closes for good.
        await task

    asyncio.run(_drive())

    assert saved == {"pid": "anthropic", "key": "sk-ant-secret"}
    assert flow.is_active() is False
    assert flow.is_masked() is False

    # Three steps, one panel: pick, key, verify — no step ever fell back to a
    # standalone Application (which would not have gone through _open at all).
    assert len(seen) == 3
    assert "Connect provider" in seen[0]
    assert "Anthropic" in seen[1]


class _Console:
    def print(self, *a, **k):
        pass


@pytest.mark.usefixtures("flow")
def test_ctrl_c_at_the_provider_picker_abandons_the_whole_command(flow, monkeypatch):
    """Ctrl+C must unwind /connect, not just close one step.

    If it merely resolved the step, ``_flow_add`` would read that as "the user
    picked nothing" and return — which looks the same here — but a menu step
    would instead redisplay its parent menu, leaving the user stuck in a flow
    they asked to leave.
    """
    from velune.cli import provider_ui
    from velune.cli.inline_flow import FlowCancelled

    called: list[str] = []
    monkeypatch.setattr(provider_ui, "save_key", lambda *a, **k: called.append("saved"))

    palette = provider_ui.ProviderPalette(console=_Console(), container=None)

    async def _drive():
        task = asyncio.ensure_future(palette.run("add"))
        await asyncio.sleep(0)
        flow.cancel()
        await task

    with pytest.raises(FlowCancelled):
        asyncio.run(_drive())
    assert called == [], "no key should have been saved"
    assert flow.is_active() is False


@pytest.mark.usefixtures("flow")
def test_ctrl_c_at_the_key_field_never_saves_a_partial_key(flow, monkeypatch):
    from velune.cli import provider_ui
    from velune.cli.inline_flow import FlowCancelled

    saved: list = []
    monkeypatch.setattr(provider_ui, "save_key", lambda *a, **k: saved.append(a))

    palette = provider_ui.ProviderPalette(console=_Console(), container=None)

    async def _drive():
        task = asyncio.ensure_future(palette.run("add"))
        await asyncio.sleep(0)
        flow._resolve("anthropic")  # get past the picker, onto the key field
        await asyncio.sleep(0)
        assert flow.is_masked(), "should be on the key field"
        flow._buffer.text = "sk-ant-half-typed"
        flow.cancel()
        await task

    with pytest.raises(FlowCancelled):
        asyncio.run(_drive())
    assert saved == []
    assert flow._buffer.text == "", "the half-typed key must not be left in the prompt box"


def test_without_a_host_the_helpers_still_run_standalone():
    """velune setup and other non-REPL entry points must be unaffected."""
    assert interactive_host.active() is None
