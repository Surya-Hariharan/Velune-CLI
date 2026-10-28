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


# ── workspace switch rebinds the tools, not just the cognition ───────────────


async def _switch(runtime, ws):
    from velune.cli.workspaces import switch_workspace

    await switch_workspace(runtime.container, ws)


def _tool(runtime, name):
    return runtime.container.get("runtime.tool_registry").get(name)


async def test_switch_points_the_filesystem_tools_at_the_new_workspace(runtime_a, tmp_path):
    ws_b = _make_repo(tmp_path / "ws_b", SENTINEL_B)
    await _switch(runtime_a, ws_b)

    listing = await _tool(runtime_a, "read_directory").execute(".")
    assert f"{SENTINEL_B}.py" in listing and f"{SENTINEL_A}.py" not in listing

    found = await _tool(runtime_a, "find_files").execute("*.py")
    assert any(SENTINEL_B in str(f) for f in found)
    assert not any(SENTINEL_A in str(f) for f in found)

    hits = await _tool(runtime_a, "grep_files").execute(SENTINEL_B.lower())
    assert hits and all(SENTINEL_A.lower() not in str(h).lower() for h in hits)


async def test_switch_points_command_execution_and_git_at_the_new_workspace(runtime_a, tmp_path):
    ws_b = _make_repo(tmp_path / "ws_b", SENTINEL_B)
    await _switch(runtime_a, ws_b)

    assert Path(_tool(runtime_a, "execute_command")._workspace_path).resolve() == ws_b.resolve()
    assert Path(_tool(runtime_a, "git_status").workspace).resolve() == ws_b.resolve()
    executor = runtime_a.container.get("runtime.execution_executor")
    assert executor.workspace_path == ws_b.resolve()


async def test_switch_points_retrieval_and_the_graph_at_the_new_workspace(runtime_a, tmp_path):
    ws_b = _make_repo(tmp_path / "ws_b", SENTINEL_B)
    await _switch(runtime_a, ws_b)
    runtime_a.container.get("runtime.repository_cognition").index(force=True)

    hits = await _tool(runtime_a, "semantic_code_search").execute("zebra ledger total")
    paths = [str(h.get("path", "")) for h in hits]
    assert paths and all(str(tmp_path / "ws_a") not in p for p in paths)

    graph = runtime_a.container.get("runtime.knowledge_graph")
    assert Path(graph._db_path).resolve().is_relative_to(ws_b.resolve())


async def test_switch_registers_the_rebuilt_services_for_lifecycle_shutdown(runtime_a, tmp_path):
    """The old instance must not stay registered, or exit shuts down the wrong one."""
    ws_b = _make_repo(tmp_path / "ws_b", SENTINEL_B)
    await _switch(runtime_a, ws_b)

    lifecycle = runtime_a.container.get("runtime.lifecycle")
    assert lifecycle._components["retrieval"] is runtime_a.container.get("runtime.retrieval")


async def test_switching_back_restores_the_first_workspace(runtime_a, tmp_path):
    ws_a = tmp_path / "ws_a"
    ws_b = _make_repo(tmp_path / "ws_b", SENTINEL_B)
    await _switch(runtime_a, ws_b)
    await _switch(runtime_a, ws_a)

    listing = await _tool(runtime_a, "read_directory").execute(".")
    assert f"{SENTINEL_A}.py" in listing and f"{SENTINEL_B}.py" not in listing


async def test_a_b_a_leaves_every_service_and_the_council_on_the_first_workspace(
    runtime_a, tmp_path
):
    ws_a = tmp_path / "ws_a"
    ws_b = _make_repo(tmp_path / "ws_b", SENTINEL_B)
    await _switch(runtime_a, ws_b)
    assert SENTINEL_B in await _council_context(runtime_a)
    await _switch(runtime_a, ws_a)

    context = await _council_context(runtime_a)
    assert SENTINEL_A in context and SENTINEL_B not in context

    container = runtime_a.container
    assert Path(container.get("runtime.workspace")) == ws_a.resolve()
    assert container.get("runtime.execution_executor").workspace_path == ws_a.resolve()
    assert Path(_tool(runtime_a, "execute_command")._workspace_path).resolve() == ws_a.resolve()
    assert Path(_tool(runtime_a, "git_status").workspace).resolve() == ws_a.resolve()
    assert (
        Path(container.get("runtime.knowledge_graph")._db_path)
        .resolve()
        .is_relative_to(ws_a.resolve())
    )
    retriever = container.get("runtime.retrieval")
    assert Path(retriever._lexical_index_path).resolve().is_relative_to(ws_a.resolve())
    assert container.get("runtime.repository_cognition").workspace_root == ws_a.resolve()


