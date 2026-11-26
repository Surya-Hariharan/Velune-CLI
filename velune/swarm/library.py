"""Load skills and permission profiles from their four locations.

Load order: built-in (shipped with Velune), user (``~/.velune``), project
(``<repo>/.velune``, trusted workspaces only), then plugin skills. A user or
project file may add new skills and profiles but may not reuse a built-in name:
such a file is skipped with a warning, so nothing can quietly redefine what a
built-in profile allows. Every item records where it came from (``source``);
the router only auto-picks ``builtin`` and ``user`` profiles.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velune.plugins.declarative.command import _FRONTMATTER_RE, _parse_simple_yaml

logger = logging.getLogger("velune.swarm.library")

BUILTIN_ROOT = Path(__file__).resolve().parent
OUTPUTS = ("text", "plan", "diff", "files", "diagram")
WRITE_MODES = ("none", "proposal", "workspace")
COMMAND_MODES = ("none", "ask", "allowlist")
TOOL_GROUPS = ("read", "search", "write", "shell", "web", "integrations")
MAX_AGENTS = 5
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class LibraryError(ValueError):
    """A skill or permission file is malformed."""


@dataclass(frozen=True)
class Skill:
    """How to do one kind of task (parsed from ``SKILL.md``)."""

    slug: str
    name: str
    description: str
    triggers: tuple[str, ...]
    team: tuple[str, ...]
    flow: tuple[tuple[str, ...], ...]  # stages of role names
    lead: str
    writers: tuple[str, ...]
    default_permission: str
    output: str
    max_agents: int
    body: str  # knowledge shared by the whole team
    roles: dict[str, str] = field(hash=False, compare=False)  # role -> its instructions
    source: str = "builtin"
    path: Path | None = None

    def matches(self, text: str) -> bool:
        lower = text.lower()
        return any(t.lower() in lower for t in self.triggers)


@dataclass(frozen=True)
class PermissionProfile:
    """What agents may do for a task (parsed from ``permissions/<slug>.md``)."""

    slug: str
    name: str
    description: str
    tools: frozenset[str]
    mcp: tuple[str, ...]  # () none, ("*",) all connected servers, or server names
    write: str
    commands: str
    max_tool_calls: int
    body: str
    source: str = "builtin"
    path: Path | None = None


# ── parsing ─────────────────────────────────────────────────────────────────


def _split_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LibraryError(f"cannot read {path.name}: {exc}") from exc
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise LibraryError(f"{path.name} has no '---' frontmatter block")
    try:
        meta = _parse_simple_yaml(match.group(1))
    except ValueError as exc:
        raise LibraryError(f"{path.name}: invalid frontmatter ({exc})") from exc
    return meta, raw[match.end() :].strip()


def _text(meta: dict[str, Any], key: str, default: str = "") -> str:
    value = meta.get(key, default)
    return str(value).strip() if value is not None else default


def _names(meta: dict[str, Any], key: str) -> tuple[str, ...]:
    """A list field: accepts ``[a, b]``, a block list or a comma-separated string."""
    value = meta.get(key) or []
    items = value.split(",") if isinstance(value, str) else list(value)
    return tuple(str(v).strip().lower() for v in items if str(v).strip())


def _choice(meta: dict[str, Any], key: str, allowed: tuple[str, ...], default: str) -> str:
    value = _text(meta, key, default).lower()
    if value not in allowed:
        raise LibraryError(f"'{key}' must be one of {', '.join(allowed)} (got '{value}')")
    return value


def _int(meta: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(meta.get(key, default))
    except (TypeError, ValueError) as exc:
        raise LibraryError(f"'{key}' must be a whole number") from exc
    if not low <= value <= high:
        raise LibraryError(f"'{key}' must be between {low} and {high}")
    return value


def parse_flow(text: str, known: Iterable[str]) -> tuple[tuple[str, ...], ...]:
    """Parse ``"a > b, c > d"`` into stages ``(("a",), ("b", "c"), ("d",))``.

    ``>`` means "then"; ``,`` means "at the same time". Every name must be
    known and appear exactly once.
    """
    known_set = set(known)
    stages: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for raw_stage in str(text).split(">"):
        stage = tuple(n.strip().lower() for n in raw_stage.split(",") if n.strip())
        if not stage:
            raise LibraryError(f"flow '{text}' has an empty stage")
        for name in stage:
            if name not in known_set:
                raise LibraryError(f"flow names '{name}', which is not in the team")
            if name in seen:
                raise LibraryError(f"flow names '{name}' more than once")
            seen.add(name)
        stages.append(stage)
    missing = known_set - seen
    if missing:
        raise LibraryError(f"flow leaves out {', '.join(sorted(missing))}")
    return tuple(stages)


def _role_sections(body: str, team: tuple[str, ...]) -> tuple[str, dict[str, str]]:
    """Split the body into shared text and one section per ``## <role>`` heading."""
    shared: list[str] = []
    roles: dict[str, list[str]] = {}
    current: list[str] = shared
    for line in body.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        name = heading.group(1).strip().lower() if heading else ""
        if heading and name in team:
            current = roles.setdefault(name, [])
            continue
        current.append(line)
    return "\n".join(shared).strip(), {k: "\n".join(v).strip() for k, v in roles.items()}


