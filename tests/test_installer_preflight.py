"""Installer platform preflight, exercised without installing anything.

Each installer is loaded with ``VELUNE_INSTALLER_SOURCE_ONLY=1`` so only its
functions are defined, then the preflight function is called with *supplied*
OS/architecture values. These are simulations of other platforms; they do not
run on, or prove anything about, native Windows-ARM64 / Alpine / Intel-Mac
hosts. Nothing is downloaded, installed or written.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "scripts" / "install.sh"
INSTALL_PS1 = ROOT / "scripts" / "install.ps1"
SUPPORT_URL = "https://github.com/Surya-Hariharan/Velune-CLI#platform-support"

_SOURCE_ONLY = {**os.environ, "VELUNE_INSTALLER_SOURCE_ONLY": "1"}


def _sh() -> str:
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no POSIX sh available")
    return sh


def _sh_reason(os_name: str, arch: str) -> str:
    result = subprocess.run(
        [_sh(), "-c", '. "$1"; unsupported_platform_reason "$2" "$3"', "sh",
         INSTALL_SH.as_posix(), os_name, arch],
        capture_output=True, text=True, env=_SOURCE_ONLY, timeout=60, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize(
    ("os_name", "arch"),
    [
        ("Linux", "x86_64"),
        ("Linux", "aarch64"),
        ("Linux", "arm64"),
        ("Linux", "amd64"),
        ("Darwin", "arm64"),
        ("Darwin", "x86_64"),  # Intel Mac: allowed (core only)
    ],
)
def test_sh_accepts_supported_platforms(os_name, arch):
    assert _sh_reason(os_name, arch) == ""


@pytest.mark.parametrize(
    ("os_name", "arch", "needle"),
    [
        ("Linux", "i686", "architecture"),
        ("Linux", "armv7l", "architecture"),
        ("Linux", "armv8l", "architecture"),
        ("Linux", "ppc64le", "architecture"),
        ("Linux", "riscv64", "architecture"),
        ("Darwin", "i386", "architecture"),
        ("FreeBSD", "amd64", "operating system"),
        ("SunOS", "x86_64", "operating system"),
        ("MINGW64_NT-10.0-26200", "x86_64", "PowerShell"),
        ("MSYS_NT-10.0", "x86_64", "PowerShell"),
        ("CYGWIN_NT-10.0", "x86_64", "PowerShell"),
    ],
)
def test_sh_rejects_unsupported_platforms_with_a_reason(os_name, arch, needle):
    reason = _sh_reason(os_name, arch)
    assert needle in reason, reason


def _run_installer_main(tmp_path, *, system: str, machine: str, stubs: dict[str, Path]):
    """Run the installer's real ``main`` with ``uname`` and other commands replaced.

    The replacements are shell *functions* defined after sourcing the script, not
    executables on ``PATH``. A function wins over any command lookup in every POSIX
    shell, whereas Git for Windows' ``sh`` puts its own ``/usr/bin`` first and so
    never consults a ``PATH`` stub (it ran the real ``uname`` and reported
    ``MINGW64_NT``). Each stub appends its name and arguments to its marker file and
    fails, so a test can see exactly what ``main`` tried to run.
    """
    sh = _sh()
    functions = [
        'uname() { if [ "$1" = "-m" ]; then echo "$STUB_MACHINE"; else echo "$STUB_SYSTEM"; fi; }'
    ]
    env = {**os.environ, "STUB_SYSTEM": system, "STUB_MACHINE": machine}
    for name, marker in stubs.items():
        env[f"STUB_MARKER_{name.upper()}"] = marker.as_posix()
        functions.append(f'{name}() {{ echo "$*" >> "$STUB_MARKER_{name.upper()}"; return 1; }}')
    env["VELUNE_INSTALLER_SOURCE_ONLY"] = "1"  # define the functions, don't run yet
    env["VELUNE_NO_MODIFY_PATH"] = "1"
    script = '. "$1"; ' + "; ".join(functions) + "; main"
    return subprocess.run(
        [sh, "-c", script, "sh", INSTALL_SH.as_posix()],
        capture_output=True, text=True, env=env, timeout=60, check=False, cwd=tmp_path,
    )  # fmt: skip


def test_sh_main_stops_before_any_download_on_unsupported_platform(tmp_path):
    """Run the real entry point on a stubbed armv7l host; curl/wget/uv must never run."""
    marker = tmp_path / "network-was-touched"
    result = _run_installer_main(
        tmp_path,
        system="Linux",
        machine="armv7l",
        stubs={"curl": marker, "wget": marker, "uv": marker},
    )
    assert result.returncode != 0
    assert "unsupported CPU architecture: armv7l" in result.stderr
    assert SUPPORT_URL in result.stderr
    assert "Nothing was downloaded or installed" in result.stderr
    assert not marker.exists(), "installer reached the network on an unsupported platform"


def test_sh_main_does_not_reject_a_supported_platform(tmp_path):
    """On supported x86-64 Linux the preflight passes and the flow reaches uv."""
    marker = tmp_path / "uv-was-called"
    result = _run_installer_main(tmp_path, system="Linux", machine="x86_64", stubs={"uv": marker})
    assert "not supported" not in result.stderr
    assert marker.exists(), "supported platform never reached the uv install step"
    assert "tool install" in marker.read_text()  # our stub uv failed on purpose


# ── PowerShell ──────────────────────────────────────────────────────────────


def _powershell() -> str:
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if exe is None:
        pytest.skip("no PowerShell available")
    return exe


def _ps_reason(arch: str, *, windows: bool = True) -> str:
    # Mirror `irm | iex`: evaluate the script text, so ExecutionPolicy is moot.
    script = (
        f"$s = Get-Content -Raw -LiteralPath '{INSTALL_PS1}'; Invoke-Expression $s; "
        f"$r = Get-VeluneUnsupportedPlatformReason -Architecture '{arch}' "
        f"-IsWindowsOs ${str(windows).lower()}; if ($r) {{ Write-Output $r }}"
    )
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, env=_SOURCE_ONLY, timeout=120, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("arch", ["X64", "AMD64", "amd64", "x64"])
def test_ps_accepts_x64(arch):
    assert _ps_reason(arch) == ""


@pytest.mark.parametrize("arch", ["Arm64", "ARM64", "arm64", "AArch64"])
def test_ps_rejects_windows_arm64(arch):
    reason = _ps_reason(arch)
    assert "ARM64" in reason
    assert "not supported" in reason


@pytest.mark.parametrize("arch", ["X86", "x86", "Arm", "Wasm", ""])
def test_ps_rejects_other_architectures(arch):
    assert "unsupported CPU architecture" in _ps_reason(arch)


def test_ps_rejects_non_windows_hosts():
    assert "install.sh" in _ps_reason("X64", windows=False)


def test_ps_install_function_throws_actionable_message_on_arm64():
    """Run Install-Velune itself with the host architecture forced to ARM64."""
    script = (
        f"$s = Get-Content -Raw -LiteralPath '{INSTALL_PS1}'; Invoke-Expression $s; "
        "function Get-VeluneHostArchitecture { 'Arm64' }; "
        "try { Install-Velune; Write-Output 'NO-THROW' } "
        "catch { Write-Output $_.Exception.Message }"
    )
    env = {k: v for k, v in os.environ.items() if k != "VELUNE_INSTALLER_SOURCE_ONLY"}
    env["VELUNE_INSTALLER_SOURCE_ONLY"] = "1"  # keep the script from auto-running
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, env=env, timeout=120, check=False,
    )  # fmt: skip
    out = result.stdout
    assert "NO-THROW" not in out, out
    # Install-Velune checks "is this Windows?" before "which architecture?" (and $IsWindows
    # is a read-only automatic variable, so a test cannot pretend to be Windows). On Windows the
    # forced ARM64 reaches the architecture check; elsewhere the correct, equally actionable
    # refusal is the not-Windows one. The ARM64 reason itself is exercised on every OS by
    # test_ps_rejects_windows_arm64, which passes the platform explicitly.
    assert ("not supported" if sys.platform == "win32" else "not Windows") in out, out
    assert SUPPORT_URL in out
    assert "Nothing was downloaded or installed" in out


def test_ps_host_architecture_matches_this_machine():
    """Native check: the real detector returns something the preflight understands."""
    script = (
        f"$s = Get-Content -Raw -LiteralPath '{INSTALL_PS1}'; Invoke-Expression $s; "
        "Write-Output (Get-VeluneHostArchitecture)"
    )
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, env=_SOURCE_ONLY, timeout=120, check=False,
    )  # fmt: skip
    assert result.stdout.strip(), result.stderr


def test_installers_document_the_support_page():
    assert SUPPORT_URL in INSTALL_SH.read_text(encoding="utf-8")
    assert SUPPORT_URL in INSTALL_PS1.read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "#### Platform support" in readme  # the anchor the installers point to


# ── Intel macOS classification (F2) ─────────────────────────────────────────

README = ROOT / "README.md"


def _readme_row(prefix: str) -> str:
    rows = [ln for ln in README.read_text(encoding="utf-8").splitlines() if ln.startswith(prefix)]
    assert len(rows) == 1, f"expected exactly one README row starting {prefix!r}"
    return rows[0]


def _sh_notes(os_name: str, arch: str) -> str:
    result = subprocess.run(
        [_sh(), "-c", '. "$1"; platform_notes "$2" "$3"', "sh",
         INSTALL_SH.as_posix(), os_name, arch],
        capture_output=True, text=True, env=_SOURCE_ONLY, timeout=60, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("arch", ["x86_64", "amd64"])
def test_intel_mac_note_says_unverified_and_may_need_rust(arch):
    note = _sh_notes("Darwin", arch)
    assert "unverified" in note.lower()
    assert "Rust" in note
    assert "cryptography" in note
    assert "[rag]" in note  # base CLI and the optional extra are distinguished
    assert "Installation will continue" in note  # not a rejection
    assert SUPPORT_URL in note
    assert "not tested" not in note.lower()  # the old, over-confident wording


@pytest.mark.parametrize(("os_name", "arch"), [("Darwin", "arm64"), ("Linux", "x86_64")])
def test_other_supported_platforms_get_no_intel_note(os_name, arch):
    assert "Intel" not in _sh_notes(os_name, arch)


def test_intel_mac_is_still_allowed_and_install_continues(tmp_path):
    """The installer must not reject Intel macOS; it warns and proceeds to uv."""
    marker = tmp_path / "uv-was-called"
    result = _run_installer_main(tmp_path, system="Darwin", machine="x86_64", stubs={"uv": marker})
    assert "not supported" not in result.stderr
    assert "unverified" in result.stdout.lower()
    assert marker.exists(), "Intel macOS never reached the uv install step"


def test_readme_intel_mac_row_matches_installer_wording():
    row = _readme_row("| macOS, Intel")
    assert "**Unverified.**" in row
    assert "Rust" in row and "cryptography" in row and "uv.lock" in row
    assert "`[rag]`" in row  # optional extra called out separately from the base CLI
    assert "does not block" in row  # consistent with the installer's behavior
    # the broad claim that F2 removed must not come back for this row
    assert "Prebuilt wheels exist" not in row
    assert "PyPI currently offers" in row  # states that live PyPI was not checked
    # same key facts as the installer note
    note = _sh_notes("Darwin", "x86_64")
    for term in ("cryptography", "Rust", "[rag]"):
        assert term in note and term in row.replace("`", "")


def test_readme_defines_the_unverified_label():
    text = README.read_text(encoding="utf-8")
    assert "**Unverified**\nmeans we found a gap" in text


def test_windows_arm64_policy_is_unchanged():
    row = _readme_row("| **Windows on ARM64**")
    assert "**Unsupported.**" in row and "stops before downloading anything" in row
    assert "no Windows ARM64 wheels" in row


def _locked_core_native_packages_missing_intel_mac_wheel() -> list[str]:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    import re

    lock = ROOT / "uv.lock"
    if not lock.is_file():
        pytest.skip("no uv.lock in this checkout")
    packages = {p["name"]: p for p in tomllib.loads(lock.read_text(encoding="utf-8"))["package"]}
    seen: set[str] = set()
    stack = [d["name"] for d in packages["velune-cli"].get("dependencies", [])]
    while stack:  # closure of the *core* (non-extra) dependencies
        name = stack.pop()
        if name in seen or name not in packages:
            continue
        seen.add(name)
        stack += [d["name"] for d in packages[name].get("dependencies", [])]
    missing = []
    for name in sorted(seen):
        tags = [w["url"].rsplit("/", 1)[-1] for w in packages[name].get("wheels", [])]
        if not tags or any(t.endswith("-none-any.whl") for t in tags):
            continue  # pure Python, or sdist-only (not a wheel-availability claim)
        if name == "pywin32":
            continue  # Windows-only dependency
        if not any(re.search(r"macosx_.*(x86_64|universal2)", t) for t in tags):
            missing.append(name)
    return missing


def test_readme_does_not_claim_intel_mac_wheels_the_lock_lacks():
    """Drift guard: the README may only call Intel macOS fully wheel-supported if uv.lock agrees."""
    missing = _locked_core_native_packages_missing_intel_mac_wheel()
    row = _readme_row("| macOS, Intel")
    if missing:
        assert "Unverified" in row and "Prebuilt wheels exist" not in row, (
            f"uv.lock has no macOS x86-64 wheel for {missing}; the README must not claim support"
        )
    # If the lock ever gains these wheels the 'Unverified' wording may be relaxed
    # deliberately; this test then stops constraining it.
