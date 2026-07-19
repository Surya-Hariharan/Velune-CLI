"""Regression tests: the interactive render path must never block on I/O.

The fullscreen REPL re-renders on every keystroke. Historically two things ran
inside that per-frame path and made typing lag — worst on machines with no
Ollama, where a socket probe took the slow refused/timeout path:

  1. ``_home_state()`` called ``list_configured_providers()`` which did a
     blocking ``socket.create_connection`` to the Ollama port every frame.
  2. ``_refresh_status_state()`` re-tokenized the entire conversation with
     tiktoken every frame.

These tests pin the fixes: the Ollama probe is memoized and moved off the
render path, provider enumeration is TTL-cached and never opens a socket, and
token counting only reruns when the conversation actually changes.
"""

from __future__ import annotations

import socket
import time
from unittest.mock import MagicMock

import velune.providers.keystore as ks
from velune.cli.modes import SessionMode
from velune.cli.repl import VeluneREPL
from velune.cli.statusbar import StatusBarState
from velune.context.utilization import ContextUtilizationTracker

# ── is_ollama_live() memoization ─────────────────────────────────────────────


def test_is_ollama_live_memoizes_within_ttl(monkeypatch):
    monkeypatch.setattr(ks, "_ollama_live_cache", None, raising=False)
    calls = {"n": 0}

    def fake_connect(_addr, timeout=None):
        calls["n"] += 1
        raise OSError("connection refused")

    monkeypatch.setattr(socket, "create_connection", fake_connect)

    results = [ks.is_ollama_live() for _ in range(50)]

    assert all(r is False for r in results)
    assert calls["n"] == 1  # 50 calls, one real probe within the TTL window


def test_is_ollama_live_reprobes_after_ttl(monkeypatch):
    monkeypatch.setattr(ks, "_ollama_live_cache", None, raising=False)
    calls = {"n": 0}

    def fake_connect(_addr, timeout=None):
        calls["n"] += 1
        raise OSError()

    monkeypatch.setattr(socket, "create_connection", fake_connect)

    clock = {"v": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["v"])

    ks.is_ollama_live()
    ks.is_ollama_live()
    assert calls["n"] == 1

    clock["v"] += ks._OLLAMA_LIVE_TTL + 1.0
    ks.is_ollama_live()
    assert calls["n"] == 2  # cache expired, one fresh probe


# ── _configured_providers(): cached, socket-free ─────────────────────────────


def _bare_repl() -> VeluneREPL:
    repl = VeluneREPL.__new__(VeluneREPL)  # bypass heavyweight __init__
    repl._providers_cache = None
    repl._ollama_live = False
    return repl


def test_configured_providers_never_probes_ollama_socket(monkeypatch):
    seen = {"include_ollama": "unset", "calls": 0}

    def fake_list(include_ollama=True):
        seen["include_ollama"] = include_ollama
        seen["calls"] += 1
        return ["groq", "openai"]

    monkeypatch.setattr(ks, "list_configured_providers", fake_list)

    def boom(*_a, **_k):
        raise AssertionError("render path must not open a socket")

    monkeypatch.setattr(socket, "create_connection", boom)

    repl = _bare_repl()
    out = repl._configured_providers()

    # The socket-probing include_ollama=True path must never be taken here.
    assert seen["include_ollama"] is False
    assert out == ["groq", "openai"]

    # Repeated calls within the TTL reuse the cached snapshot.
    for _ in range(10):
        repl._configured_providers()
    assert seen["calls"] == 1


def test_configured_providers_prepends_ollama_from_background_flag(monkeypatch):
    monkeypatch.setattr(ks, "list_configured_providers", lambda include_ollama=True: ["groq"])

    repl = _bare_repl()
    repl._ollama_live = True

    out = repl._configured_providers()
    assert out[0] == "ollama"
    assert "groq" in out


# ── _refresh_status_state(): token count only recomputed on change ───────────


def _status_repl() -> VeluneREPL:
    repl = VeluneREPL.__new__(VeluneREPL)
    repl.container = MagicMock()
    repl.container.get.return_value = None  # no workspace → no git subprocess
    repl.active_model = MagicMock(model_id="m", context_length=8192, provider_id="groq")
    repl._context_tracker = ContextUtilizationTracker()
    repl._conversation = [{"role": "user", "content": "hello world"}]
    repl._ctx_signature = None
    repl._status_state = StatusBarState()
    repl._interrupts = MagicMock(exit_hint_active=False)
    repl._mode_manager = MagicMock()
    repl._mode_manager.current = SessionMode.NORMAL
    repl.session_cost = 0.0
    repl._job_registry = None
    repl._mcp_registry = MagicMock()
    repl._mcp_registry._entries = {}
    repl._prev_ctx_pct = 0.0
    return repl


def test_status_render_does_not_retokenize_when_conversation_unchanged():
    repl = _status_repl()

    calls = {"n": 0}
    orig = repl._context_tracker.update

    def counting(convo):
        calls["n"] += 1
        return orig(convo)

    repl._context_tracker.update = counting

    for _ in range(5):
        repl._refresh_status_state()
    assert calls["n"] == 1  # tokenized once despite five renders

    repl._conversation.append({"role": "assistant", "content": "hi there"})
    repl._refresh_status_state()
    assert calls["n"] == 2  # re-tokenized after the conversation grew
