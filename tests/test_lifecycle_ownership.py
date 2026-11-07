"""Ownership detection (P2.1): when may Velune treat its installation as its own to change?

Fixtures build throwaway environments in ``tmp_path`` shaped like the ones the
P2.0 spike observed (uv 0.12.3, Windows). Nothing here starts uv or touches the
real Velune installation: ``importlib.metadata.distribution`` and the uv
tool-directory lookup are replaced.
"""

from __future__ import annotations

import importlib.metadata as md
import json
from pathlib import Path

import pytest

from velune.lifecycle import ownership as own
from velune.lifecycle.errors import ExitCode, LifecycleError
from velune.lifecycle.ownership import OwnershipKind

RECEIPT = """\
[tool]
requirements = [{ name = "velune-cli", extras = ["rag"], specifier = "==0.9.9" }]
python = "C:/Python314/python.exe"
entrypoints = [
    { name = "velune", install-path = "C:/bin/velune.exe", from = "velune-cli" },
]

[tool.options]
find-links = ["file:///C:/secret-index/user:pass"]
"""

UNPINNED_RECEIPT = '[tool]\nrequirements = [{ name = "velune-cli" }]\npython = "/usr/bin/python3"\n'


def build_env(
    root: Path,
    *,
    tool_name: str = "velune-cli",
    receipt: str | None = RECEIPT,
    installer: str | None = "uv\n",
    direct_url: object = None,
    with_dist: bool = True,
) -> Path:
    """Create ``root/tools/<tool_name>`` like a uv tool env; return its path."""
    prefix = root / "tools" / tool_name
    site = prefix / "Lib" / "site-packages"
    site.mkdir(parents=True)
    (prefix / "pyvenv.cfg").write_text("home = C:\\Python314\nuv = 0.12.3\n")
    if receipt is not None:
        (prefix / "uv-receipt.toml").write_text(receipt, encoding="utf-8")
    if with_dist:
        info = site / "velune_cli-0.9.9.dist-info"
        info.mkdir()
        (info / "METADATA").write_text("Metadata-Version: 2.1\nName: velune-cli\nVersion: 0.9.9\n")
        if installer is not None:
            (info / "INSTALLER").write_text(installer)
        if direct_url is not None:
            text = direct_url if isinstance(direct_url, str) else json.dumps(direct_url)
            (info / "direct_url.json").write_text(text)
    return prefix


@pytest.fixture
def detect(monkeypatch: pytest.MonkeyPatch):
    """Run detection against *prefix* with uv's tool dir fixed to *tool_dir*."""

    def run(prefix: Path, tool_dir: Path | None, problem: str | None = None):
        site = prefix / "Lib" / "site-packages"
        infos = list(site.glob("velune_cli-*.dist-info")) if site.exists() else []

        def fake_distribution(name: str):
            if not infos:
                raise md.PackageNotFoundError(name)
            return md.Distribution.at(infos[0])

        monkeypatch.setattr(own.importlib_metadata, "distribution", fake_distribution)
        return own.detect_ownership(
            prefix=prefix,
            resolve_tool_dir=lambda: (tool_dir, problem if tool_dir is None else None),
        )

    return run


def test_confirmed_uv_tool(tmp_path, detect):
    prefix = build_env(tmp_path)
    report = detect(prefix, tmp_path / "tools")
    assert report.kind is OwnershipKind.UV_TOOL
    assert report.mutation_allowed
    assert report.version == "0.9.9"
    assert report.specifier == "==0.9.9"
    assert report.extras == ("rag",)
    assert report.python_base == "C:/Python314/python.exe"
    assert report.installation == {"kind": "uv_tool", "owned": True, "path": str(prefix)}


def test_unpinned_receipt_has_no_specifier(tmp_path, detect):
    prefix = build_env(tmp_path, receipt=UNPINNED_RECEIPT)
    report = detect(prefix, tmp_path / "tools")
    assert report.kind is OwnershipKind.UV_TOOL
    assert report.specifier is None
    assert report.extras == ()


def test_missing_receipt_is_ambiguous(tmp_path, detect):
    prefix = build_env(tmp_path, receipt=None)
    report = detect(prefix, tmp_path / "tools")
    assert report.kind is OwnershipKind.AMBIGUOUS
    assert not report.mutation_allowed


def test_installer_uv_without_receipt_is_not_enough(tmp_path, detect):
    """A plain `uv venv` + `uv pip install` writes INSTALLER=uv and no receipt (P2.0 D4)."""
    prefix = build_env(tmp_path, tool_name="somevenv", receipt=None)
    report = detect(prefix, tmp_path / "elsewhere")
    assert report.kind is OwnershipKind.AMBIGUOUS
    assert "INSTALLER=uv alone is not proof" in report.reasons[0]
    assert not report.mutation_allowed


