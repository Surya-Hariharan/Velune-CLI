"""F2: purge must not delete through a link swapped in after the preview.

The audit showed a planned subdirectory replaced by a junction after ``build_plan``: the
file inside matched size and mtime, so ``execute_plan`` unlinked a file *outside* the
approved root. ``execute_plan`` now re-validates the whole chain (root identity, every
directory down to the parent, the leaf's identity) immediately before each deletion.

Everything here uses temporary directories and synthetic files. Link tests that need a
privilege this machine lacks are skipped and reported as skips, never as passes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from velune.lifecycle import purge

ON_WINDOWS = sys.platform.startswith("win")


def write(path: Path, text: str = "x", mtime_ns: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def snap(base: Path) -> dict[str, str]:
    return {
        str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(base.rglob("*"))
        if p.is_file()
    }


def make_link(link: Path, target: Path, kind: str) -> None:
    """Create a junction (Windows) or a symlink at *link* (which must not exist)."""
    if kind == "junction":
        if not ON_WINDOWS:
            pytest.skip("junctions are a Windows feature")
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            pytest.skip("could not create a junction here")
        return
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need a privilege this environment lacks")


def swap_dir_for_link(directory: Path, target: Path, kind: str) -> Path:
    """Move the real directory aside and put a link where it was; returns the moved copy."""
    aside = directory.with_name(directory.name + ".aside")
    os.replace(directory, aside)
    try:
        make_link(directory, target, kind)
    except BaseException:
        os.replace(aside, directory)  # leave the fixture as it was before skipping
        raise
    return aside


LINKS = ["junction", "symlink"]


@pytest.fixture
def env(tmp_path):
    home, cfg, data = tmp_path / "home" / ".velune", tmp_path / "cfg", tmp_path / "data"
    cfg.mkdir()
    data.mkdir()
    outside = tmp_path / "outside"
    write(outside / "precious.txt", "do not delete")
    write(home / "sessions" / "top.json", "top")
    write(home / "sessions" / "sub" / "f1.json", "one", mtime_ns=1_700_000_000_000_000_000)
    write(home / "sessions" / "a" / "b" / "deep.json", "deep", mtime_ns=1_700_000_000_000_000_000)
    write(home / "logs" / "l.log", "log")
    write(home / "velune.toml", "keep me")  # config category: never selected here
    roots = purge.Roots(home, cfg, data)
    return type("Env", (), {"roots": roots, "tmp": tmp_path, "home": home, "outside": outside})()


def plan_sessions(env):
    plan = purge.build_plan(("sessions",), env.roots)
    assert not plan.root_problems and not plan.anomalies
    assert plan.roots, "the plan must carry the validated root and its identity"
    return plan


def mirror(src: Path, dst: Path) -> None:
    """Copy a tree preserving mtimes, so only identity can tell the copy from the original."""
    shutil.copytree(src, dst, copy_function=shutil.copy2)


# ---- the audit scenario ---------------------------------------------------------------


@pytest.mark.parametrize("kind", LINKS)
def test_subdirectory_swapped_for_a_link_after_preview_deletes_nothing_outside(env, kind):
    plan = plan_sessions(env)
    mirror(env.home / "sessions" / "sub", env.outside / "sub")  # same names, sizes and mtimes
    before = snap(env.outside)
    swap_dir_for_link(env.home / "sessions" / "sub", env.outside / "sub", kind)
    report = purge.execute_plan(plan)
    assert report.stopped and report.failed == [("sessions", "changed_since_preview")]
    assert snap(env.outside) == before
    assert (env.outside / "sub" / "f1.json").exists() and (env.outside / "precious.txt").exists()


@pytest.mark.parametrize("kind", LINKS)
def test_ancestor_link_several_levels_up_is_caught(env, kind):
    plan = plan_sessions(env)
    mirror(env.home / "sessions" / "a", env.outside / "a")
    before = snap(env.outside)
    swap_dir_for_link(env.home / "sessions" / "a", env.outside / "a", kind)
    report = purge.execute_plan(plan)
    assert report.stopped
    assert snap(env.outside) == before


@pytest.mark.parametrize("kind", LINKS)
def test_the_planned_target_directory_itself_swapped_for_a_link(env, kind):
    plan = plan_sessions(env)
    mirror(env.home / "sessions", env.outside / "sessions")
    before = snap(env.outside)
    swap_dir_for_link(env.home / "sessions", env.outside / "sessions", kind)
    report = purge.execute_plan(plan)
    assert report.stopped and snap(env.outside) == before


@pytest.mark.parametrize("kind", LINKS)
def test_root_replaced_by_a_link_deletes_nothing_behind_it(env, kind):
    plan = plan_sessions(env)
    elsewhere = env.tmp / "elsewhere"
    mirror(env.home, elsewhere)
    before = snap(elsewhere)
    swap_dir_for_link(env.home, elsewhere, kind)
    report = purge.execute_plan(plan)
    assert report.stopped and snap(elsewhere) == before


def test_root_replaced_by_a_different_real_directory_is_detected(env):
    plan = plan_sessions(env)
    os.replace(env.home, env.home.with_name(".velune.old"))
    mirror(env.home.with_name(".velune.old"), env.home)  # identical content, new identity
    before = snap(env.home)
    report = purge.execute_plan(plan)
    assert report.stopped and snap(env.home) == before


def test_nested_directory_replaced_by_an_identical_looking_real_directory(env):
    plan = plan_sessions(env)
    sub = env.home / "sessions" / "sub"
    os.replace(sub, sub.with_name("sub.old"))
    mirror(sub.with_name("sub.old"), sub)  # same names, sizes, mtimes; different directory
    report = purge.execute_plan(plan)
    assert report.stopped
    assert (sub / "f1.json").exists()


def test_file_replaced_with_same_size_and_mtime_is_detected_by_identity(env):
    plan = plan_sessions(env)
    target = env.home / "sessions" / "sub" / "f1.json"
    st = target.stat()
    other = write(target.with_name("other.tmp"), "two", mtime_ns=st.st_mtime_ns)
    assert other.stat().st_size == st.st_size
    os.replace(other, target)  # same size, same mtime_ns, different file
    report = purge.execute_plan(plan)
    assert report.stopped and target.read_text(encoding="utf-8") == "two"


# ---- mid-run swaps and failures -----------------------------------------------------------


@pytest.mark.parametrize("kind", LINKS)
def test_link_swapped_in_between_two_deletions(env, kind):
    plan = plan_sessions(env)
    mirror(env.home / "sessions" / "a", env.outside / "a")
    before = snap(env.outside)
    calls = {"n": 0}

    def sneaky_remove(path: Path) -> None:
        os.unlink(path)
        calls["n"] += 1
        if calls["n"] == 1:
            a = env.home / "sessions" / "a"
            if a.exists() and not os.path.islink(a):
                swap_dir_for_link(a, env.outside / "a", kind)

    report = purge.execute_plan(plan, remove_file=sneaky_remove)
    assert snap(env.outside) == before
    assert report.stopped or calls["n"] >= 1


def test_deletion_failure_midway_stops_and_reports_the_rest_untouched(env):
    plan = plan_sessions(env)
    seen = {"n": 0}

    def flaky(path: Path) -> None:
        seen["n"] += 1
        if seen["n"] == 2:
            raise PermissionError("locked")
        os.unlink(path)

    report = purge.execute_plan(plan, remove_file=flaky)
    assert report.stopped == "a deletion failed, so deletion stopped"
    assert report.failed == [("sessions", "PermissionError")] and report.not_attempted
    assert (env.home / "sessions").exists() and (env.outside / "precious.txt").exists()
    assert (env.home / "velune.toml").exists()


def test_a_normal_purge_still_removes_everything_selected_and_only_that(env):
    plan = plan_sessions(env)
    before_outside = snap(env.outside)
    report = purge.execute_plan(plan)
    assert report.stopped is None and not report.failed
    assert not (env.home / "sessions").exists()
    assert (env.home / "logs" / "l.log").exists() and (env.home / "velune.toml").exists()
    assert snap(env.outside) == before_outside


def test_workspace_slugs_are_removed_when_empty_and_checked(env):
    slug = env.roots.data_dir / "workspaces" / "proj-0123456789"
    from velune.core.paths import COGNITIVE_DB_NAME

    write(slug / COGNITIVE_DB_NAME, "db")
    plan = purge.build_plan(("memory",), env.roots)
    assert not plan.anomalies and plan.workspace_dirs
    report = purge.execute_plan(plan)
    assert report.stopped is None
    assert not slug.exists()


# ---- crafted plans: containment must not rely on string prefixes -------------------------


def crafted(env, path: Path, kind: str = "file") -> purge.Plan:
    info = purge.RootInfo(env.home, purge._root_ident(env.home))
    st = os.lstat(path)
    item = purge.Item(path, kind, st.st_size, st.st_mtime_ns, purge._ident(st))
    target = purge.Target("sessions", path, [item], root_label=purge.HOME)
    return purge.Plan(selected=("sessions",), targets=[target], roots={purge.HOME: info})


def test_dotdot_path_that_escapes_the_root_is_refused(env):
    escaping = env.home / "sessions" / ".." / ".." / ".." / "outside" / "precious.txt"
    plan = crafted(env, escaping)
    report = purge.execute_plan(plan)
    assert report.stopped and (env.outside / "precious.txt").exists()


def test_similar_prefix_sibling_is_not_inside_the_root(env):
    evil = env.home.with_name(env.home.name + "-evil")
    victim = write(evil / "sessions" / "x.json", "not yours")
    plan = crafted(env, victim)
    report = purge.execute_plan(plan)
    assert report.stopped and victim.exists()


def test_a_path_outside_the_root_entirely_is_refused(env):
    plan = crafted(env, env.outside / "precious.txt")
    report = purge.execute_plan(plan)
    assert report.stopped and (env.outside / "precious.txt").exists()


def test_missing_root_identity_fails_closed(env):
    plan = plan_sessions(env)
    plan.roots = {k: purge.RootInfo(v.path, None) for k, v in plan.roots.items()}
    report = purge.execute_plan(plan)
    assert report.stopped
    assert (env.home / "sessions" / "top.json").exists()


def test_a_plan_without_root_information_fails_closed(env):
    plan = plan_sessions(env)
    plan.roots = {}
    report = purge.execute_plan(plan)
    assert report.stopped and (env.home / "sessions" / "top.json").exists()


def test_chain_check_unit_cases(env):
    info = purge.RootInfo(env.home, purge._root_ident(env.home))
    good = env.home / "sessions" / "sub" / "f1.json"
    assert purge._chain_ok(info, good)
    assert not purge._chain_ok(info, env.home)  # the root itself is never a deletion target
    assert not purge._chain_ok(None, good)
    assert not purge._chain_ok(info, env.outside / "precious.txt")
