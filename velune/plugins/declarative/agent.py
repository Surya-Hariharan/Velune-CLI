"""Plugin agent parser — reads ``.md`` files from a plugin's ``agents/`` dir.

Each file defines a *sub-agent* that can be invoked from a command or
auto-selected by the orchestrator.  Format:

    ---
    name: code-reviewer
    description: Reviews code for bugs and adherence to conventions
    tools: Glob, Grep, Read          # comma-sep list of tool names
    model: sonnet                    # model hint (maps to Velune model IDs)
    color: red                       # display colour hint
    ---

    Detailed system-prompt-style instructions the agent follows...

The ``description`` field drives auto-selection: when a user prompt or
command matches the description, this agent can be preferred.
The ``tools`` list narrows which tools the agent may call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velune.plugins.declarative.command import _FRONTMATTER_RE, _parse_simple_yaml

logger = logging.getLogger("velune.plugins.declarative.agent")


@dataclass
class PluginAgent:
    """A sub-agent definition contributed by a plugin."""

    name: str
    plugin_name: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    model: str = ""  # model hint, e.g. "sonnet", "opus"
    color: str = ""  # UI colour hint
    instructions: str = ""  # body of the markdown file
    source_file: Path = field(default_factory=Path)

    @property
    def help_label(self) -> str:
        return f"(agent:{self.plugin_name})"

    @property
    def system_block(self) -> str:
        """Formatted block for injection as a system message when this agent is active."""
        lines = [f"## Agent: {self.name}"]
        if self.description:
            lines.append(f"*{self.description}*")
        if self.tools:
            lines.append(f"Available tools: {', '.join(self.tools)}")
        lines.append("")
        lines.append(self.instructions)
        return "\n".join(lines)

    def matches(self, user_text: str) -> bool:
        """Very simple trigger: true if any word from description appears in text."""
        if not self.description:
            return False
        lower = user_text.lower()
        return any(w.lower() in lower for w in self.description.split() if len(w) > 4)


def parse_agent_file(path: Path, plugin_name: str) -> PluginAgent | None:
    """Parse an agent ``.md`` file and return a ``PluginAgent``.

    Returns ``None`` on parse failure (logs warning).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read agent file %s: %s", path, exc)
        return None

    fm: dict[str, Any] = {}
    body = raw

    m = _FRONTMATTER_RE.match(raw)
    if m:
        fm = _parse_simple_yaml(m.group(1))
        body = raw[m.end() :]

    # Name: prefer frontmatter, fall back to filename stem
    name = str(fm.get("name", path.stem.lower().replace(" ", "-")))

    # Tools list
    tools_raw = fm.get("tools", fm.get("tool", []))
    if isinstance(tools_raw, str):
        tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
    else:
        tools = list(tools_raw)

    return PluginAgent(
        name=name,
        plugin_name=plugin_name,
        description=str(fm.get("description", "")),
        tools=tools,
        model=str(fm.get("model", "")),
        color=str(fm.get("color", "")),
        instructions=body.strip(),
        source_file=path,
    )


def load_plugin_agents(agents_dir: Path, plugin_name: str) -> list[PluginAgent]:
    """Scan *agents_dir* and return all valid ``PluginAgent`` objects."""
    if not agents_dir.exists() or not agents_dir.is_dir():
        return []

    agents: list[PluginAgent] = []
    for md_file in sorted(agents_dir.glob("*.md")):
        agent = parse_agent_file(md_file, plugin_name)
        if agent is not None:
            agents.append(agent)
            logger.debug("Loaded plugin agent '%s' from %s", agent.name, md_file)
    return agents
