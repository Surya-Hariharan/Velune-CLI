"""The council and the code tools get *their runtime's* services (audit finding A1).

The REPL builds its own ``ServiceContainer``. Council orchestration and the code
tools used to read the process-wide registry instead -- which nothing ever
populates -- so the council ran with "Repository context unavailable." These
tests build the real runtime through the normal production path (``build_runtime``)
and never touch ``velune.kernel.registry``: a test that registered services into
the global container would pass whether or not the product was wired.

Isolation: HOME / USERPROFILE / app-data point at a temp directory, and a socket
guard fails the test on any non-loopback network attempt, so initialising the
runtime is proven not to probe a provider or spend anything.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

from velune.core.runtime import build_runtime

SENTINEL_A = "VELUNE_REPO_CONTEXT_SENTINEL_ALPHA"
SENTINEL_B = "VELUNE_REPO_CONTEXT_SENTINEL_BETA"
UNAVAILABLE = "Repository context unavailable."


def _make_repo(root: Path, sentinel: str) -> Path:
    root.mkdir(parents=True)
    (root / f"{sentinel}.py").write_text(
        f"def {sentinel.lower()}_entry():\n    return 1\n", encoding="utf-8"
    )
    (root / "zebra_ledger.py").write_text(
        "def zebra_ledger_total(items):\n    return sum(items)\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Local").mkdir(parents=True)
    (home / "Roaming").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "Local"))
    monkeypatch.setenv("APPDATA", str(home / "Roaming"))
    monkeypatch.setenv("VELUNE_DATA_HOME", str(tmp_path / "data"))
    for key in list(__import__("os").environ):
        if key.endswith("_API_KEY"):
            monkeypatch.delenv(key)
    return home


@pytest.fixture
def network(monkeypatch):
    """Record every non-loopback connection or DNS lookup; refuse it."""
    attempts: list[str] = []
    loopback = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}  # noqa: S104

    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in loopback:
            attempts.append(f"connect {address}")
            raise OSError("network disabled by the test")
        return real_connect(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if host not in loopback and host is not None:
            attempts.append(f"dns {host}")
            raise socket.gaierror("network disabled by the test")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return attempts


@pytest.fixture
def runtime_a(tmp_path, isolated_home, network):
    return build_runtime(_make_repo(tmp_path / "ws_a", SENTINEL_A))


async def _council_context(runtime, prompt="where is the entry point?") -> str:
    """The ``repo_context`` the council's seats are handed for *prompt*."""
    orchestrator = runtime.container.get("runtime.council_orchestrator")
    seen: dict[str, str] = {}

    async def capture(prompt, repo_context, **kwargs):
        seen["context"] = repo_context
        return {"final_summary": "stub", "task_plan": None, "coder_proposal": None}

    orchestrator.execute_task = capture  # no model is ever called
    async for _ in orchestrator.stream(prompt, council_tier="full"):
        pass
    return seen["context"]


# ── council context ──────────────────────────────────────────────────────────


async def test_council_context_contains_the_repository_evidence(runtime_a):
    context = await _council_context(runtime_a)
    assert SENTINEL_A in context
    assert UNAVAILABLE not in context
    assert "zebra_ledger.py" in context


async def test_council_context_describes_the_runtimes_workspace_not_the_cwd(
    runtime_a, tmp_path, monkeypatch
):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # a different cwd must not redirect git/config context
    context = await _council_context(runtime_a)
    assert SENTINEL_A in context


async def test_a_second_runtime_never_sees_the_first_workspaces_context(
    runtime_a, tmp_path, isolated_home, network
):
    runtime_b = build_runtime(_make_repo(tmp_path / "ws_b", SENTINEL_B))
    context_b = await _council_context(runtime_b)
    context_a = await _council_context(runtime_a)

    assert SENTINEL_B in context_b and SENTINEL_A not in context_b
    assert SENTINEL_A in context_a and SENTINEL_B not in context_a


