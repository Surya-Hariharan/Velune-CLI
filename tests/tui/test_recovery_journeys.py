"""Terminal journeys for credential persistence and local-model recovery.

Same harness as ``test_journeys.py``: the real CLI in a PTY, an isolated
sandbox (config, keyring, data and home), and the local fake backend. A
relaunch reuses the sandbox, which is exactly what a restart is.
"""

from __future__ import annotations

import time

from . import flows
from .fake_backend import OUTAGE_MARKER
from .harness import assert_no_secret

NORMAL = "NORMAL │"
LOCAL_MODEL = "fake-model:latest"


def assert_clean_frame(s) -> None:
    t = s.text()
    assert "Traceback" not in t, t
    assert t.count(NORMAL) == 1, t


def relaunch(launch, **kw):
    """Start the CLI again in the same sandbox and get to the idle composer.

    A relaunch can meet two prompts on the way, in either order: the wizard
    (when no provider is reachable, e.g. only a local model whose server is
    down — it runs as for a fresh install) and the workspace-trust question
    (activating a model writes ``velune.toml`` into the workspace). The wizard
    is skipped and trust is declined, the safe default.
    """
    s = launch(**kw)
    handled: set[str] = set()
    deadline = time.time() + flows.STARTUP_TIMEOUT
    while time.time() < deadline:
        t = s.text()
        if "Trust this directory?" in t and "trust" not in handled:
            handled.add("trust")
            s.type("n")
            s.key("enter")
        elif "Skip Setup" in t and "wizard" not in handled:
            handled.add("wizard")
            for _ in range(3):
                s.key("down")
                time.sleep(0.1)
            s.key("enter")
            s.wait_for("Continue to REPL", 20)
            s.key("enter")
        elif flows.COMPOSER in t:
            s.settle()
            return s
        time.sleep(0.2)
    raise AssertionError("never reached the composer\n" + s.text())


def exit_cleanly(s) -> None:
    flows.run_command(s, "/exit")
    assert s.wait_exit(20), "process did not exit after /exit"


def test_saved_key_survives_restart_and_stays_verified(launch, backend):
    s = launch()
    flows.skip_onboarding(s)
    flows.connect(s)
    s.wait_for("OpenAI connected.", 30)
    flows.idle(s)
    exit_cleanly(s)

    # After a restart the stored key must still be the one in use: the fake
    # backend accepts only FAKE_KEY, and no *_API_KEY variable reaches the CLI.
    s2 = relaunch(launch)
    before = len(backend.requests)
    flows.ask(s2, "after restart")
    s2.wait_for("FAKE-REPLY-OK", 40)
    flows.idle(s2)
    assert any(
        path.endswith("/chat/completions") and host == "api.openai.com"
        for _m, path, host in backend.requests[before:]
    )
    s2.snapshot("20_key_after_restart")
    assert_no_secret(s2)
    assert_clean_frame(s2)


def test_provider_outage_saves_the_key_and_says_so(launch, backend):
    key = f"sk-velune-test-{OUTAGE_MARKER}-000000000000"
    s = launch()
    flows.skip_onboarding(s)
    flows.connect(s, provider="openai", key=key)
    s.wait_for("validation could not be completed", 30)
    t = s.text()
    assert "temporarily unavailable" in t, t
    assert "Invalid" not in t and "rejected" not in t.lower(), t
    s.snapshot("21_outage_saved_unverified")
    flows.idle(s, 30)
    assert key not in s.raw
    assert_clean_frame(s)


def test_local_model_on_custom_host_then_offline_then_back(launch, backend):
    # 1. Ollama reachable only through OLLAMA_HOST (the fake backend's port).
    s = relaunch(launch, ollama=True)
    flows.run_command(s, f"/model connect {LOCAL_MODEL}")
    s.wait_for(f"Active model: {LOCAL_MODEL}", 40)
    flows.idle(s)
    flows.ask(s, "hello local")
    s.wait_for("FAKE-REPLY-OK", 30)
    flows.idle(s)
    assert any(
        method == "POST" and path.startswith("/api/chat") for method, path, _h in backend.requests
    ), "chat did not go to the OLLAMA_HOST server"
    exit_cleanly(s)

    # 2. Restart with the server gone: the model is kept, the prompt is not lost.
    s2 = relaunch(launch, ollama=False)
    flows.ask(s2, "hello again")
    s2.wait_for("is unavailable, so the message was not sent", 30)
    t = s2.wait_for("Ollama server is not reachable", 10)
    assert "ollama serve" in t, t
    s2.wait_for("❯ hello again", 10)  # the prompt was put back in the composer
    s2.snapshot("22_local_model_unavailable")
    assert_clean_frame(s2)
    s2.key("ctrl-u")
    exit_cleanly(s2)

    # 3. Server back: the saved registration is reused with no setup.
    s3 = relaunch(launch, ollama=True)
    flows.ask(s3, "back again")
    s3.wait_for("FAKE-REPLY-OK", 30)
    flows.idle(s3)
    assert_clean_frame(s3)
