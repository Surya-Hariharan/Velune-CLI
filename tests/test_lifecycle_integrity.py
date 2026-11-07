"""The installed-files check: RECORD sizes and hashes, nothing else, never raises."""

from __future__ import annotations

import base64
import hashlib
import importlib
from pathlib import Path

import pytest

from velune.lifecycle import integrity as integ


def _h(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def make_dist(root: Path, name="demo_pkg", version="1.0", files=None, record=True, hashes=True):
    files = {"demo_pkg/__init__.py": b"x = 1\n", "demo_pkg/mod.py": b"y = 2\n"} | (files or {})
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    info = root / f"{name}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    if record:
        rows = [
            f"{rel},{_h(d) if hashes else ''},{len(d) if hashes else ''}"
            for rel, d in files.items()
        ]
        rows.append(f"{info.name}/RECORD,,")
        (info / "RECORD").write_text("\n".join(rows) + "\n")
    return info


@pytest.fixture
def site(tmp_path, monkeypatch):
    root = tmp_path / "site"
    root.mkdir()
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    return root


def test_intact_install_is_intact(site):
    make_dist(site)
    result = integ.verify_record("demo-pkg")
    assert result.intact and result.checked == 2 and result.problems == ()


def test_changed_file_is_damaged_without_leaking_the_full_path(site, tmp_path):
    make_dist(site)
    (site / "demo_pkg" / "mod.py").write_bytes(b"y = 3\n")
    result = integ.verify_record("demo_pkg")
    assert result.record == integ.DAMAGED and "1 file(s) changed" in result.problems[0]
    assert "demo_pkg/mod.py" in result.problems[0]
    assert str(tmp_path) not in " ".join(result.problems)


def test_missing_file_is_damaged(site):
    make_dist(site)
    (site / "demo_pkg" / "__init__.py").unlink()
    result = integ.verify_record("demo_pkg")
    assert result.record == integ.DAMAGED and "1 file(s) missing" in result.problems[0]


def test_same_size_different_content_is_still_caught(site):
    make_dist(site)
    (site / "demo_pkg" / "mod.py").write_bytes(b"y = 9\n")  # same length as b"y = 2\n"
    assert integ.verify_record("demo_pkg").record == integ.DAMAGED


def test_extra_files_are_not_a_problem(site):
    make_dist(site)
    (site / "demo_pkg" / "__pycache__").mkdir()
    (site / "demo_pkg" / "notes.txt").write_text("added later")
    assert integ.verify_record("demo_pkg").intact


def test_no_record_or_no_hashes_is_unverifiable_not_intact(site):
    make_dist(site, name="no_record", record=False, files={"no_record.py": b"a"})
    assert integ.verify_record("no_record").record == integ.UNVERIFIABLE
    make_dist(site, name="no_hash", hashes=False, files={"no_hash.py": b"a"})
    assert integ.verify_record("no_hash").record == integ.UNVERIFIABLE


def test_unknown_distribution_is_unverifiable(site):
    result = integ.verify_record("surely-not-installed-xyz")
    assert result.record == integ.UNVERIFIABLE and not result.intact


def test_garbage_record_never_raises(site):
    info = make_dist(site)
    (info / "RECORD").write_bytes(
        b"\xff\xfe\x00 not,csv\n\x00,\x00,\x00,\x00\n../../../etc/passwd,sha256=zz,1\n"
    )
    assert integ.verify_record("demo_pkg").record in {integ.UNVERIFIABLE, integ.DAMAGED}


def test_entries_outside_site_packages_are_checked_too(site, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "tool"
    script.write_bytes(b"#!/x\n")
    info = make_dist(site)
    (info / "RECORD").write_text(
        f"demo_pkg/__init__.py,{_h(b'x = 1' + chr(10).encode())},6\n"
        f"../bin/tool,{_h(b'#!/x' + chr(10).encode())},5\n"
    )
    assert integ.verify_record("demo_pkg").intact
    script.write_bytes(b"#!/changed\n")
    assert integ.verify_record("demo_pkg").record == integ.DAMAGED


def test_list_distributions_is_sorted_and_normalised(site):
    make_dist(site, name="Zed_Pkg", files={"zed_pkg.py": b"1"})
    make_dist(site, name="alpha_pkg", files={"alpha_pkg.py": b"1"})
    names = [n for n, _ in integ.list_distributions() or ()]
    assert "zed-pkg" in names and "alpha-pkg" in names and names == sorted(names)


def test_check_installation_combines_both(site):
    make_dist(site)
    result = integ.check_installation("demo_pkg")
    assert result.intact and ("demo-pkg", "1.0") in (result.distributions or ())
    (site / "demo_pkg" / "mod.py").unlink()
    again = integ.check_installation("demo_pkg")
    assert again.record == integ.DAMAGED and again.distributions == result.distributions


def test_listing_failure_is_none_not_an_exception(site, monkeypatch):
    def boom():
        raise RuntimeError("C:\\Users\\bob\\secret")

    monkeypatch.setattr(integ.importlib_metadata, "distributions", boom)
    assert integ.list_distributions() is None


def test_module_never_writes_or_runs_anything():
    source = Path(integ.__file__).read_text(encoding="utf-8")
    for word in ("unlink", "rmtree", "write_text", "write_bytes", "subprocess", "os.remove"):
        assert word not in source, word
