"""Plugin skill parser — reads ``SKILL.md`` files from ``skills/*/`` directories.

Skills inject specialised knowledge into the model's context.  Each skill
lives in its own sub-directory under ``skills/``:

    skills/
    └── best-practices/
        └── SKILL.md           # required

``SKILL.md`` format::

    ---
    name: Best Practices
    description: Enforces team coding standards
    triggers: ["code review", "write code", "best practice"]
    always: false              # if true, always inject regardless of triggers
    ---

    When writing code for this project:
    1. Always add type hints
    2. Never use print() — use logging
    3. All functions must have docstrings

Skills are injected into the system prompt when:
- ``always: true`` — every turn
- ``always: false`` (default) — when the user's message contains a trigger phrase
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velune.plugins.declarative.command import _parse_simple_yaml, _FRONTMATTER_RE

logger = logging.getLogger("velune.plugins.declarative.skill")


@dataclass
class PluginSkill:
    """A context-injection skill contributed by a plugin."""

    slug: str                          # directory name, kebab-case
    plugin_name: str
    name: str = ""                     # human-readable name from frontmatter
    description: str = ""
    body: str = ""                     # content injected into context
    triggers: list[str] = field(default_factory=list)
    always: bool = False               # inject every turn regardless of triggers
    source_file: Path = field(default_factory=Path)

    def matches(self, user_text: str) -> bool:
        """Return True if this skill should be injected for *user_text*."""
        if self.always:
            return True
        if not self.triggers:
            return False
        lower = user_text.lower()
        return any(t.lower() in lower for t in self.triggers)

    @property
    def context_block(self) -> str:
        """The text to inject into the model's context (title + body)."""
        title = self.name or self.slug
        return f"## Skill: {title}\n\n{self.body}"

    @property
    def help_label(self) -> str:
        return f"(plugin:{self.plugin_name})"


def parse_skill_file(skill_dir: Path, plugin_name: str) -> PluginSkill | None:
    """Parse a ``SKILL.md`` inside *skill_dir* and return a ``PluginSkill``."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    try:
        raw = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read skill file %s: %s", skill_md, exc)
        return None

    fm: dict[str, Any] = {}
    body = raw

    m = _FRONTMATTER_RE.match(raw)
    if m:
        fm = _parse_simple_yaml(m.group(1))
        body = raw[m.end():]

    slug = skill_dir.name.lower()
    name = str(fm.get("name", slug))
    description = str(fm.get("description", ""))
    triggers_raw = fm.get("triggers", fm.get("trigger", []))
    triggers = [triggers_raw] if isinstance(triggers_raw, str) else list(triggers_raw)
    always = bool(fm.get("always", False))

    return PluginSkill(
        slug=slug,
        plugin_name=plugin_name,
        name=name,
        description=description,
        body=body.strip(),
        triggers=triggers,
        always=always,
        source_file=skill_md,
    )


def load_plugin_skills(skills_dir: Path, plugin_name: str) -> list[PluginSkill]:
    """Scan *skills_dir* for ``*/SKILL.md`` files and return all valid skills."""
    if not skills_dir.exists() or not skills_dir.is_dir():
        return []

    skills: list[PluginSkill] = []
    for sub in sorted(skills_dir.iterdir()):
        if not sub.is_dir():
            continue
        skill = parse_skill_file(sub, plugin_name)
        if skill is not None:
            skills.append(skill)
            logger.debug("Loaded plugin skill '%s' from %s", skill.name, sub)

    return skills
