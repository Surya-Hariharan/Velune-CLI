"""Home-directory and secret scrubbing for pasteable diagnostics.

Platform-specific path styles are exercised by passing ``home`` explicitly, so
the Windows cases run on Linux CI and vice versa — these are simulations of the
other OS's path syntax, not native runs.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from velune.core.privacy import redact_local_paths, sanitize_diagnostics

WIN_HOME = r"C:\Users\alice"
UNIX_HOME = "/home/alice"
MAC_HOME = "/Users/alice"


@pytest.mark.parametrize(
    ("text", "home", "expected"),
    [
        (r"C:\Users\alice\AppData\Local\Velune", WIN_HOME, r"~\AppData\Local\Velune"),
        ("C:/Users/alice/AppData/Local/Velune", WIN_HOME, "~/AppData/Local/Velune"),
        (r"c:\users\ALICE\x", WIN_HOME, r"~\x"),  # Windows paths are case-insensitive
        (r"\\?\C:\Users\alice\x", WIN_HOME, r"~\x"),  # extended-length prefix
        ("/home/alice/.local/bin/velune", UNIX_HOME, "~/.local/bin/velune"),
        ("/Users/alice/Library/x", MAC_HOME, "~/Library/x"),
        ("/c/Users/alice/bin", WIN_HOME, "~/bin"),  # Git Bash / MSYS style
    ],
)
def test_known_home_is_normalised(text, home, expected):
    assert redact_local_paths(text, home) == expected


def test_other_users_home_is_redacted_too():
    # An error message can mention a different account (shared machine, CI).
    assert redact_local_paths(r"cannot read C:\Users\bob\velune.toml", WIN_HOME) == (
        r"cannot read ~\velune.toml"
    )
    assert redact_local_paths("denied: /home/bob/data", UNIX_HOME) == "denied: ~/data"


def test_home_with_spaces_is_matched_via_the_literal_home():
    home = r"C:\Users\Alice Smith"
    assert redact_local_paths(rf"{home}\AppData\x", home) == r"~\AppData\x"


def test_username_is_not_replaced_outside_paths():
    # A bare word must survive: usernames like "user" would garble sentences.
    assert redact_local_paths("user alice cancelled", WIN_HOME) == "user alice cancelled"


def test_non_home_paths_are_left_alone():
    for text in (r"C:\Program Files\Python312\python.exe", "/usr/local/bin/python", "/opt/x"):
        assert redact_local_paths(text, WIN_HOME) == text


def test_home_prefix_is_not_a_partial_match_of_a_longer_name():
    # /home/alice2 is a different account from /home/alice; neither may leak.
    assert redact_local_paths("/home/alice2/x", UNIX_HOME) == "~/x"


def test_empty_and_none_like_inputs():
    assert redact_local_paths("", WIN_HOME) == ""
    assert redact_local_paths("plain text", "") == "plain text"
    assert redact_local_paths("/x", "/") == "/x"  # a root "home" is never matched


def test_sanitize_walks_nested_structures_and_keeps_shape():
    payload = {
        "environment": {"python_executable": r"C:\Users\alice\venv\Scripts\python.exe"},
        "ok": True,
        "count": 3,
        "checks": [
            {"name": "x", "message": r"cannot write C:\Users\alice\AppData\Local\Velune\db"},
            {"nested": ({"deep": "/home/alice/.velune"},)},
        ],
        "nothing": None,
    }
    out = sanitize_diagnostics(payload, WIN_HOME)
    assert out["environment"]["python_executable"] == r"~\venv\Scripts\python.exe"
    assert out["checks"][0]["message"] == r"cannot write ~\AppData\Local\Velune\db"
    assert out["checks"][1]["nested"][0]["deep"] == "~/.velune"
    assert (out["ok"], out["count"], out["nothing"]) == (True, 3, None)
    json.dumps(out)  # still serialisable
    assert "alice" not in json.dumps(out)


def test_sanitize_does_not_mutate_the_input():
    payload = {"p": r"C:\Users\alice\x"}
    sanitize_diagnostics(payload, WIN_HOME)
    assert payload == {"p": r"C:\Users\alice\x"}


def test_credential_shaped_text_is_redacted_in_diagnostics():
    key = "sk-" + "a" * 30
    out = sanitize_diagnostics({"message": f"401 for key {key}"}, WIN_HOME)
    assert key not in out["message"]


def test_doctor_json_never_contains_the_real_home(monkeypatch, tmp_path):
    """End to end: `velune doctor --json` with a fake home must not leak it."""
    from velune.cli.commands import doctor as doctor_mod

    home = tmp_path / "alice_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(
        doctor_mod,
        "_environment_summary",
        lambda: {
            "python_executable": str(home / "venv" / "python"),
            "data_dir": str(home / ".velune"),
        },
    )
    monkeypatch.setattr(
        doctor_mod,
        "_run_checks",
        lambda _checks: [
            {
                "name": "Velune Data Directories Writable",
                "status": "fail",
                "message": f"cannot write {home / '.velune' / 'x.db'}",
            }
        ],
    )
    monkeypatch.setattr(doctor_mod, "_all_checks", lambda: [])

    result = CliRunner().invoke(doctor_mod.doctor_cmd, ["--json"])
    # Core check failed on purpose -> exit 1, but stdout must still be valid JSON.
    assert result.exit_code == 1, result.output
    data = json.loads(result.stdout)
    assert str(home) not in result.stdout
    assert "alice_home" not in result.stdout
    assert data["environment"]["python_executable"].startswith("~")
    assert data["environment"]["data_dir"].replace("\\", "/") == "~/.velune"
    assert "checks" in data
