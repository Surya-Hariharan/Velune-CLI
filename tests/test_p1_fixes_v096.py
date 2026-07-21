"""Regression tests for the v0.9.6 P1 production-readiness fixes.

Each test pins one behaviour the v0.9.6 audit found broken, so a future change
that reintroduces the defect fails loudly:

* R1 — a single Ctrl+C cancels the running foreground generation
* R2 — the stream renderer always finalizes the assistant region, even when
  generation is cancelled before the first token
* R8 — a KeyboardInterrupt at the tool-approval prompt propagates (aborts the
  turn) rather than being swallowed as a silent denial
* R3 — resuming a session archives the live conversation and adopts the resumed
  session id instead of discarding data
* R4 — WebFetch re-validates every redirect hop (SSRF-via-redirect guard)
* R5 — the credential master key prefers a passphrase over the weak machine
  fallback, and legacy machine-key stores still decrypt
* R7 — the reranker trust score differentiates the real hybrid source labels
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from velune.cli.interrupts import InterruptController

# ── R1: single Ctrl+C cancels foreground generation ─────────────────────────


async def test_cancel_foreground_cancels_running_task():
    ctrl = InterruptController()
    ctrl._loop = asyncio.get_running_loop()  # normally set by install()
    started = asyncio.Event()

    async def work():
        async with ctrl.foreground():
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(work())
    await started.wait()
    assert ctrl.has_foreground is True

    assert ctrl.cancel_foreground() is True

    with pytest.raises(asyncio.CancelledError):
        await task
    # The cancellation is attributable to the user, so the REPL loop knows to
    # recover rather than treat it as a shutdown.
    assert ctrl.consume_user_cancelled() is True


async def test_cancel_foreground_noop_when_idle():
    ctrl = InterruptController()
    assert ctrl.has_foreground is False
    assert ctrl.cancel_foreground() is False


def test_foreground_task_restored_after_nesting():
    async def scenario():
        ctrl = InterruptController()
        async with ctrl.foreground():
            outer = ctrl._foreground_task
            async with ctrl.foreground():
                assert ctrl.has_foreground is True
            # Nested exit must not blank out the still-active outer context.
            assert ctrl._foreground_task is outer
        assert ctrl.has_foreground is False

    asyncio.run(scenario())


# ── R2: stream renderer always finalizes ────────────────────────────────────


class _FakeCaps:
    supports_streaming = True


class _FakeProviderCancelsEarly:
    """Streams nothing and cancels before the first token."""

    def get_capabilities(self):
        return _FakeCaps()

    async def stream(self, request):
        raise asyncio.CancelledError()
        yield  # pragma: no cover — makes this an async generator


class _RecordingUI:
    def __init__(self):
        self.begin_calls = 0
        self.finish_calls = 0

    def begin_assistant(self, text="Thinking..."):
        self.begin_calls += 1

    def update_assistant(self, text, *, final=False):
        pass

    def finish_assistant(self):
        self.finish_calls += 1


async def test_stream_renderer_finalizes_on_early_cancel():
    from rich.console import Console

    from velune.cli.stream_renderer import StreamRenderer

    ui = _RecordingUI()
    renderer = StreamRenderer(
        console=Console(),
        interrupts=InterruptController(),
        status_state=SimpleNamespace(last_latency_ms=None, last_tokens_per_sec=None),
    )
    renderer.attach_fullscreen_ui(ui)

    # A fresh controller did not issue the cancel, so render re-raises — but the
    # finally must still tear the assistant region down first.
    with pytest.raises(asyncio.CancelledError):
        await renderer.render(_FakeProviderCancelsEarly(), MagicMock())

    assert ui.begin_calls == 1
    assert ui.finish_calls >= 1


# ── R8: KeyboardInterrupt at the approval prompt propagates ──────────────────


def _approval_repl_ui():
    repl = MagicMock()
    ui = MagicMock()
    return repl, ui


# The prompt now runs through the shared prompt_toolkit widget rather than a
# blocking rich.prompt read on a stdin the fullscreen app already owns, so these
# patch single_select instead of asyncio.to_thread. The guarantees are unchanged:
# a real Ctrl+C aborts the turn, and anything else denies.


async def test_approval_prompt_propagates_keyboard_interrupt():
    from velune.cli.handlers.tool_chat import _prompt_approval

    repl, ui = _approval_repl_ui()
    with patch("velune.cli.interactive.is_interactive_tty", return_value=True):
        with patch("velune.cli.interactive.single_select", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                await _prompt_approval(repl, ui, "execute_command", {"command": "rm -rf /"})


async def test_approval_prompt_denies_on_widget_failure():
    from velune.cli.handlers.tool_chat import _prompt_approval

    repl, ui = _approval_repl_ui()
    with patch("velune.cli.interactive.is_interactive_tty", return_value=True):
        with patch("velune.cli.interactive.single_select", side_effect=EOFError):
            assert await _prompt_approval(repl, ui, "write_file", {"path": "x"}) is False


async def test_approval_prompt_denies_without_a_tty():
    """Piped input or CI: nobody is there to approve a mutating call."""
    from velune.cli.handlers.tool_chat import _prompt_approval

    repl, ui = _approval_repl_ui()
    with patch("velune.cli.interactive.is_interactive_tty", return_value=False):
        assert await _prompt_approval(repl, ui, "write_file", {"path": "x"}) is False


# ── R3: session resume archives the live conversation ───────────────────────


async def test_resume_snapshot_archives_and_adopts_session_id():
    from velune.cli.handlers.session_mgmt import _resume_snapshot

    repl = MagicMock()
    repl._session_id = "orig-random-id"
    repl._conversation = [{"role": "user", "content": "unsaved live turn"}]
    repl._archive_current_session = MagicMock()
    repl._end_episodic_session = AsyncMock()
    repl._start_episodic_session = AsyncMock()

    meta = SimpleNamespace(total_tokens=42, title="Prior", turn_count=2, model_id="m")
    resumed = [{"role": "user", "content": "old conversation"}]
    repl._session_store.load.return_value = (meta, resumed)

    ok = await _resume_snapshot(repl, "resumed-id")

    assert ok is True
    # The live conversation was archived before being replaced (no silent loss).
    repl._archive_current_session.assert_called_once()
    # And the REPL adopts the resumed id so exit writes back to that slot.
    assert repl._session_id == "resumed-id"
    assert repl._conversation == resumed
    assert repl.session_tokens == 42


# ── R4: WebFetch re-validates redirect hops ─────────────────────────────────


class _Resp:
    def __init__(self, *, is_redirect=False, location=None, text=""):
        self.is_redirect = is_redirect
        self.headers = {"location": location} if location else {}
        self.text = text

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        return self._responses.pop(0)


def _validator_blocking_metadata(url):
    if "169.254.169.254" in url:
        return (False, "blocked link-local metadata address")
    return (True, None)


async def test_web_fetch_rejects_redirect_to_blocked_host():
    from velune.tools.web.fetch import WebFetch

    client = _FakeClient(
        [_Resp(is_redirect=True, location="http://169.254.169.254/latest/meta-data/")]
    )
    with (
        patch("velune.tools.web.fetch.validate_url", side_effect=_validator_blocking_metadata),
        patch("httpx.AsyncClient", return_value=client),
    ):
        with pytest.raises(ValueError, match="validation failed"):
            await WebFetch().execute("https://public.example.com")


async def test_web_fetch_follows_allowed_redirect():
    from velune.tools.web.fetch import WebFetch

    client = _FakeClient(
        [
            _Resp(is_redirect=True, location="https://other.example.com/next"),
            _Resp(text="final content"),
        ]
    )
    with (
        patch("velune.tools.web.fetch.validate_url", return_value=(True, None)),
        patch("httpx.AsyncClient", return_value=client),
    ):
        result = await WebFetch().execute("https://public.example.com")
    assert result == "final content"


# ── R5: credential master-key hardening ─────────────────────────────────────


def test_passphrase_master_key_roundtrip(monkeypatch):
    from velune.providers import crypto

    monkeypatch.setattr(crypto, "_keyring_read_key", lambda: None)
    monkeypatch.setattr(crypto, "_keyring_create_key", lambda: None)
    monkeypatch.setenv("VELUNE_MASTER_PASSPHRASE", "a-strong-secret-passphrase")

    blob = crypto.encrypt_credentials('{"k": "v"}')
    assert crypto.decrypt_credentials(blob) == '{"k": "v"}'


def test_legacy_machine_key_store_still_decrypts(monkeypatch):
    from velune.providers import crypto

    monkeypatch.setattr(crypto, "_keyring_read_key", lambda: None)
    monkeypatch.setattr(crypto, "_keyring_create_key", lambda: None)

    # Encrypt with no keyring and no passphrase → legacy machine fallback key.
    monkeypatch.delenv("VELUNE_MASTER_PASSPHRASE", raising=False)
    blob = crypto.encrypt_credentials('{"legacy": true}')

    # A passphrase later appears; the store must still open via candidate keys.
    monkeypatch.setenv("VELUNE_MASTER_PASSPHRASE", "newly-configured-secret")
    assert crypto.decrypt_credentials(blob) == '{"legacy": true}'


# ── R7: reranker trust vocabulary ───────────────────────────────────────────


def test_reranker_trust_differentiates_real_sources():
    from velune.retrieval.reranker import HeuristicReranker

    r = HeuristicReranker()
    graph = r._calculate_trust_score(SimpleNamespace(source="graph"))
    vector = r._calculate_trust_score(SimpleNamespace(source="vector"))
    lexical = r._calculate_trust_score(SimpleNamespace(source="lexical"))
    unknown = r._calculate_trust_score(SimpleNamespace(source="something-else"))

    # The real hybrid labels are no longer stuck at the 0.5 default...
    assert graph > 0.5
    assert vector > 0.5
    assert lexical > 0.5
    # ...structural graph hits outrank plain keyword hits on trust...
    assert graph > lexical
    # ...an unrecognised source still defaults to 0.5.
    assert unknown == 0.5


def test_reranker_intent_boost_applies_to_real_graph_source():
    from velune.retrieval.reranker import HeuristicReranker

    r = HeuristicReranker()
    base = r._calculate_trust_score(SimpleNamespace(source="graph"))
    boosted = r._calculate_trust_score(SimpleNamespace(source="graph"), "dependency_analysis")
    assert boosted > base


# ── R9: BM25 small-corpus degeneracy (found during runtime verification) ─────


def _bm25_with(docs: dict[str, str]):
    from velune.retrieval.keyword import BM25Retriever
    from velune.retrieval.schemas import RetrievalDocument

    bm = BM25Retriever()
    bm.add_documents_batch(
        [RetrievalDocument(id=k, content=v, namespace="workspace") for k, v in docs.items()]
    )
    return bm


def test_bm25_matches_verbatim_tokens_in_tiny_corpus():
    """BM25Okapi's IDF is ≤ 0 for every term in a 1–2 document corpus, so the
    old `score > 0` filter silently dropped verbatim matches — lexical
    retrieval returned nothing at all in small workspaces."""
    bm = _bm25_with(
        {
            "app.py": "app.py list_users load_users health flask python",
            "store.py": "store.py connect fetch_all sqlite3 python",
        }
    )
    assert [h.document.id for h in bm.retrieve("list_users flask")] == ["app.py"]
    assert [h.document.id for h in bm.retrieve("sqlite3 connect")] == ["store.py"]
    # Single-document corpus — the most degenerate case (IDF strictly < 0).
    solo = _bm25_with({"only.py": "solitary_function helper"})
    assert [h.document.id for h in solo.retrieve("solitary_function")] == ["only.py"]


def test_bm25_still_returns_nothing_for_unmatched_query():
    bm = _bm25_with({"app.py": "list_users flask python"})
    assert bm.retrieve("completely unrelated tokens") == []


def test_bm25_scores_are_never_negative():
    bm = _bm25_with(
        {
            "a.py": "shared_term alpha",
            "b.py": "shared_term beta",
            "c.py": "shared_term gamma",
        }
    )
    hits = bm.retrieve("shared_term")  # df == N → IDF ≤ 0 for every doc
    assert len(hits) == 3
    assert all(h.score >= 0.0 for h in hits)


def test_bm25_natural_language_reaches_snake_case_symbols():
    """Identifier subword expansion: "list users" must reach list_users."""
    bm = _bm25_with(
        {
            "users.py": "def list_users(db): return db.query(User).all()",
            "orders.py": "def create_order(cart): pass",
        }
    )
    assert [h.document.id for h in bm.retrieve("list users")] == ["users.py"]


def test_bm25_natural_language_reaches_camel_case_symbols():
    bm = _bm25_with(
        {
            "config.py": "class HTTPServerConfig: maxRetryCount = 3",
            "auth.py": "class TokenValidator: pass",
        }
    )
    assert [h.document.id for h in bm.retrieve("max retry count")] == ["config.py"]
    assert [h.document.id for h in bm.retrieve("http server")] == ["config.py"]


def test_bm25_exact_identifier_query_still_works():
    """The whole identifier is still indexed — exact-symbol queries keep working."""
    bm = _bm25_with(
        {
            "users.py": "def list_users(db): pass",
            "misc.py": "users list here",
        }
    )
    hits = bm.retrieve("list_users")
    assert hits and hits[0].document.id == "users.py"
