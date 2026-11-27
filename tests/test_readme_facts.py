"""The README graphics may only state what the repository can show.

Each graphic in ``docs/assets`` carries ``data-*`` attributes next to the figures it draws.
These tests recompute those figures from the code (provider registry, slash-command registry,
the CI workflow) and from the documented permission table, so a
graphic that drifts from the repository fails CI instead of quietly misleading a reader.
They also hold every graphic to the rules that keep it safe to embed: self-contained, no
script, no external reference, a viewBox, a title and a description, and a small size.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from velune.cli.slash_dispatcher import build_slash_registry
from velune.providers.registry import ProviderRegistry

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"
GRAPHICS = [
    "velune-questions.svg",
    "velune-council.svg",
    "velune-permissions.svg",
    "velune-numbers.svg",
    "velune-connect.svg",
]
MAX_BYTES = 60_000

#: Provider id -> the name drawn in velune-connect.svg, split the way the graphic splits them.
LOCAL = {
    "ollama": "Ollama",
    "lmstudio": "LM Studio",
    "llamacpp": "llama.cpp",
    "openai-compat": "OpenAI-compatible servers",
}
CLOUD = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "mistral": "Mistral",
    "xai": "xAI",
    "groq": "Groq",
    "openrouter": "OpenRouter",
    "together": "Together AI",
    "fireworks": "Fireworks AI",
    "deepseek": "DeepSeek",
    "cohere": "Cohere",
    "meta": "Meta",
    "nvidia": "NVIDIA NIM",
    "huggingface": "Hugging Face",
}


def _read(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def _tree(name: str) -> ET.Element:
    return ET.fromstring(_read(name))


def _with(root: ET.Element, attr: str) -> list[ET.Element]:
    return [el for el in root.iter() if attr in el.attrib]


def _facts() -> dict[str, str]:
    return {
        el.attrib["data-fact"]: el.attrib["data-value"]
        for el in _with(_tree("velune-numbers.svg"), "data-fact")
    }


# ── Safety and structure of every graphic ───────────────────────────────────────────────────


@pytest.mark.parametrize("name", GRAPHICS)
def test_graphic_is_a_small_self_contained_valid_svg(name):
    text = _read(name)
    root = ET.fromstring(text)  # raises on malformed XML
    assert root.tag.endswith("svg")
    assert re.fullmatch(r"0 0 \d+ \d+", root.attrib.get("viewBox", "")), "needs a viewBox"
    assert "width" not in root.attrib and "height" not in root.attrib, (
        "must scale with its container"
    )
    assert len(text.encode("utf-8")) < MAX_BYTES
    tags = {el.tag.rsplit("}", 1)[-1] for el in root.iter()}
    assert {"title", "desc"} <= tags, "needs a title and a description for assistive technology"
    for forbidden in ("script", "foreignObject", "image", "iframe", "use", "style"):
        assert forbidden not in tags, f"<{forbidden}> is not allowed in a README graphic"
    lowered = text.lower()
    assert "http://" not in lowered.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in lowered and "@import" not in lowered
    assert " href=" not in lowered and "xlink:href" not in lowered
    assert not re.search(r"\son[a-z]+\s*=", lowered), "no event handlers"


@pytest.mark.parametrize("name", GRAPHICS)
def test_graphic_is_used_by_the_readme(name):
    assert name in (ROOT / "README.md").read_text(encoding="utf-8")


# ── Counts ──────────────────────────────────────────────────────────────────────────────────


def test_provider_count_and_split_match_the_registry():
    ids = set(ProviderRegistry()._factories)
    assert ids == set(LOCAL) | set(CLOUD), "a provider was added or removed: update the graphics"
    assert _facts()["providers"] == str(len(ids))
    svg = _read("velune-numbers.svg")
    assert f"{len(LOCAL)} local" in svg and f"{len(CLOUD)} cloud" in svg
    connect = _read("velune-connect.svg")
    assert f"{len(ids)} provider adapters" in connect
    assert f"Local · {len(LOCAL)}" in connect and f"Cloud · {len(CLOUD)}" in connect
    for label in [*LOCAL.values(), *CLOUD.values()]:
        assert label in connect, f"{label} is a provider but is missing from velune-connect.svg"
    questions = _read("velune-questions.svg")
    assert f"{len(ids)} provider adapters" in questions
    assert f"{len(LOCAL)} local" in questions and f"{len(CLOUD)} cloud" in questions


def test_slash_command_counts_match_the_registry():
    registry = build_slash_registry(MagicMock())
    commands = {cmd.name: cmd for cmd in registry._commands.values()}
    visible = [cmd for cmd in commands.values() if not cmd.hidden]
    categories = {cmd.category for cmd in visible}
    assert _facts()["slash_commands"] == str(len(visible))
    assert f"in {len(categories)} categories" in _read("velune-numbers.svg")
    docs = (ROOT / "docs" / "slash-commands.md").read_text(encoding="utf-8")
    assert f"{len(visible)} commands across {len(categories)} categories" in docs


def test_memory_tier_count_matches_the_memory_command():
    registry = build_slash_registry(MagicMock())
    memory = next(cmd for cmd in registry._commands.values() if cmd.name == "memory")
    tiers = memory.description.split(":", 1)[1].split(",")
    assert _facts()["memory_tiers"] == str(len(tiers))
    assert [t.strip() for t in tiers] == ["working", "episodic", "semantic", "graph", "lineage"]


def test_council_skill_and_profile_counts_match_the_library():
    from velune.swarm.library import SkillLibrary

    library = SkillLibrary.load(user_root=Path("does-not-exist"))
    builtin_skills = [s for s in library.skills.values() if s.source == "builtin"]
    builtin_profiles = [p for p in library.profiles.values() if p.source == "builtin"]
    assert _facts()["council_skills"] == str(len(builtin_skills))
    assert f"{len(builtin_profiles)} permission profiles" in _read("velune-numbers.svg")


def test_ci_matrix_matches_the_workflow():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    m = re.search(r"fail-fast: false\s+matrix:\s+os: \[(.*?)\]\s+python-version: \[(.*?)\]", ci)
    assert m, "the test matrix in ci.yml changed shape: update this test"
    systems = [s.strip() for s in m.group(1).split(",")]
    versions = [v.strip().strip('"') for v in m.group(2).split(",")]
    assert _facts()["ci_matrix"] == f"{len(systems)} × {len(versions)}"
    assert f"{versions[0]} through {versions[-1]}" in _read("velune-numbers.svg")


def test_zero_analytics_claim_is_still_in_the_security_policy():
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "### Zero telemetry" in security
    assert "no analytics, usage reporting or remote crash reporting" in " ".join(security.split())
    assert _facts()["analytics"] == "0"


# ── Permission matrix ───────────────────────────────────────────────────────────────────────


def _documented_modes() -> dict[str, list[str]]:
    text = (ROOT / "docs" / "filesystem-tools.md").read_text(encoding="utf-8")
    section = text.split("### Execution modes", 1)[1].split("###", 1)[0]
    table: dict[str, list[str]] = {}
    for line in section.splitlines():
        cells = [x.strip() for x in line.strip().strip("|").split("|")]
        if cells and cells[0] in {"Manual", "Plan", "Auto"}:
            table[cells[0]] = [
                "refused" if "refused" in x else "asked" if "asked" in x else "allowed"
                for x in cells[1:]
            ]
    return table


def test_permission_graphic_matches_the_documented_table():
    documented = _documented_modes()
    assert set(documented) == {"Manual", "Plan", "Auto"}
    drawn: dict[str, list[str]] = {}
    for pill in _with(_tree("velune-permissions.svg"), "data-mode"):
        drawn.setdefault(pill.attrib["data-mode"], []).append(pill.attrib["data-state"])
    assert drawn == documented


def test_manual_is_the_default_mode_the_graphic_says_it_is():
    from velune.kernel.config import ExecutionConfig

    assert ExecutionConfig().mode == "manual"
    assert "default" in _read("velune-permissions.svg")