@pytest.mark.parametrize(
    "receipt",
    [
        "this is = = not toml",
        "[other]\nx = 1\n",
        "[tool]\nrequirements = []\n",
        '[tool]\nrequirements = [{ name = "someone-else" }]\n',
        '[tool]\nrequirements = [{ name = "velune-cli", specifier = "not a specifier" }]\n',
        '[tool]\nrequirements = [{ name = "velune-cli", extras = "rag" }]\n',
        '[tool]\nrequirements = [{ name = "velune-cli", extras = ["bad name!"] }]\n',
        "",
    ],
)
def test_malformed_receipt_is_indeterminate(tmp_path, detect, receipt):
    prefix = build_env(tmp_path, receipt=receipt)
    report = detect(prefix, tmp_path / "tools")
    assert report.kind is OwnershipKind.INDETERMINATE
    assert not report.mutation_allowed


def test_receipt_that_is_a_directory_is_malformed(tmp_path, detect):
    prefix = build_env(tmp_path, receipt=None)
    (prefix / "uv-receipt.toml").mkdir()
    assert detect(prefix, tmp_path / "tools").kind is OwnershipKind.INDETERMINATE


def test_prefix_outside_uv_tool_dir_is_ambiguous(tmp_path, detect):
    prefix = build_env(tmp_path)
    report = detect(prefix, tmp_path / "some-other-tool-dir")
    assert report.kind is OwnershipKind.AMBIGUOUS
    assert not report.mutation_allowed


def test_prefix_nested_below_tool_dir_is_not_accepted(tmp_path, detect):
    """Must be a direct child of the tool dir, not merely somewhere below it."""
    prefix = build_env(tmp_path)
    report = detect(prefix, tmp_path)  # tools/velune-cli is two levels below tmp_path
    assert report.kind is OwnershipKind.AMBIGUOUS


def test_wrong_tool_name_directory_is_ambiguous(tmp_path, detect):
    prefix = build_env(tmp_path, tool_name="other-tool")
    assert detect(prefix, tmp_path / "tools").kind is OwnershipKind.AMBIGUOUS


def test_prefix_given_with_dotdot_still_matches(tmp_path, detect):
    build_env(tmp_path)
    (tmp_path / "tools" / "sibling").mkdir()  # `..` must be traversable on POSIX too
    odd = tmp_path / "tools" / "sibling" / ".." / "velune-cli"
    assert detect(odd, tmp_path / "tools").kind is OwnershipKind.UV_TOOL


def test_unknown_tool_dir_cannot_confirm(tmp_path, detect):
    prefix = build_env(tmp_path)
    report = detect(prefix, None, "uv was not found on PATH")
    assert report.kind is OwnershipKind.AMBIGUOUS or report.kind is OwnershipKind.INDETERMINATE
    assert not report.mutation_allowed


def test_receipt_and_installer_but_unverifiable_tool_dir_is_indeterminate(tmp_path, detect):
    prefix = build_env(tmp_path)
    report = detect(prefix, None, "uv was not found on PATH")
    assert report.kind is OwnershipKind.INDETERMINATE
    assert "uv was not found" in report.reasons[0]


@pytest.mark.parametrize("installer", ["pip\n", "poetry", "conda"])
def test_non_uv_installer_without_uv_signals(tmp_path, detect, installer):
    prefix = build_env(tmp_path, tool_name="venv", receipt=None, installer=installer)
    report = detect(prefix, tmp_path / "elsewhere")
    assert report.kind is OwnershipKind.NOT_UV
    assert not report.mutation_allowed


def test_non_uv_installer_inside_a_uv_looking_env_is_ambiguous(tmp_path, detect):
    prefix = build_env(tmp_path, installer="pip\n")
    report = detect(prefix, tmp_path / "tools")
    assert report.kind is OwnershipKind.AMBIGUOUS


def test_no_installer_and_no_receipt_is_indeterminate(tmp_path, detect):
    prefix = build_env(tmp_path, tool_name="venv", receipt=None, installer=None)
    assert detect(prefix, tmp_path / "elsewhere").kind is OwnershipKind.INDETERMINATE


def test_installer_text_must_be_exactly_uv(tmp_path, detect):
    prefix = build_env(tmp_path, installer="uv-something\n")
    report = detect(prefix, tmp_path / "tools")
    assert report.kind is OwnershipKind.AMBIGUOUS


def test_package_metadata_missing(tmp_path, detect):
    prefix = build_env(tmp_path, with_dist=False)
    assert detect(prefix, tmp_path / "tools").kind is OwnershipKind.INDETERMINATE