def parse_skill(path: Path, source: str) -> Skill:
    """Parse one ``SKILL.md``; the folder name is the slug."""
    meta, body = _split_frontmatter(path)
    slug = path.parent.name.lower()
    if not _NAME_RE.match(slug):
        raise LibraryError(f"skill folder '{slug}' must be lowercase letters, digits and '-'")
    team = _names(meta, "team")
    if not team:
        raise LibraryError("'team' lists no roles")
    max_agents = _int(meta, "max_agents", min(len(team), MAX_AGENTS), 1, MAX_AGENTS)
    if len(team) > max_agents:
        raise LibraryError(f"team has {len(team)} roles but max_agents is {max_agents}")
    flow = parse_flow(_text(meta, "flow") or " > ".join(team), team)
    lead = _text(meta, "lead", flow[-1][0]).lower()
    if flow[-1] != (lead,):
        raise LibraryError(f"the lead '{lead}' must be alone in the last stage of the flow")
    writers = _names(meta, "writers")
    if set(writers) - set(team):
        raise LibraryError("'writers' names a role that is not in the team")
    shared, roles = _role_sections(body, team)
    missing = [r for r in team if not roles.get(r)]
    if missing:
        raise LibraryError(f"no '## {missing[0]}' section with instructions")
    return Skill(
        slug=slug,
        name=_text(meta, "name", slug),
        description=_text(meta, "description"),
        triggers=_names(meta, "triggers"),
        team=team,
        flow=flow,
        lead=lead,
        writers=writers,
        default_permission=_text(meta, "default_permission", "read-only").lower(),
        output=_choice(meta, "output", OUTPUTS, "text"),
        max_agents=max_agents,
        body=shared,
        roles=roles,
        source=source,
        path=path,
    )


def parse_profile(path: Path, source: str) -> PermissionProfile:
    """Parse one ``permissions/<slug>.md``; the file name is the slug."""
    meta, body = _split_frontmatter(path)
    slug = path.stem.lower()
    if not _NAME_RE.match(slug):
        raise LibraryError(f"profile '{slug}' must be lowercase letters, digits and '-'")
    tools = _names(meta, "tools")
    unknown = [t for t in tools if t not in TOOL_GROUPS and not re.match(r"^[a-z0-9_]+$", t)]
    if unknown:
        raise LibraryError(f"unknown tool entry '{unknown[0]}'")
    write = _choice(meta, "write", WRITE_MODES, "none")
    commands = _choice(meta, "commands", COMMAND_MODES, "none")
    if write != "workspace" and "write" in tools:
        raise LibraryError("tools lists 'write' but write is not 'workspace'")
    if commands == "none" and "shell" in tools:
        raise LibraryError("tools lists 'shell' but commands is 'none'")
    return PermissionProfile(
        slug=slug,
        name=_text(meta, "name", slug),
        description=_text(meta, "description"),
        tools=frozenset(tools),
        mcp=_names(meta, "mcp"),
        write=write,
        commands=commands,
        max_tool_calls=_int(meta, "max_tool_calls", 40, 0, 500),
        body=body,
        source=source,
        path=path,
    )


