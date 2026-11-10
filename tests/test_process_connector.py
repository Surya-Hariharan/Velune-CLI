"""The `process` integration: operate background commands the agent started (VAIR A2).

Visibility, identity and authorization are enforced by the connector and the
gate, not by instruction to the model: only the agent's own shell jobs exist as
far as these tools can tell, they are addressed by job id never by pid, and
stopping goes through the A1 process-identity protections.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from tests.integration_fakes import ScriptedProvider, call, final, make_env, turn
from velune.core.redaction import register_secret_value, unregister_secret_value
from velune.core.task_registry import JobRecord, JobRegistry, JobStatus, ProcessLog
from velune.execution.processes import process_alive
from velune.permissions.gate import Approval
from velune.permissions.policy import ExecutionMode

# ── Fixtures ────────────────────────────────────────────────────────────────


def job(
    registry: JobRegistry,
    job_id: str = "job-0001",
    *,
    kind: str = "shell",
    owner: str = "agent",
    status: JobStatus = JobStatus.RUNNING,
    lines: tuple[str, ...] = (),
    command: str = "python server.py",
    **extra,
) -> JobRecord:
    record = JobRecord(
        job_id,
        f"shell:{command}"[:40],
        kind=kind,
        status=status,
        command=command,
        log=ProcessLog(),
        owner=owner,
        **extra,
    )
    for line in lines:
        record.log.append("stdout", line + "\n")
    registry.register(record)
    return record


async def run(env, action: str, **params):
    result = await env.manager.execute("process", action, params)
    return result


@pytest.fixture
def registry() -> JobRegistry:
    return JobRegistry()


@pytest.fixture
def env(tmp_path, registry):
    return make_env(tmp_path, ExecutionMode.AUTO, connectors={}, job_registry=registry)


# ── Visibility ──────────────────────────────────────────────────────────────


async def test_only_the_agents_own_shell_jobs_are_visible(env, registry):
    job(registry, "job-0001")
    job(registry, "job-0002", owner="user")
    job(registry, "job-0003", owner="")
    job(registry, "job-0004", kind="task")
    job(registry, "job-0005", kind="cognition")
    result = await run(env, "list")
    assert [j["job_id"] for j in result.data["jobs"]] == ["job-0001"]
    assert result.data["count"] == 1


async def test_other_jobs_are_indistinguishable_from_missing_ones(env, registry):
    job(registry, "job-0002", owner="user")
    job(registry, "job-0004", kind="task")
    answers = []
    for jid in ("job-0002", "job-0004", "job-0099"):
        for action in ("status", "logs", "wait_ready", "stop"):
            r = await run(env, action, job_id=jid)
            assert not r.ok and r.code.value == "not_found"
            answers.append(r.error.replace(jid, "X"))
    assert len(set(answers)) == 1  # no probing of other jobs


async def test_a_job_that_is_not_the_agents_can_never_be_stopped(env, registry):
    record = job(registry, "job-0002", owner="user")
    assert not (await run(env, "stop", job_id="job-0002")).ok
    assert record.status is JobStatus.RUNNING


@pytest.mark.parametrize("bad", ["", "1", "job-", "../etc", "job-1; rm -rf", 5, None, "JOB-0001"])
async def test_malformed_job_ids_are_rejected(env, bad):
    r = await run(env, "status", job_id=bad)
    assert not r.ok and r.code.value == "validation_failed"


async def test_a_pid_is_never_shown_or_accepted(env, registry):
    job(registry, "job-0001", pid=os.getpid(), pid_started=time.time())
    dumped = json.dumps(
        [
            (await run(env, "list")).to_dict(),
            (await run(env, "status", job_id="job-0001")).to_dict(),
            (await run(env, "logs", job_id="job-0001")).to_dict(),
        ]
    )
    assert "pid" not in dumped.lower() and str(os.getpid()) not in dumped
    for key in ("pid", "process_id"):  # an extra argument is simply ignored by the connector
        r = await run(env, "stop", job_id="job-0999", **{key: os.getpid()})
        assert not r.ok
    assert process_alive(os.getpid())


async def test_unknown_actions_are_unsupported(env):
    connector = env.manager.get("process")
    r = await connector.execute("kill", {"pid": 1})
    assert not r.ok and r.code.value == "unsupported_operation"


# ── Status, logs, readiness ─────────────────────────────────────────────────


async def test_status_describes_a_running_and_a_finished_job(env, registry):
    job(registry, "job-0001")
    done = job(registry, "job-0002", status=JobStatus.COMPLETED, exit_code=0, ready=True)
    done.completed_at = done.submitted_at + 5
    running = (await run(env, "status", job_id="job-0001")).data
    assert running["running"] is True and running["status"] == "running"
    assert running["command"] == "python server.py" and "exit_code" not in running
    finished = (await run(env, "status", job_id="job-0002")).data
    assert finished["running"] is False and finished["exit_code"] == 0 and finished["ready"] is True
    assert finished["seconds"] == 5.0


async def test_status_redacts_secrets_in_the_command(env, registry):
    job(registry, "job-0001", command="serve --api_key=abcd1234efgh")
    data = (await run(env, "status", job_id="job-0001")).data
    assert "abcd1234efgh" not in data["command"]


async def test_logs_return_the_latest_lines_and_honour_tail_and_stream(env, registry):
    record = job(registry, "job-0001", lines=[f"line {i}" for i in range(10)])
    record.log.append("stderr", "warning!\n")
    data = (await run(env, "logs", job_id="job-0001", tail=3)).data
    assert data["lines"] == ["line 8", "line 9", "[stderr] warning!"]
    only_err = (await run(env, "logs", job_id="job-0001", stream="stderr")).data
    assert only_err["lines"] == ["warning!"]
    default = (await run(env, "logs", job_id="job-0001")).data
    assert len(default["lines"]) == 11


async def test_log_arguments_are_bounded_and_validated(env, registry):
    job(registry, "job-0001", lines=[f"l{i}" for i in range(700)])
    huge = (await run(env, "logs", job_id="job-0001", tail=10**9)).data
    assert len(huge["lines"]) == 500
    zero = (await run(env, "logs", job_id="job-0001", tail=0)).data
    assert len(zero["lines"]) == 1  # clamped, never "everything"
    bad = await run(env, "logs", job_id="job-0001", tail="many")
    assert bad.code.value == "validation_failed"
    bad = await run(env, "logs", job_id="job-0001", stream="stdin")
    assert bad.code.value == "validation_failed"


async def test_log_lines_are_truncated_and_dropped_lines_reported(env, registry):
    record = job(registry, "job-0001")
    record.log = ProcessLog(max_lines=3, max_chars=10_000)
    for i in range(6):
        record.log.append("stdout", f"{i}" + "x" * 900 + "\n")
    data = (await run(env, "logs", job_id="job-0001")).data
    assert all(len(line) <= 500 for line in data["lines"])
    assert data["older_lines_dropped"] == 3


async def test_logs_are_redacted_before_they_reach_the_model(env, registry):
    register_secret_value("db-pass-98765")
    try:
        job(
            registry,
            "job-0001",
            lines=[
                "connecting with db-pass-98765",
                "dsn postgres://app:urlpass123@db/x",
                "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123",
            ],
        )
        dumped = json.dumps((await run(env, "logs", job_id="job-0001")).to_dict())
        for secret in ("db-pass-98765", "urlpass123", "abcdefghijklmnopqrstuvwxyz0123"):
            assert secret not in dumped
    finally:
        unregister_secret_value("db-pass-98765")


async def test_a_job_without_a_log_returns_no_lines(env, registry):
    record = job(registry, "job-0001")
    record.log = None
    assert (await run(env, "logs", job_id="job-0001")).data["lines"] == []


async def test_wait_ready_matches_literal_text_not_a_regex(env, registry):
    record = job(registry, "job-0001", lines=["aXXb"])
    miss = (await run(env, "wait_ready", job_id="job-0001", log_contains="a.*b", timeout=1)).data
    assert miss["ready"] is False and "log pattern not seen" in miss["reason"]
    record.log.append("stdout", "a.*b is printed\n")
    hit = (await run(env, "wait_ready", job_id="job-0001", log_contains="a.*b", timeout=1)).data
    assert hit["ready"] is True
    assert registry.get("job-0001").ready is True


async def test_a_catastrophic_pattern_cannot_hang_the_readiness_check(env, registry):
    job(registry, "job-0001", lines=["a" * 5000 + "!"])
    started = time.monotonic()
    data = (await run(env, "wait_ready", job_id="job-0001", log_contains="(a+)+$", timeout=1)).data
    assert data["ready"] is False and time.monotonic() - started < 5


async def test_wait_ready_checks_a_local_port_only(env, registry):
    import socket

    job(registry, "job-0001")
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    try:
        port = server.getsockname()[1]
        assert (await run(env, "wait_ready", job_id="job-0001", port=port, timeout=2)).data["ready"]
    finally:
        server.close()
    # There is no host parameter: the schema cannot be used to probe anything else.
    manifest = {c.action: c for c in env.manager.get("process").capabilities()}["wait_ready"]
    assert "host" not in manifest.params["properties"]


async def test_wait_ready_clamps_the_timeout(env, registry):
    job(registry, "job-0001")
    started = time.monotonic()
    r = await run(env, "wait_ready", job_id="job-0001", log_contains="never", timeout=0)
    assert (
        r.data["ready"] is False and time.monotonic() - started < 5
    )  # 0 → at least 1s, not forever


async def test_wait_ready_reports_an_exited_process(env, registry):
    job(registry, "job-0001", status=JobStatus.FAILED, exit_code=2, lines=["server ready"])
    data = (await run(env, "wait_ready", job_id="job-0001", log_contains="server ready")).data
    assert data["ready"] is False and "exit code 2" in data["reason"]


# ── Stop ────────────────────────────────────────────────────────────────────


async def test_stop_cancels_a_running_job(env, registry):
    import threading

    record = job(registry, "job-0001", cancel_event=threading.Event())
    result = await run(env, "stop", job_id="job-0001")
    assert result.ok and result.data["stopped"] is True and result.resource_ids == ["job-0001"]
    assert record.cancel_event.is_set() and record.status is JobStatus.CANCELLED


async def test_stopping_a_finished_job_changes_nothing(env, registry):
    record = job(registry, "job-0001", status=JobStatus.COMPLETED, exit_code=0, pid=os.getpid())
    result = await run(env, "stop", job_id="job-0001")
    assert result.ok and result.data["already_finished"] is True
    assert record.status is JobStatus.COMPLETED and record.exit_code == 0
    assert process_alive(os.getpid())  # a recycled pid is never signalled


# ── Authorization ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", list(ExecutionMode))
async def test_reads_run_in_every_mode(tmp_path, registry, mode):
    env = make_env(tmp_path, mode, connectors={}, job_registry=registry)
    job(registry, "job-0001", lines=["server ready"])
    for action, params in (
        ("list", {}),
        ("status", {"job_id": "job-0001"}),
        ("logs", {"job_id": "job-0001"}),
        ("wait_ready", {"job_id": "job-0001", "log_contains": "ready", "timeout": 1}),
    ):
        assert (await env.manager.execute("process", action, params)).ok, action


async def test_stop_follows_the_execution_mode(tmp_path, registry):
    async def ask(tool, decision):
        return Approval.ALLOW_ONCE

    plan = make_env(tmp_path, ExecutionMode.PLAN, connectors={}, job_registry=registry)
    job(registry, "job-0001")
    refused = await plan.manager.execute("process", "stop", {"job_id": "job-0001"})
    assert refused.denied and registry.get("job-0001").status is JobStatus.RUNNING
    assert plan.records[-1]["actions"][0]["scope"] == "process.job.stop"

    manual = make_env(tmp_path, ExecutionMode.MANUAL, connectors={}, job_registry=registry)
    assert (await manual.manager.execute("process", "stop", {"job_id": "job-0001"})).denied

    asking = make_env(tmp_path, ExecutionMode.MANUAL, ask=ask, connectors={}, job_registry=registry)
    assert (await asking.manager.execute("process", "stop", {"job_id": "job-0001"})).ok
    assert registry.get("job-0001").status is JobStatus.CANCELLED


async def test_the_process_integration_exists_only_with_a_job_registry(tmp_path):
    bare = make_env(tmp_path, connectors={})
    assert bare.manager.get("process") is None
    from velune.resources.manager import ResourceManager

    manager = ResourceManager()
    manager.register_defaults()
    assert "process" not in manager.list_ids()


async def test_attaching_and_detaching_the_registry(tmp_path):
    from velune.resources.manager import ResourceManager

    manager = ResourceManager()
    manager.attach_job_registry(JobRegistry())
    assert manager.get("process") is not None
    manager.attach_job_registry(None)
    assert manager.get("process") is None


# ── Through the model: discover → enable → operate a real process ───────────


@pytest.fixture
def trusted_python(monkeypatch):
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])


async def _start_server(tmp_path, registry) -> str:
    from velune.execution.sandbox import SubprocessSandbox
    from velune.tools.safety import ApprovalMode
    from velune.tools.terminal.execute import ExecuteCommand

    (tmp_path / "srv.py").write_text(
        "import time\nprint('server ready', flush=True)\n"
        "for i in range(600):\n    print(f'tick {i}', flush=True)\n    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    tool = ExecuteCommand(
        sandbox=SubprocessSandbox(tmp_path),
        workspace_path=str(tmp_path),
        approval_mode=ApprovalMode.SAFE,
        job_registry=registry,
    )
    return (await tool.execute("python srv.py", background=True))["job_id"]


async def test_the_model_can_run_status_logs_readiness_and_stop_on_a_real_process(
    tmp_path, trusted_python
):
    registry = JobRegistry()
    job_id = await _start_server(tmp_path, registry)
    env = make_env(tmp_path, ExecutionMode.AUTO, connectors={}, job_registry=registry)
    provider = ScriptedProvider(
        turn(call("integrations_enable", "e1", integration="process")),
        turn(
            call(
                "process__wait_ready", "w1", job_id=job_id, log_contains="server ready", timeout=20
            )
        ),
        turn(call("process__logs", "g1", job_id=job_id, tail=5)),
        turn(call("process__status", "s1", job_id=job_id)),
        turn(call("process__stop", "x1", job_id=job_id)),
        turn(call("process__status", "s2", job_id=job_id)),
        final("stopped"),
    )
    result = await env.run(provider, "stop the dev server job")
    by_id = {i.call.id: json.loads(i.result) for i in result.invocations}

    assert by_id["w1"]["data"]["ready"] is True
    assert any(
        line.startswith("tick") or line == "server ready" for line in by_id["g1"]["data"]["lines"]
    )
    assert by_id["s1"]["data"]["running"] is True and "pid" not in json.dumps(by_id["s1"])
    assert by_id["x1"]["data"]["stopped"] is True
    assert by_id["s2"]["data"]["status"] == "cancelled"

    record = registry.get(job_id)
    pid = record.pid
    done, _ = await asyncio.wait({record.task}, timeout=15)
    assert done and not process_alive(pid)  # the whole process is gone
    scopes = [r["actions"][0]["scope"] for r in env.records if r["tool"].startswith("process__")]
    assert "process.job.stop" in scopes and "process.job.logs" in scopes


async def test_only_the_integrations_the_model_asked_for_are_exposed(tmp_path):
    registry = JobRegistry()
    env = make_env(tmp_path, connectors={}, job_registry=registry)
    provider = ScriptedProvider(final())
    await env.run(provider)
    assert provider.tool_names[0] == ["integrations_list", "integrations_enable"]
    desc = {t["function"]["name"]: t["function"]["description"] for t in provider.requests[0].tools}
    assert "process" in desc["integrations_enable"]
