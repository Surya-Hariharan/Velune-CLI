#!/usr/bin/env python3
"""
Run every check required before publishing to PyPI.
Must exit 0 before any tag is pushed.
"""
from __future__ import annotations

import glob
import re
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

# Force UTF-8 output so this script works correctly when captured by a parent
# process on Windows (pipes default to cp1252 otherwise).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CHECKS: list[tuple[str, object]] = []


def check(label: str):
    def decorator(fn):
        CHECKS.append((label, fn))
        return fn
    return decorator


@check("Python version is 3.11+")
def check_python():
    assert sys.version_info >= (3, 11), f"Need 3.11+, got {sys.version}"


@check("velune/__init__.py has __version__")
def check_version() -> str:
    init = Path("velune/__init__.py").read_text()
    assert "__version__" in init
    version = re.search(r'__version__\s*=\s*["\'](.+?)["\']', init)
    assert version, "Could not parse __version__"
    v = version.group(1)
    assert re.match(r"^\d+\.\d+\.\d+", v), f"Invalid version: {v}"
    return v


@check("CHANGELOG.md has entry for this version")
def check_changelog():
    init = Path("velune/__init__.py").read_text()
    version = re.search(r'__version__\s*=\s*["\'](.+?)["\']', init).group(1)
    changelog = Path("CHANGELOG.md").read_text()
    assert f"[{version}]" in changelog, f"No changelog entry for {version}"


@check("All tests pass")
def check_tests():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short", "--timeout=30"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"Tests failed:\n{result.stdout[-2000:]}"


@check("Ruff linting passes")
def check_ruff():
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "velune/"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"Ruff errors:\n{result.stdout}"


@check("Security audit passes")
def check_security():
    result = subprocess.run(
        [sys.executable, "scripts/security_audit.py"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"Security issues:\n{result.stdout}"


@check("Package builds successfully")
def check_build():
    import shutil
    shutil.rmtree("dist", ignore_errors=True)
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--sdist"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"Build failed:\n{result.stderr}"
    wheels = list(Path("dist").glob("*.whl"))
    assert wheels, "No wheel found in dist/"


@check("Twine check passes")
def check_twine():
    files = glob.glob("dist/*")
    assert files, "No dist/ files to check -run build first"
    result = subprocess.run(
        [sys.executable, "-m", "twine", "check"] + files,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"Twine check failed:\n{result.stdout}"


@check("pip install from wheel works on clean venv")
def check_install():
    wheels = glob.glob("dist/*.whl")
    assert wheels, "No wheel to test"
    with tempfile.TemporaryDirectory() as tmp:
        venv.create(tmp, with_pip=True)
        # Locate pip and velune inside the venv (cross-platform)
        tmp_path = Path(tmp)
        if sys.platform == "win32":
            pip_bin = tmp_path / "Scripts" / "pip.exe"
            velune_bin = tmp_path / "Scripts" / "velune.exe"
        else:
            pip_bin = tmp_path / "bin" / "pip"
            velune_bin = tmp_path / "bin" / "velune"
        result = subprocess.run(
            [str(pip_bin), "install", wheels[0], "--quiet"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, f"Install failed:\n{result.stderr}"
        result2 = subprocess.run(
            [str(velune_bin), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert result2.returncode == 0, (
            f"velune --version failed after install:\n{result2.stderr}"
        )


@check("README.md exists and is non-empty")
def check_readme():
    readme = Path("README.md")
    assert readme.exists(), "README.md missing"
    assert len(readme.read_text()) > 1000, "README is too short"


@check("LICENSE file exists")
def check_license():
    assert Path("LICENSE").exists(), "LICENSE file missing"


@check("CONTRIBUTING.md exists")
def check_contributing():
    assert Path("CONTRIBUTING.md").exists(), "CONTRIBUTING.md missing"


@check("docs/mcp.md exists")
def check_mcp_docs():
    assert Path("docs/mcp.md").exists(), "docs/mcp.md missing"


@check("WINDOWS.md exists")
def check_windows():
    assert Path("WINDOWS.md").exists(), "WINDOWS.md missing"


@check(".veluneignore template covers .env files")
def check_veluneignore():
    from velune.repository.scanner import DEFAULT_VELUNEIGNORE
    assert ".env" in DEFAULT_VELUNEIGNORE
    assert "*.pem" in DEFAULT_VELUNEIGNORE


# -- Run all checks --------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("Velune Pre-Release Checklist")
    print("=" * 60)

    all_pass = True
    for label, fn in CHECKS:
        try:
            result = fn()
            extra = f" ({result})" if isinstance(result, str) else ""
            print(f"  PASS  {label}{extra}")
        except AssertionError as e:
            print(f"  FAIL  {label}")
            print(f"        {e}")
            all_pass = False
        except Exception as e:
            print(f"  ERROR {label}")
            print(f"        {type(e).__name__}: {e}")
            all_pass = False

    print("=" * 60)
    if all_pass:
        print("All checks passed -safe to tag and release.")
        sys.exit(0)
    else:
        print("Fix failing checks before releasing.")
        sys.exit(1)


if __name__ == "__main__":
    main()