# ── the library ─────────────────────────────────────────────────────────────


@dataclass
class SkillLibrary:
    """All skills and profiles available for this workspace."""

    skills: dict[str, Skill] = field(default_factory=dict)
    profiles: dict[str, PermissionProfile] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def load(
        cls,
        *,
        workspace: Path | None = None,
        trusted: bool = False,
        user_root: Path | None = None,
        plugin_skills: Iterable[Any] = (),
        builtin_root: Path = BUILTIN_ROOT,
    ) -> SkillLibrary:
        """Load every location in order. Project files load only when *trusted*."""
        library = cls()
        roots: list[tuple[str, Path]] = [("builtin", builtin_root)]
        roots.append(("user", user_root or Path.home() / ".velune"))
        if workspace is not None and trusted:
            roots.append(("project", Path(workspace) / ".velune"))
        for source, root in roots:
            library._load_root(source, root)
        for plugin_skill in plugin_skills:
            library._add_plugin_skill(plugin_skill)
        for skill in list(library.skills.values()):
            if skill.default_permission not in library.profiles:
                library._warn(
                    skill.path,
                    f"default_permission '{skill.default_permission}' does not exist; "
                    "using read-only",
                )
                library.skills[skill.slug] = _replace_permission(skill, "read-only")
        return library

    def _warn(self, path: Path | None, message: str) -> None:
        text = f"{path}: {message}" if path else message
        self.warnings.append(text)
        logger.warning("Skipped or changed swarm file %s", text)

    def _load_root(self, source: str, root: Path) -> None:
        skills_dir, profiles_dir = root / "skills", root / "permissions"
        if profiles_dir.is_dir():
            for path in sorted(profiles_dir.glob("*.md")):
                self._add(source, path, parse_profile, self.profiles)
        if skills_dir.is_dir():
            for folder in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
                if (folder / "SKILL.md").is_file():
                    self._add(source, folder / "SKILL.md", parse_skill, self.skills)

    def _add(self, source: str, path: Path, parse: Any, into: dict[str, Any]) -> None:
        try:
            item = parse(path, source)
        except LibraryError as exc:
            self._warn(path, str(exc))
            return
        existing = into.get(item.slug)
        if existing is not None:
            self._warn(
                path,
                f"'{item.slug}' is already defined ({existing.source}); a {source} file "
                "may add new names but not replace one",
            )
            return
        into[item.slug] = item

    def _add_plugin_skill(self, plugin_skill: Any) -> None:
        """A plugin's context skill becomes a one-agent, read-only, text skill."""
        slug = str(getattr(plugin_skill, "slug", "") or "").lower()
        if not _NAME_RE.match(slug) or slug in self.skills:
            if slug in self.skills:
                self._warn(None, f"plugin skill '{slug}' clashes with an existing skill")
            return
        body = str(getattr(plugin_skill, "body", "") or "")
        self.skills[slug] = Skill(
            slug=slug,
            name=str(getattr(plugin_skill, "name", "") or slug),
            description=str(getattr(plugin_skill, "description", "") or ""),
            triggers=tuple(str(t).lower() for t in getattr(plugin_skill, "triggers", []) or []),
            team=("worker",),
            flow=(("worker",),),
            lead="worker",
            writers=(),
            default_permission="read-only",
            output="text",
            max_agents=1,
            body=body,
            roles={"worker": "Complete the task using the knowledge above."},
            source="plugin",
            path=getattr(plugin_skill, "source_file", None),
        )

    def skill(self, slug: str) -> Skill | None:
        return self.skills.get(slug.lower())

    def profile(self, slug: str) -> PermissionProfile | None:
        return self.profiles.get(slug.lower())

    def auto_profiles(self) -> list[PermissionProfile]:
        """Profiles the router may pick on its own (never project-defined ones)."""
        return [p for p in self.profiles.values() if p.source in ("builtin", "user")]


def _replace_permission(skill: Skill, permission: str) -> Skill:
    from dataclasses import replace

    return replace(skill, default_permission=permission)
