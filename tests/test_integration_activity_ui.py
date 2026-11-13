"""How integration tools read in the REPL's activity rows (VAIR A2)."""

from __future__ import annotations

import json

import pytest

from velune.cli.handlers.tool_chat import _describe_call, _summarize_integration, _summarize_result


def row(result: str, error: bool = False):
    return _summarize_result("docker__ps", {"result": result, "error": error}, None, "")


@pytest.mark.parametrize(
    ("name", "args", "target"),
    [
        ("docker__stop", {"container": "web-1"}, "web-1"),
        ("process__logs", {"job_id": "job-0001", "tail": 50}, "job-0001"),
        ("integrations_enable", {"integration": "docker"}, "docker"),
        ("postgres__query", {"sql": "SELECT  1\n FROM t"}, "SELECT 1 FROM t"),
        ("supabase__inspect_schema", {"table": "users"}, "users"),
        ("docker__inspect", {"target": "img:1"}, "img:1"),
        ("docker__images", {}, ""),
    ],
)
def test_the_card_shows_what_is_being_acted_on(name, args, target):
    assert _describe_call(name, args, None) == (name, target)


def test_a_successful_result_reads_as_one_line():
    body = json.dumps({"ok": True, "integration": "docker", "action": "ps", "data": [1, 2]})
    assert row(body)[0][1] == "docker ps: ok"


def test_a_failure_shows_its_code_and_message():
    body = json.dumps(
        {
            "ok": False,
            "integration": "docker",
            "action": "stop",
            "error": {"code": "connection_failed", "message": 'the "app" went away'},
        }
    )
    style, text = row(body)[0]
    assert "err" in style and text == 'docker stop: connection_failed: the "app" went away'


def test_a_truncated_payload_still_summarizes():
    """The event payload is cut at 2000 characters, so the JSON may be incomplete."""
    body = json.dumps(
        {"ok": True, "integration": "postgres", "action": "query", "data": "x" * 5000}
    )
    assert row(body[:2000])[0][1] == "postgres query: ok"


def test_enable_results_read_naturally():
    on = json.dumps({"enabled": True, "integration": "docker", "tools": []})
    off = json.dumps({"enabled": False, "integration": "docker", "state": "unavailable"})
    assert row(on)[0][1] == "Enabled docker"
    style, text = row(off)[0]
    assert text == "Could not enable docker" and "err" in style


def test_other_results_are_untouched():
    assert _summarize_integration("plain text") is None
    assert _summarize_integration('{"something": "else"}') is None
    assert row("plain text output")[0][1] == "plain text output"


def test_loop_level_errors_still_win():
    style, text = row("Error: the user denied permission to run 'docker__stop'.", error=True)[0]
    assert "err" in style and text.startswith("Error: the user denied")