def test_metadata_from_another_environment_is_ambiguous(tmp_path, detect, monkeypatch):
    prefix = build_env(tmp_path)
    other = build_env(tmp_path / "other")
    detect(prefix, tmp_path / "tools")  # install fake distribution() for `prefix`
    info = next((other / "Lib" / "site-packages").glob("*.dist-info"))
    monkeypatch.setattr(own.importlib_metadata, "distribution", lambda n: md.Distribution.at(info))
    report = own.detect_ownership(
        prefix=prefix, resolve_tool_dir=lambda: (tmp_path / "tools", None)
    )
    assert report.kind is OwnershipKind.AMBIGUOUS
    assert not report.mutation_allowed


# ---- direct_url.json (PEP 610) --------------------------------------------


def test_editable_install_is_detected_and_refused(tmp_path, detect):
    prefix = build_env(
        tmp_path, direct_url={"url": "file:///src/velune", "dir_info": {"editable": True}}
    )
    report = detect(prefix, tmp_path / "tools")
    assert report.kind is OwnershipKind.EDITABLE
    assert not report.mutation_allowed


def test_direct_url_present_but_not_editable_is_not_called_editable(tmp_path, detect):
    """A local-wheel `uv tool install` writes direct_url.json (P2.0 D2) and is not editable."""
    prefix = build_env(
        tmp_path,
        direct_url={"url": "file:///C:/w/velune_cli-0.9.9-py3-none-any.whl", "archive_info": {}},
    )
    report = detect(prefix, tmp_path / "tools")
    assert report.kind is OwnershipKind.UV_TOOL
    assert report.direct_url == "archive"
    assert report.installed_from_local_file


def test_directory_install_that_is_not_editable(tmp_path, detect):
    prefix = build_env(tmp_path, direct_url={"url": "file:///src", "dir_info": {}})
    report = detect(prefix, tmp_path / "tools")
    assert report.direct_url == "directory"
    assert report.kind is OwnershipKind.UV_TOOL


@pytest.mark.parametrize("raw", ["{not json", "[]", '{"no_url": 1}', '{"url": "x"}', '{"url": 5}'])
def test_malformed_direct_url_fails_closed(tmp_path, detect, raw):
    prefix = build_env(tmp_path, direct_url=raw)
    report = detect(prefix, tmp_path / "tools")
    assert report.kind is OwnershipKind.INDETERMINATE
    assert not report.mutation_allowed


# ---- helpers --------------------------------------------------------------


def test_only_uv_tool_authorises_mutation():
    allowed = [k for k in OwnershipKind if own.OwnershipReport(k, (), "p").mutation_allowed]
    assert allowed == [OwnershipKind.UV_TOOL]


@pytest.mark.parametrize(
    "kind",
    [
        OwnershipKind.NOT_UV,
        OwnershipKind.EDITABLE,
        OwnershipKind.AMBIGUOUS,
        OwnershipKind.INDETERMINATE,
    ],
)
def test_require_uv_tool_refuses_everything_else(kind):
    report = own.OwnershipReport(kind, ("because",), "/p")
    with pytest.raises(LifecycleError) as caught:
        own.require_uv_tool(report)
    assert caught.value.code is ExitCode.REFUSED
    assert "Nothing was changed" in (caught.value.recovery or "")
    assert caught.value.details["ownership"] == kind.value


def test_require_uv_tool_passes_confirmed():
    report = own.OwnershipReport(OwnershipKind.UV_TOOL, (), "/p")
    assert own.require_uv_tool(report) is report


def test_resolve_uv_tool_dir_uses_argv_without_shell(tmp_path):
    calls = []

    class Done:
        returncode = 0
        stdout = f"{tmp_path}\n"

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Done()

    path, problem = own.resolve_uv_tool_dir("uv-bin", run=fake_run)
    assert (path, problem) == (tmp_path, None)
    assert calls[0][0] == ["uv-bin", "tool", "dir"]
    assert not calls[0][1].get("shell")


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(1, "/x\n"), (0, ""), (0, "a\nb\n"), (0, "relative/dir\n")],
)
def test_resolve_uv_tool_dir_rejects_odd_output(returncode, stdout):
    class Done:
        pass

    done = Done()
    done.returncode, done.stdout = returncode, stdout
    path, problem = own.resolve_uv_tool_dir("uv", run=lambda *a, **k: done)
    assert path is None
    assert problem


def test_resolve_uv_tool_dir_when_uv_cannot_start():
    def boom(*a, **k):
        raise FileNotFoundError("nope")

    path, problem = own.resolve_uv_tool_dir("uv", run=boom)
    assert path is None and "FileNotFoundError" in (problem or "")


def test_resolve_uv_tool_dir_without_uv(monkeypatch):
    monkeypatch.setattr(own.shutil, "which", lambda name: None)
    path, problem = own.resolve_uv_tool_dir()
    assert path is None and "not found" in (problem or "")