async def test_a_workspace_switch_is_reflected_in_the_next_council_context(
    runtime_a, tmp_path, network
):
    from velune.cli.workspaces import switch_workspace

    ws_b = _make_repo(tmp_path / "ws_b", SENTINEL_B)
    await switch_workspace(runtime_a.container, ws_b)

    context = await _council_context(runtime_a)
    assert SENTINEL_B in context and SENTINEL_A not in context


async def test_runtime_does_not_publish_itself_into_the_process_wide_registry(runtime_a):
    """Wiring is by injection: a global would leak one runtime's services into another's."""
    from velune.kernel.registry import get_container

    assert not get_container().has("runtime.repository_cognition")
    assert not get_container().has("runtime.provider_registry")


# ── code tools ───────────────────────────────────────────────────────────────


def test_code_tools_are_registered_with_the_runtime_container(runtime_a):
    registry = runtime_a.container.get("runtime.tool_registry")
    for name in ("semantic_code_search", "symbol_search", "go_to_definition"):
        assert registry.get(name)._container is runtime_a.container, name


async def test_semantic_code_search_goes_through_the_runtime_retriever(runtime_a):
    runtime_a.container.get("runtime.repository_cognition").index(force=True)
    tool = runtime_a.container.get("runtime.tool_registry").get("semantic_code_search")

    # "zebra ledger total" is not a substring of anything on disk: a grep (the
    # fallback used when the retriever is unreachable) finds nothing. Only the
    # BM25 index's identifier-aware tokenisation maps it to zebra_ledger_total.
    hits = await tool.execute("zebra ledger total")

    assert hits, "the tool fell back to grep instead of using the runtime retriever"
    assert any("zebra_ledger" in str(h.get("path", "")) for h in hits)
    assert all(h.get("source") for h in hits)  # retriever hits carry their source leg


async def test_symbol_search_consults_the_runtime_knowledge_graph(runtime_a):
    tool = runtime_a.container.get("runtime.tool_registry").get("symbol_search")
    queried: list[str] = []
    graph = runtime_a.container.get("runtime.knowledge_query")

    async def spy(label):
        queried.append(label)
        return []

    graph.find_by_label = spy
    assert await tool.execute("zebra_ledger_total") == []
    assert queried == ["zebra_ledger_total"]  # answered by the graph, not a file re-scan


async def test_go_to_definition_consults_the_runtime_knowledge_graph(runtime_a):
    tool = runtime_a.container.get("runtime.tool_registry").get("go_to_definition")
    queried: list[str] = []
    graph = runtime_a.container.get("runtime.knowledge_query")

    async def spy(label):
        queried.append(label)
        return []

    graph.find_by_label = spy
    await tool.execute("zebra_ledger_total", "zebra_ledger.py", 1)
    assert queried == ["zebra_ledger_total"]


# ── side effects ─────────────────────────────────────────────────────────────


async def test_initialisation_and_a_council_run_make_no_external_calls(runtime_a, network):
    await _council_context(runtime_a)
    tool = runtime_a.container.get("runtime.tool_registry").get("semantic_code_search")
    await tool.execute("zebra ledger total")
    assert network == [], f"unexpected network activity: {network}"


async def test_wiring_does_not_turn_on_model_probes(runtime_a, tmp_path, monkeypatch):
    """Each probe is a real completion request. Wiring the council must not enable them.

    A fake cloud provider is registered in the runtime and a cloud model is handed
    to the registry's probe step; if the registry could now resolve providers
    (e.g. because the runtime had been published as the process-wide container) it
    would ping the model.
    """
    from tests.council_fakes import FakeProvider, make_model
    from velune.models.probes import FastProbe

    pings: list[str] = []

    async def ping(self, provider, model_id):  # pragma: no cover - must not run
        pings.append(model_id)
        return True

    monkeypatch.setattr(FastProbe, "ping", ping)
    monkeypatch.chdir(tmp_path)  # the probe cache path is cwd-relative
    runtime_a.container.get("runtime.provider_registry").register(
        "fakecloud", FakeProvider("fakecloud")
    )
    model = make_model("cloud-model", "fakecloud", is_local=False)
    await runtime_a.container.get("runtime.model_registry")._probe([model])
    assert pings == []
