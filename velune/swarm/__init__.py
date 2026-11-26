"""Skills-based agent swarm behind ``/council``.

A *skill* (``skills/<slug>/SKILL.md``) says how to do a kind of task: shared
knowledge, a default team of roles, the order they run in, and the output it
produces. A *permission profile* (``permissions/<slug>.md``) says what the
agents may do for that task. For each run one skill and one profile are chosen,
a team is built, and the agents run in stages.
"""

from velune.swarm.library import (
    LibraryError,
    PermissionProfile,
    Skill,
    SkillLibrary,
    parse_flow,
)

__all__ = ["LibraryError", "PermissionProfile", "Skill", "SkillLibrary", "parse_flow"]
