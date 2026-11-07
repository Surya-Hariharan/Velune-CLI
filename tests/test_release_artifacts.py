"""Release artifact verification, checksums, and the release workflow wiring."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_artifacts.py"
VERSION = "9.8.7"

_spec = importlib.util.spec_from_file_location("release_artifacts", SCRIPT)
assert _spec and _spec.loader
ra = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ra)


def _make_wheel(dist: Path, version: str = VERSION, *, metadata_version: str | None = None):
    path = dist / f"velune_cli-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("velune/__init__.py", f'__version__ = "{version}"\n')
        z.writestr(
            f"velune_cli-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: velune-cli\nVersion: {metadata_version or version}\n",
        )
    return path


def _make_sdist(dist: Path, version: str = VERSION, *, stem: str = "velune_cli"):
    path = dist / f"{stem}-{version}.tar.gz"
    with tarfile.open(path, "w:gz") as t:
        for name, data in (
            (f"{stem}-{version}/PKG-INFO", b"Name: velune-cli\n"),
            (f"{stem}-{version}/velune/__init__.py", b"x = 1\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture
def dist(tmp_path):
    d = tmp_path / "dist"
    d.mkdir()
    return d


@pytest.fixture
def good_dist(dist):
    _make_wheel(dist)
    _make_sdist(dist)
    return dist


# ── verify ──────────────────────────────────────────────────────────────────


def test_good_dist_passes(good_dist):
    assert ra.verify_dist(good_dist, VERSION) == []


def test_old_style_hyphenated_sdist_name_is_accepted(dist):
    _make_wheel(dist)
    _make_sdist(dist, stem="velune-cli")
    assert ra.verify_dist(dist, VERSION) == []


def test_missing_directory_and_empty_directory_fail(tmp_path, dist):
    assert ra.verify_dist(tmp_path / "nope", VERSION)
    assert any("no artifacts" in p for p in ra.verify_dist(dist, VERSION))


def test_missing_sdist_or_wheel_fails(dist):
    _make_wheel(dist)
    assert any("sdist" in p for p in ra.verify_dist(dist, VERSION))
    (dist / f"velune_cli-{VERSION}-py3-none-any.whl").unlink()
    _make_sdist(dist)
    assert any("wheel" in p for p in ra.verify_dist(dist, VERSION))


def test_empty_artifact_fails(good_dist):
    (good_dist / f"velune_cli-{VERSION}.tar.gz").write_bytes(b"")
    assert any("empty" in p for p in ra.verify_dist(good_dist, VERSION))


def test_truncated_wheel_fails(good_dist):
    wheel = good_dist / f"velune_cli-{VERSION}-py3-none-any.whl"
    wheel.write_bytes(wheel.read_bytes()[:20])
    assert any("zip" in p or "corrupt" in p for p in ra.verify_dist(good_dist, VERSION))


def test_garbage_sdist_fails(good_dist):
    (good_dist / f"velune_cli-{VERSION}.tar.gz").write_bytes(b"not a tarball")
    assert any(".tar.gz" in p for p in ra.verify_dist(good_dist, VERSION))


def test_version_mismatch_with_tag_fails(good_dist):
    problems = ra.verify_dist(good_dist, "1.0.0")
    assert problems and all("does not match" in p for p in problems)


def test_metadata_version_mismatch_fails(dist):
    _make_wheel(dist, metadata_version="0.0.1")
    _make_sdist(dist)
    assert any("METADATA Version" in p for p in ra.verify_dist(dist, VERSION))


def test_unexpected_files_and_multiple_wheels_fail(good_dist):
    (good_dist / "notes.txt").write_text("x")
    _make_wheel(good_dist, "9.8.6")
    problems = ra.verify_dist(good_dist, VERSION)
    assert any("notes.txt" in p for p in problems)
    assert any("exactly 1 wheel" in p for p in problems)


def test_wrong_distribution_name_fails(dist):
    other = dist / f"evil_pkg-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(other, "w") as z:
        z.writestr("x.py", "")
    _make_sdist(dist)
    assert any("not velune-cli" in p for p in ra.verify_dist(dist, VERSION))


def test_platform_specific_wheel_is_rejected(dist):
    path = dist / f"velune_cli-{VERSION}-cp312-cp312-win_amd64.whl"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("velune/__init__.py", "")
        z.writestr(f"velune_cli-{VERSION}.dist-info/METADATA", f"Version: {VERSION}\n")
    _make_sdist(dist)
    assert any("py3-none-any" in p for p in ra.verify_dist(dist, VERSION))


# ── checksums ───────────────────────────────────────────────────────────────


def test_checksums_are_sha256sum_format_sorted_and_deterministic(good_dist):
    first = ra.build_checksums(good_dist)
    assert first == ra.build_checksums(good_dist)
    lines = first.splitlines()
    assert lines == sorted(lines, key=lambda line: line.split("  ", 1)[1])
    for line in lines:
        digest, name = line.split("  ", 1)  # exactly two spaces, as sha256sum emits
        assert digest == hashlib.sha256((good_dist / name).read_bytes()).hexdigest()
    assert first.endswith("\n") and "\r" not in first
    assert {line.split("  ")[1] for line in lines} == {p.name for p in good_dist.iterdir()}


def test_checksum_names_are_unambiguous_basenames(good_dist):
    for line in ra.build_checksums(good_dist).splitlines():
        name = line.split("  ", 1)[1]
        assert "/" not in name and "\\" not in name


def test_check_passes_then_detects_tamper_truncation_and_extras(good_dist, tmp_path):
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(ra.build_checksums(good_dist), encoding="utf-8")
    assert ra.check_checksums(good_dist, sums) == []

    wheel = good_dist / f"velune_cli-{VERSION}-py3-none-any.whl"
    original = wheel.read_bytes()
    wheel.write_bytes(original + b"x")
    assert any("mismatch" in p for p in ra.check_checksums(good_dist, sums))
    wheel.write_bytes(original)

    (good_dist / "extra.whl").write_bytes(b"x")
    assert any("not in the manifest" in p for p in ra.check_checksums(good_dist, sums))
    (good_dist / "extra.whl").unlink()

    wheel.unlink()
    assert any("missing" in p for p in ra.check_checksums(good_dist, sums))


def test_check_rejects_missing_empty_or_malformed_manifest(good_dist, tmp_path):
    assert ra.check_checksums(good_dist, tmp_path / "absent")
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert ra.check_checksums(good_dist, empty)
    bad = tmp_path / "bad"
    bad.write_text("zzz  file\n")
    assert any("malformed" in p for p in ra.check_checksums(good_dist, bad))


def test_check_rejects_path_traversal_names(good_dist, tmp_path):
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{'0' * 64}  ../escape.whl\n")
    assert any("malformed" in p for p in ra.check_checksums(good_dist, sums))


def test_manifest_file_itself_is_never_listed(good_dist):
    (good_dist / "SHA256SUMS").write_text("stale")
    assert "SHA256SUMS" not in ra.build_checksums(good_dist)


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_end_to_end_and_exit_codes(good_dist, tmp_path, capsys):
    out = tmp_path / "checksums" / "SHA256SUMS"
    assert ra.main(["verify", "--dist", str(good_dist), "--version", VERSION]) == 0
    assert ra.main(["checksums", "--dist", str(good_dist), "--output", str(out)]) == 0
    assert out.read_bytes() == ra.build_checksums(good_dist).encode()
    assert not (good_dist / "SHA256SUMS").exists()  # kept out of dist/ for the PyPI upload
    assert ra.main(["check", "--dist", str(good_dist), "--sums", str(out)]) == 0

    (good_dist / f"velune_cli-{VERSION}.tar.gz").write_bytes(b"")
    assert ra.main(["verify", "--dist", str(good_dist), "--version", VERSION]) == 1
    assert "ERROR" in capsys.readouterr().err


def test_cli_refuses_to_checksum_an_empty_or_missing_dist(dist, tmp_path):
    out = tmp_path / "SHA256SUMS"
    assert ra.main(["checksums", "--dist", str(dist), "--output", str(out)]) == 1
    assert ra.main(["checksums", "--dist", str(tmp_path / "nope"), "--output", str(out)]) == 1
    assert not out.exists()


# ── workflow wiring ─────────────────────────────────────────────────────────


def _yaml():
    # PyYAML is only a transitive dev dependency; skip rather than break the
    # artifact tests above where it is absent (e.g. the lowest-versions job).
    return pytest.importorskip("yaml")


@pytest.fixture(scope="module")
def release():
    return _yaml().safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))


def _steps_text(job: dict) -> str:
    return "\n".join(str(s) for s in job.get("steps", []))


def test_ci_workflow_is_callable():
    ci = _yaml().safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    triggers = ci.get("on", ci.get(True))  # PyYAML parses a bare `on:` key as True
    assert "workflow_call" in triggers
    assert "push" in triggers and "pull_request" in triggers  # existing triggers kept


def test_release_gate_includes_the_full_ci_matrix(release):
    jobs = release["jobs"]
    assert jobs["full-ci"]["uses"] == "./.github/workflows/ci.yml"
    assert "ci" in jobs  # the original quick check is kept, not replaced
    assert {"ci", "full-ci"} <= set(jobs["build"]["needs"])
    assert jobs["publish"]["needs"] == "build"
    assert jobs["release"]["needs"] == "publish"


def test_build_verifies_then_checksums_outside_dist(release):
    text = _steps_text(release["jobs"]["build"])
    assert text.index("release_artifacts.py verify") < text.index("release_artifacts.py checksums")
    assert "checksums/SHA256SUMS" in text
    assert "twine check --strict" in text  # existing safeguard kept
    assert "Verify tag matches package version" in text


def test_publish_checks_integrity_before_uploading_and_keeps_safeguards(release):
    steps = release["jobs"]["publish"]["steps"]
    names = [s.get("name", "") for s in steps]
    check = next(i for i, n in enumerate(names) if n.startswith("Verify artifacts"))
    upload = next(i for i, n in enumerate(names) if n == "Publish to PyPI")
    assert check < upload
    publish = steps[upload]
    assert publish["with"]["packages-dir"] == "dist/"
    assert "PYPI_TOKEN" in publish["with"]["password"]  # auth mechanism untouched
    assert release["jobs"]["publish"]["environment"]["name"] == "pypi"


def test_github_release_attaches_checksums_and_fails_on_missing_files(release):
    steps = release["jobs"]["release"]["steps"]
    create = next(s for s in steps if s.get("name") == "Create Release")
    assert "checksums/SHA256SUMS" in create["with"]["files"]
    assert "dist/*" in create["with"]["files"]
    assert create["with"]["fail_on_unmatched_files"] is True
    assert "release_artifacts.py check" in _steps_text(release["jobs"]["release"])


def test_no_release_is_triggered_by_these_changes(release):
    # Still tag-triggered only: nothing here can publish on push to a branch.
    triggers = release.get("on", release.get(True))
    assert list(triggers) == ["push"] and list(triggers["push"]) == ["tags"]


# ── duplicate manifest entries (F3) ──────────────────────────────────────────


def _manifest_lines(good_dist):
    return ra.build_checksums(good_dist).splitlines()


def _write_manifest(tmp_path, lines):
    sums = tmp_path / "SHA256SUMS"
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return sums


def test_identical_duplicate_entry_is_rejected(good_dist, tmp_path):
    lines = _manifest_lines(good_dist)
    sums = _write_manifest(tmp_path, [*lines, lines[0]])  # same name, same hash, repeated
    problems = ra.check_checksums(good_dist, sums)
    assert any("duplicate entry" in p for p in problems), problems


def test_conflicting_duplicate_entry_is_rejected(good_dist, tmp_path):
    lines = _manifest_lines(good_dist)
    name = lines[0].split("  ", 1)[1]
    sums = _write_manifest(tmp_path, [*lines, f"{'1' * 64}  {name}"])
    problems = ra.check_checksums(good_dist, sums)
    assert any("duplicate entry" in p and name in p for p in problems), problems


@pytest.mark.parametrize("bad_first", [True, False])
def test_bad_and_correct_hash_for_one_file_is_rejected_in_either_order(
    good_dist, tmp_path, bad_first
):
    # Neither the first nor the last entry may be silently trusted.
    lines = _manifest_lines(good_dist)
    good = lines[0]
    name = good.split("  ", 1)[1]
    bad = f"{'0' * 64}  {name}"
    ordered = [bad, good, *lines[1:]] if bad_first else [good, bad, *lines[1:]]
    problems = ra.check_checksums(good_dist, _write_manifest(tmp_path, ordered))
    assert any("duplicate entry" in p for p in problems), problems


def test_duplicate_error_is_actionable_and_does_not_cascade(good_dist, tmp_path):
    lines = _manifest_lines(good_dist)
    name = lines[0].split("  ", 1)[1]
    sums = _write_manifest(tmp_path, [lines[0], f"{'2' * 64}  {name}", *lines[1:]])
    problems = ra.check_checksums(good_dist, sums)
    (dup,) = [p for p in problems if "duplicate" in p]
    assert "SHA256SUMS:2" in dup and "line 1" in dup and name in dup
    # the ambiguous name is reported once, not also as a misleading mismatch
    assert not any(name in p and "mismatch" in p for p in problems)


def test_duplicate_manifest_fails_the_cli_closed(good_dist, tmp_path, capsys):
    lines = _manifest_lines(good_dist)
    sums = _write_manifest(tmp_path, [*lines, lines[-1]])
    assert ra.main(["check", "--dist", str(good_dist), "--sums", str(sums)]) == 1
    assert "duplicate entry" in capsys.readouterr().err


def test_valid_manifest_still_passes_after_duplicate_detection(good_dist, tmp_path):
    sums = _write_manifest(tmp_path, _manifest_lines(good_dist))
    assert ra.check_checksums(good_dist, sums) == []


# ── release artifact names and handoff (F1) ─────────────────────────────────


def _artifact_refs(job: dict, action: str) -> list[dict]:
    return [s["with"] for s in job.get("steps", []) if action in s.get("uses", "") and "with" in s]


def test_release_artifact_names_do_not_collide_with_ci_artifacts():
    """ci.yml runs inside the release run (full-ci); names are unique per run."""
    yaml = _yaml()
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    rel = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    ci_uploads = {
        w["name"] for job in ci["jobs"].values() for w in _artifact_refs(job, "upload-artifact")
    }
    release_uploads = {
        w["name"] for job in rel["jobs"].values() for w in _artifact_refs(job, "upload-artifact")
    }
    assert "dist" in ci_uploads  # the situation this guards against still exists
    assert release_uploads, "release builds no artifacts?"
    assert ci_uploads.isdisjoint(release_uploads), ci_uploads & release_uploads


def test_release_never_uses_the_generic_ci_artifact_names(release):
    for job in release["jobs"].values():
        for ref in (
            *_artifact_refs(job, "upload-artifact"),
            *_artifact_refs(job, "download-artifact"),
        ):
            assert ref["name"] not in {"dist", "checksums"}, ref


def test_release_artifacts_are_produced_once_and_consumed_exactly(release):
    jobs = release["jobs"]
    produced = {w["name"]: w["path"] for w in _artifact_refs(jobs["build"], "upload-artifact")}
    assert produced == {"release-dist": "dist/", "release-checksums": "checksums/SHA256SUMS"}
    for consumer in ("publish", "release"):
        consumed = {
            w["name"]: w["path"] for w in _artifact_refs(jobs[consumer], "download-artifact")
        }
        # exactly the artifacts the release build produced, restored to the
        # directories the later steps read from
        assert consumed == {"release-dist": "dist/", "release-checksums": "checksums/"}, consumer
    # nothing else uploads or downloads in the release workflow
    others = [j for j in jobs if j not in {"build", "publish", "release"}]
    for name in others:
        assert not _artifact_refs(jobs[name], "upload-artifact"), name
        assert not _artifact_refs(jobs[name], "download-artifact"), name


def test_downstream_steps_read_the_directories_the_artifacts_restore_into(release):
    text_publish = _steps_text(release["jobs"]["publish"])
    assert "cd dist && sha256sum --check --strict ../checksums/SHA256SUMS" in text_publish
    release_job = release["jobs"]["release"]
    assert "release_artifacts.py check --dist dist --sums checksums/SHA256SUMS" in _steps_text(
        release_job
    )
    create = next(s for s in release_job["steps"] if s.get("name") == "Create Release")
    assert "dist/*" in create["with"]["files"] and "checksums/SHA256SUMS" in create["with"]["files"]


def test_release_gate_and_safeguards_survive_the_rename(release):
    jobs = release["jobs"]
    assert set(jobs["build"]["needs"]) == {"ci", "full-ci"}
    assert jobs["publish"]["needs"] == "build" and jobs["release"]["needs"] == "publish"
    for job in jobs.values():  # nothing may run despite a failed upstream job
        assert "always()" not in str(job.get("if", ""))
    assert "twine check --strict" in _steps_text(jobs["build"])
    assert "Verify tag matches package version" in _steps_text(jobs["build"])
    assert jobs["publish"]["environment"]["name"] == "pypi"
