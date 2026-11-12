"""A bare ``python`` is only runnable through the sandbox when it is trusted.

Regression for tests that failed on any machine whose first ``python`` on PATH was not the
interpreter running the suite: the sandbox correctly rejected it as untrusted, and the tests
(which are about cancellation / background jobs) never ran. These two tests pin both halves:
the fixture used by those tests yields a trusted interpreter, and the policy still rejects an
interpreter that is not.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from velune.core.errors.execution import SandboxError
from velune.execution.command_spec import CommandSpec, _is_trusted_path
from velune.execution.sandbox import SubprocessSandbox


def test_fixture_makes_bare_python_resolve_to_a_trusted_interpreter(
    interpreter_under_test_on_path, tmp_path
):
    found = shutil.which("python")
    assert found is not None
    resolved = Path(found).resolve()
    assert _is_trusted_path(resolved, workspace=tmp_path), resolved
    assert Path(sys.executable).parent == Path(found).parent


def test_an_untrusted_python_earlier_on_path_is_still_refused(monkeypatch, tmp_path):
    """The policy is unchanged: a planted ``python`` in an arbitrary directory is rejected."""
    planted_dir = tmp_path / "planted"
    planted_dir.mkdir()
    name = "python.exe" if sys.platform == "win32" else "python"
    planted = planted_dir / name
    planted.write_bytes(b"")
    planted.chmod(0o755)
    monkeypatch.setenv("PATH", str(planted_dir) + os.pathsep + os.environ.get("PATH", ""))
    (tmp_path / "x.py").write_text("print('hi')\n", encoding="utf-8")
    spec = CommandSpec.from_string("python x.py", cwd=tmp_path, timeout=10.0)
    with pytest.raises(SandboxError, match="untrusted"):
        SubprocessSandbox(tmp_path).execute(spec)
