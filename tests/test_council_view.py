"""Regression test for the CouncilDisplayView header markup bug.

`render_header` used to build its body with `Text.assemble()` given
markup-syntax strings instead of parsed `Text` — this printed literal
`[bold #ff5fa2]...[/bold]` brackets AND silently dropped the actual
task/objective text (misparsed as a bogus style argument instead of content).
"""

from __future__ import annotations

from rich.console import Console

from velune.cli.display.council_view import CouncilDisplayView


def _rendered(task: str) -> str:
    console = Console(force_terminal=False, color_system=None, width=100)
    with console.capture() as capture:
        CouncilDisplayView(console).render_header(task)
    return capture.get()


def test_render_header_shows_objective_text():
    output = _rendered("Explain recursion in one sentence.")
    assert "Explain recursion in one sentence." in output


def test_render_header_does_not_leak_markup_syntax():
    output = _rendered("Explain recursion in one sentence.")
    assert "[bold" not in output
    assert "[/bold" not in output
    assert "VELUNE COGNITIVE OS" in output


def test_render_header_task_with_brackets_is_not_parsed_as_markup():
    # task is user-controlled; literal '[' must render as text, never be
    # misread as the start of a markup tag (and must not raise).
    task = "Explain list comprehension using [x for x in y] syntax."
    output = _rendered(task)
    assert "[x for x in y]" in output


# ── render_reviewer_report / render_challenger_report: None handling ────────
#
# Regression test for a real crash: CouncilOrchestrator's tier<3 early return
# leaves reviewer_report/challenger_report as None by design (not an error),
# but the view previously assumed a report object was always present and
# crashed with `AttributeError: 'NoneType' object has no attribute 'passed'`.


def _capture(fn, *args) -> str:
    console = Console(force_terminal=False, color_system=None, width=100)
    with console.capture() as capture:
        fn(CouncilDisplayView(console), *args)
    return capture.get()


def test_render_reviewer_report_none_does_not_crash():
    output = _capture(lambda view: view.render_reviewer_report(None))
    assert "did not run" in output.lower()


def test_render_challenger_report_none_does_not_crash():
    output = _capture(lambda view: view.render_challenger_report(None))
    assert "did not run" in output.lower()


def test_render_reviewer_report_dict_still_renders_normally():
    report = {"passed": False, "confidence_rating": 0.4, "critical_issues": ["bad thing"]}
    output = _capture(lambda view: view.render_reviewer_report(report))
    assert "bad thing" in output
    assert "FAIL" in output


def test_render_challenger_report_object_still_renders_normally():
    class _FakeChallengerReport:
        severity_rating = 0.9
        failure_vectors = ["race condition under load"]

    output = _capture(lambda view: view.render_challenger_report(_FakeChallengerReport()))
    assert "race condition under load" in output
