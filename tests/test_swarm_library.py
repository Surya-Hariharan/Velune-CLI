"""Skills and permission profiles for the council swarm (velune/swarm/library.py)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from velune.swarm.library import LibraryError, SkillLibrary, parse_flow

SKILL = """---
name: Docs
description: Write documentation
triggers: [document, docs]
team: [reader, author]
flow: "reader > author"
lead: author
default_permission: read-only
output: text
---
Shared knowledge.

## reader
Read the code.

## author
Write the docs.
"""

PROFILE = """---
name: Notes
description: Read only, few calls
tools: [read]
write: none
commands: none
max_tool_calls: 5
---
Guidance.
"""


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _load(tmp_path: Path, *, trusted: bool = False, **kwargs) -> SkillLibrary:
    return SkillLibrary.load(
        workspace=tmp_path / "repo",
        trusted=trusted,
        user_root=tmp_path / "home" / ".velune",
        **kwargs,
    )


# ── the built-in library ─────────────────────────────────────────────────────


def test_builtin_skills_and_profiles_load_cleanly(tmp_path):
    library = _load(tmp_path)
    assert library.warnings == []
    assert set(library.skills) == {"coding", "planning", "review", "research"}
    assert set(library.profiles) == {
        "answer-only",
        "read-only",
        "propose-changes",
        "edit-workspace",
        "build-project",
        "integrations",
    }
    for skill in library.skills.values():
        assert skill.source == "builtin"
        assert skill.default_permission in library.profiles
        assert skill.flow[-1] == (skill.lead,)
        assert all(skill.roles[role] for role in skill.team)


def test_coding_runs_implementer_and_tester_together():
    library = SkillLibrary.load(user_root=Path("does-not-exist"))
    coding = library.skill("coding")
    assert coding is not None
    assert coding.flow == (("explorer",), ("implementer", "tester"), ("reviewer",))
    assert coding.writers == ("implementer", "tester")
    assert coding.output == "diff"


def test_the_career_question_matches_research_triggers():
    library = SkillLibrary.load(user_root=Path("does-not-exist"))
    question = "how valid is this project for my AI and bioinformatics career path"
    assert [s.slug for s in library.skills.values() if s.matches(question)] == ["research"]


# ── user and project files ───────────────────────────────────────────────────


def test_user_files_add_new_skills_and_profiles(tmp_path):
    user = tmp_path / "home" / ".velune"
    _write(user, "skills/docs/SKILL.md", SKILL)
    _write(user, "permissions/notes.md", PROFILE)
    library = _load(tmp_path)
    assert library.skill("docs").source == "user"
    assert library.skill("docs").roles == {"reader": "Read the code.", "author": "Write the docs."}
    assert library.skill("docs").body == "Shared knowledge."
    assert library.profile("notes").max_tool_calls == 5
    assert library.profile("notes") in library.auto_profiles()


def test_project_files_load_only_for_a_trusted_workspace(tmp_path):
    repo = tmp_path / "repo" / ".velune"
    _write(repo, "skills/docs/SKILL.md", SKILL)
    _write(repo, "permissions/notes.md", PROFILE)
    assert _load(tmp_path, trusted=False).skill("docs") is None
    trusted = _load(tmp_path, trusted=True)
    assert trusted.skill("docs").source == "project"
    assert trusted.profile("notes").source == "project"


def test_the_router_never_auto_picks_a_project_profile(tmp_path):
    _write(tmp_path / "repo" / ".velune", "permissions/notes.md", PROFILE)
    library = _load(tmp_path, trusted=True)
    assert library.profile("notes") is not None
    assert "notes" not in {p.slug for p in library.auto_profiles()}


@pytest.mark.parametrize("where", ["home", "repo"])
def test_a_built_in_name_cannot_be_replaced(tmp_path, where):
    wider = PROFILE.replace("tools: [read]", "tools: [read, write]").replace(
        "write: none", "write: workspace"
    )
    _write(tmp_path / where / ".velune", "permissions/read-only.md", wider)
    library = _load(tmp_path, trusted=True)
    assert library.profile("read-only").source == "builtin"
    assert library.profile("read-only").write == "none"
    assert any("may add new names but not replace one" in w for w in library.warnings)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda t: t.replace("team: [reader, author]", "team: []"), "lists no roles"),
        (lambda t: t.replace('"reader > author"', '"reader > ghost"'), "not in the team"),
        (lambda t: t.replace('"reader > author"', '"reader > author > reader"'), "more than once"),
        (lambda t: t.replace('"reader > author"', '"author > reader"'), "alone in the last stage"),
        (lambda t: t.replace("## author\nWrite the docs.\n", ""), "no '## author' section"),
        (lambda t: t.replace("output: text", "output: video"), "'output' must be one of"),
        (lambda t: t.split("---\nShared")[1], "no '---' frontmatter"),
    ],
)
def test_a_malformed_skill_is_skipped_with_a_reason(tmp_path, change, message):
    _write(tmp_path / "home" / ".velune", "skills/docs/SKILL.md", change(SKILL))
    library = _load(tmp_path)
    assert library.skill("docs") is None
    assert any(message in w for w in library.warnings), library.warnings
    assert library.skill("coding") is not None  # the rest still loads


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda t: t.replace("tools: [read]", "tools: [read, write]"), "write is not 'workspace'"),
        (lambda t: t.replace("tools: [read]", "tools: [read, shell]"), "commands is 'none'"),
        (lambda t: t.replace("write: none", "write: everything"), "'write' must be one of"),
        (lambda t: t.replace("max_tool_calls: 5", "max_tool_calls: lots"), "whole number"),
    ],
)
def test_a_malformed_profile_is_skipped_with_a_reason(tmp_path, change, message):
    _write(tmp_path / "home" / ".velune", "permissions/notes.md", change(PROFILE))
    library = _load(tmp_path)
    assert library.profile("notes") is None
    assert any(message in w for w in library.warnings), library.warnings


def test_a_skill_naming_a_missing_profile_falls_back_to_read_only(tmp_path):
    _write(
        tmp_path / "home" / ".velune",
        "skills/docs/SKILL.md",
        SKILL.replace("default_permission: read-only", "default_permission: nowhere"),
    )
    library = _load(tmp_path)
    assert library.skill("docs").default_permission == "read-only"
    assert any("'nowhere' does not exist" in w for w in library.warnings)


def test_a_plugin_skill_becomes_a_one_agent_read_only_skill(tmp_path):
    plugin = SimpleNamespace(
        slug="team-style",
        name="Team style",
        description="House rules",
        body="Use type hints.",
        triggers=["style"],
        source_file=None,
    )
    skill = _load(tmp_path, plugin_skills=[plugin]).skill("team-style")
    assert skill.source == "plugin"
    assert skill.flow == (("worker",),)
    assert skill.default_permission == "read-only"


# ── flow parsing ─────────────────────────────────────────────────────────────


def test_flow_parses_stages_in_order():
    assert parse_flow("a > b, c > d", ["a", "b", "c", "d"]) == (("a",), ("b", "c"), ("d",))


@pytest.mark.parametrize(
    ("flow", "message"),
    [
        ("a > > b", "empty stage"),
        ("a > x", "not in the team"),
        ("a, a > b", "more than once"),
        ("a", "leaves out b"),
    ],
)
def test_flow_rejects_bad_input(flow, message):
    with pytest.raises(LibraryError, match=message):
        parse_flow(flow, ["a", "b"])


# ── /skills ──────────────────────────────────────────────────────────────────


def _repl(tmp_path: Path, width: int = 160):
    import io

    from rich.console import Console

    container = SimpleNamespace(get=lambda key: str(tmp_path))
    return SimpleNamespace(
        console=Console(file=io.StringIO(), width=width), container=container, _plugin_manager=None
    )


async def test_skills_lists_every_skill_and_profile(tmp_path):
    from velune.cli.handlers.skills import cmd_skills

    repl = _repl(tmp_path)
    await cmd_skills(repl, "")
    out = repl.console.file.getvalue()
    for name in ("coding", "planning", "review", "research", "answer-only", "build-project"):
        assert name in out
    assert "explorer → implementer, tester → reviewer" in out


async def test_skills_show_prints_one_skill_with_its_roles(tmp_path):
    from velune.cli.handlers.skills import cmd_skills

    repl = _repl(tmp_path)
    await cmd_skills(repl, "show coding")
    out = repl.console.file.getvalue()
    assert "Writers: implementer, tester" in out and "reviewer" in out
    await cmd_skills(repl, "show nothing-here")
    assert "No skill or permission profile named 'nothing-here'" in repl.console.file.getvalue()


async def test_skills_keeps_names_whole_in_a_narrow_terminal(tmp_path):
    from velune.cli.handlers.skills import cmd_skills

    repl = _repl(tmp_path, width=60)
    await cmd_skills(repl, "")
    out = repl.console.file.getvalue()
    for name in ("build-project", "propose-changes", "edit-workspace", "answer-only", "planning"):
        assert name in out
    assert "…" not in out
