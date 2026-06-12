from __future__ import annotations

from velune.cli.commands.doctor import _render_results


def test_doctor_table_grouping():
    from velune.cli.commands.doctor import console as doctor_console

    results = [
        {"name": "Internet Connectivity", "status": "ok", "message": "Online"},
        {"name": "SQLite DB Initializable", "status": "ok", "message": "Success"},
        {"name": "OpenAI API Key", "status": "warn", "message": "Not configured"},
        {"name": "Python Version", "status": "ok", "message": "3.11.0"},
    ]

    with doctor_console.capture() as capture:
        _render_results(results)
    output = capture.get()

    # Verify that the category headers / groupings are in the output
    assert "Providers" in output
    assert "Storage" in output
    assert "Security" in output
    assert "Performance" in output

    # Verify that check names are rendered
    assert "Internet Connectivity" in output
    assert "SQLite DB Initializable" in output
    assert "OpenAI API Key" in output
    assert "Python Version" in output