async def test_the_read_boundary_follows_the_switch(runtime_a, tmp_path):
    """After the switch a read of the *previous* project is outside the workspace."""
    from velune.execution.path_guard import PathTraversalError

    ws_a = tmp_path / "ws_a"
    ws_b = _make_repo(tmp_path / "ws_b", SENTINEL_B)
    await _switch(runtime_a, ws_b)

    assert await _tool(runtime_a, "read_file").execute(f"{SENTINEL_B}.py")
    with pytest.raises((PathTraversalError, PermissionError)):
        await _tool(runtime_a, "read_file").execute(str(ws_a / f"{SENTINEL_A}.py"))


async def test_repeated_switching_stops_every_superseded_engine(runtime_a, tmp_path):
    engines = [runtime_a.container.get("runtime.intelligence_engine")]
    ws_a = tmp_path / "ws_a"
    ws_b = _make_repo(tmp_path / "ws_b", SENTINEL_B)
    for target in (ws_b, ws_a, ws_b, ws_a):
        await _switch(runtime_a, target)
        engines.append(runtime_a.container.get("runtime.intelligence_engine"))

    assert all(engine is not None for engine in engines)
    assert len({id(e) for e in engines}) == len(engines), "an engine was reused across workspaces"
    assert [e._running for e in engines] == [False, False, False, False, True]


async def test_shutdown_after_a_switch_stops_the_current_engine(runtime_a, tmp_path):
    await _switch(runtime_a, _make_repo(tmp_path / "ws_b", SENTINEL_B))
    engine = runtime_a.container.get("runtime.intelligence_engine")
    assert engine._running

    await runtime_a.container.get("runtime.lifecycle").shutdown()

    assert engine._running is False
    assert all(task.done() for task in engine._tasks)


async def test_a_service_that_fails_to_rebuild_is_retired_not_left_on_the_old_workspace(
    runtime_a, tmp_path, monkeypatch
):
    """A tool registry still bound to the previous project is worse than no registry."""
    import velune.tools.subsystems as tool_subsystems

    def boom(env):
        raise RuntimeError("cannot build tools")

    monkeypatch.setattr(tool_subsystems.TOOL_MODULES[0], "factory", boom, raising=False)
    ws_b = _make_repo(tmp_path / "ws_b", SENTINEL_B)

    notes = await _switch_notes(runtime_a, ws_b)

    assert any("tool_registry" in n for n in notes)
    assert runtime_a.container.get("runtime.tool_registry") is None
    # everything that did rebuild is on the new workspace
    assert runtime_a.container.get("runtime.execution_executor").workspace_path == ws_b.resolve()


async def _switch_notes(runtime, ws) -> list[str]:
    from velune.cli.workspaces import switch_workspace

    return await switch_workspace(runtime.container, ws)


async def test_the_superseded_command_sandbox_is_stopped(runtime_a, tmp_path):
    class Sandbox:
        stopped = 0

        def stop(self):
            Sandbox.stopped += 1

    runtime_a.container.get("runtime.execution_executor").sandbox = Sandbox()

    await _switch(runtime_a, _make_repo(tmp_path / "ws_b", SENTINEL_B))

    assert Sandbox.stopped == 1


async def test_runtime_does_not_publish_itself_into_the_process_wide_registry(runtime_a):
    """Wiring is by injection: a global would leak one runtime's services into another's."""
    from velune.kernel.registry import get_container

    assert not get_container().has("runtime.repository_cognition")
    assert not get_container().has("runtime.provider_registry")


async def test_switching_does_not_publish_into_the_process_wide_registry(runtime_a, tmp_path):
    await _switch(runtime_a, _make_repo(tmp_path / "ws_b", SENTINEL_B))
    for key in (
        "runtime.tool_registry",
        "runtime.retrieval",
        "runtime.knowledge_graph",
        "runtime.execution_executor",
        "runtime.intelligence_engine",
    ):
        from velune.kernel.registry import get_container

        assert not get_container().has(key), key


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
