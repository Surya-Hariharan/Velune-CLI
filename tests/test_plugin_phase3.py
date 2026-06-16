"""Tests for Phase 3 — Declarative Plugin System.

Covers:
- Manifest parsing (valid, missing name, CC-compat paths)
- Command parsing (frontmatter, render, aliases)
- Skill parsing (triggers, always, context_block)
- Plugin scanner (discovers plugins, deduplicates by name)
- PluginManager (load, enable/disable, all_commands, matching_skills)
- Hook wiring via PluginManager.wire_hooks
- MCP wiring via PluginManager.wire_mcp
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_plugin(tmp_path: Path) -> Path:
    """Create a minimal valid plugin directory."""
    root = tmp_path / "my-plugin"
    root.mkdir()
    (root / "plugin.json").write_text(
        json.dumps({
            "name": "my-plugin",
            "version": "1.2.3",
            "description": "A test plugin",
            "author": {"name": "Test Author", "email": "test@example.com"},
        }),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def plugin_with_commands(tmp_plugin: Path) -> Path:
    cmds = tmp_plugin / "commands"
    cmds.mkdir()
    (cmds / "review.md").write_text(
        textwrap.dedent("""\
            ---
            description: Review a file
            args: [file-path]
            aliases: [rv, r]
            ---

            Please review the file at $1 and suggest improvements.
        """),
        encoding="utf-8",
    )
    (cmds / "summarize.md").write_text(
        textwrap.dedent("""\
            ---
            description: Summarize $*
            ---

            Summarize the following: $*
        """),
        encoding="utf-8",
    )
    return tmp_plugin


@pytest.fixture
def plugin_with_skills(tmp_plugin: Path) -> Path:
    skills = tmp_plugin / "skills"
    skills.mkdir()
    always = skills / "always-on"
    always.mkdir()
    (always / "SKILL.md").write_text(
        textwrap.dedent("""\
            ---
            name: Always On Skill
            description: Injected every turn
            always: true
            ---

            You are a helpful expert.
        """),
        encoding="utf-8",
    )
    triggered = skills / "code-review"
    triggered.mkdir()
    (triggered / "SKILL.md").write_text(
        textwrap.dedent("""\
            ---
            name: Code Review Skill
            triggers: [code review, review code, "check this"]
            always: false
            ---

            When reviewing code:
            1. Check for bugs
            2. Suggest refactors
        """),
        encoding="utf-8",
    )
    return tmp_plugin


@pytest.fixture
def plugin_with_hooks(tmp_plugin: Path) -> Path:
    hooks_dir = tmp_plugin / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "hooks.json").write_text(
        json.dumps({
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "echo pre", "timeout": 5}
                        ],
                    }
                ]
            }
        }),
        encoding="utf-8",
    )
    return tmp_plugin


@pytest.fixture
def plugin_with_mcp(tmp_plugin: Path) -> Path:
    (tmp_plugin / ".mcp.json").write_text(
        json.dumps({
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "${VELUNE_PLUGIN_ROOT}"],
            }
        }),
        encoding="utf-8",
    )
    return tmp_plugin


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------

class TestDeclarativePluginManifest:
    def test_loads_from_flat_plugin_json(self, tmp_plugin: Path) -> None:
        from velune.plugins.declarative.manifest import DeclarativePluginManifest

        m = DeclarativePluginManifest.load(tmp_plugin)
        assert m is not None
        assert m.name == "my-plugin"
        assert m.version == "1.2.3"
        assert m.description == "A test plugin"
        assert m.author.name == "Test Author"
        assert m.author.email == "test@example.com"
        assert m.enabled is True

    def test_loads_from_claude_plugin_subdir(self, tmp_path: Path) -> None:
        from velune.plugins.declarative.manifest import DeclarativePluginManifest

        root = tmp_path / "cc-plugin"
        root.mkdir()
        subdir = root / ".claude-plugin"
        subdir.mkdir()
        (subdir / "plugin.json").write_text(
            json.dumps({"name": "cc-plugin", "version": "0.1.0"}),
            encoding="utf-8",
        )
        m = DeclarativePluginManifest.load(root)
        assert m is not None
        assert m.name == "cc-plugin"

    def test_loads_from_velune_plugin_subdir(self, tmp_path: Path) -> None:
        from velune.plugins.declarative.manifest import DeclarativePluginManifest

        root = tmp_path / "vl-plugin"
        root.mkdir()
        subdir = root / ".velune-plugin"
        subdir.mkdir()
        (subdir / "plugin.json").write_text(
            json.dumps({"name": "vl-plugin"}),
            encoding="utf-8",
        )
        m = DeclarativePluginManifest.load(root)
        assert m is not None
        assert m.name == "vl-plugin"

    def test_returns_none_when_no_manifest(self, tmp_path: Path) -> None:
        from velune.plugins.declarative.manifest import DeclarativePluginManifest

        root = tmp_path / "empty-dir"
        root.mkdir()
        assert DeclarativePluginManifest.load(root) is None

    def test_returns_none_for_missing_name(self, tmp_path: Path) -> None:
        from velune.plugins.declarative.manifest import DeclarativePluginManifest

        root = tmp_path / "nameless"
        root.mkdir()
        (root / "plugin.json").write_text(json.dumps({"version": "1.0"}), encoding="utf-8")
        assert DeclarativePluginManifest.load(root) is None

    def test_author_as_string(self, tmp_path: Path) -> None:
        from velune.plugins.declarative.manifest import DeclarativePluginManifest

        root = tmp_path / "p"
        root.mkdir()
        (root / "plugin.json").write_text(
            json.dumps({"name": "p", "author": "Jane Doe"}), encoding="utf-8"
        )
        m = DeclarativePluginManifest.load(root)
        assert m is not None
        assert m.author.name == "Jane Doe"

    def test_paths_resolve_to_root(self, tmp_plugin: Path) -> None:
        from velune.plugins.declarative.manifest import DeclarativePluginManifest

        m = DeclarativePluginManifest.load(tmp_plugin)
        assert m is not None
        assert m.commands_dir == tmp_plugin / "commands"
        assert m.skills_dir == tmp_plugin / "skills"
        assert m.hooks_file == tmp_plugin / "hooks" / "hooks.json"
        assert m.mcp_file == tmp_plugin / ".mcp.json"

    def test_disabled_plugin(self, tmp_path: Path) -> None:
        from velune.plugins.declarative.manifest import DeclarativePluginManifest

        root = tmp_path / "off"
        root.mkdir()
        (root / "plugin.json").write_text(
            json.dumps({"name": "off", "enabled": False}), encoding="utf-8"
        )
        m = DeclarativePluginManifest.load(root)
        assert m is not None
        assert m.enabled is False


# ---------------------------------------------------------------------------
# Command parser tests
# ---------------------------------------------------------------------------

class TestPluginCommand:
    def test_parse_command_file(self, plugin_with_commands: Path) -> None:
        from velune.plugins.declarative.command import parse_command_file

        cmd = parse_command_file(plugin_with_commands / "commands" / "review.md", "my-plugin")
        assert cmd is not None
        assert cmd.name == "review"
        assert cmd.description == "Review a file"
        assert cmd.aliases == ["rv", "r"]
        assert cmd.args == ["file-path"]

    def test_render_positional_args(self, plugin_with_commands: Path) -> None:
        from velune.plugins.declarative.command import parse_command_file

        cmd = parse_command_file(plugin_with_commands / "commands" / "review.md", "my-plugin")
        rendered = cmd.render("src/main.py", plugin_with_commands)
        assert "src/main.py" in rendered
        assert "$1" not in rendered

    def test_render_star_args(self, plugin_with_commands: Path) -> None:
        from velune.plugins.declarative.command import parse_command_file

        cmd = parse_command_file(plugin_with_commands / "commands" / "summarize.md", "my-plugin")
        rendered = cmd.render("this long text", plugin_with_commands)
        assert "this long text" in rendered

    def test_render_plugin_root_substitution(self, plugin_with_commands: Path) -> None:
        from velune.plugins.declarative.command import PluginCommand

        cmd = PluginCommand(
            name="test",
            plugin_name="my-plugin",
            body="Root is ${VELUNE_PLUGIN_ROOT}",
        )
        rendered = cmd.render("", plugin_with_commands)
        assert str(plugin_with_commands) in rendered

    def test_render_claude_plugin_root_substitution(self, plugin_with_commands: Path) -> None:
        from velune.plugins.declarative.command import PluginCommand

        cmd = PluginCommand(
            name="test",
            plugin_name="my-plugin",
            body="Root is ${CLAUDE_PLUGIN_ROOT}",
        )
        rendered = cmd.render("", plugin_with_commands)
        assert str(plugin_with_commands) in rendered

    def test_leftover_placeholders_removed(self) -> None:
        from velune.plugins.declarative.command import PluginCommand

        cmd = PluginCommand(
            name="test",
            plugin_name="p",
            body="$1 and $2",
        )
        rendered = cmd.render("only-one", Path("/tmp"))
        assert "$2" not in rendered
        assert "only-one" in rendered

    def test_load_plugin_commands(self, plugin_with_commands: Path) -> None:
        from velune.plugins.declarative.command import load_plugin_commands

        cmds = load_plugin_commands(plugin_with_commands / "commands", "my-plugin")
        assert len(cmds) == 2
        names = {c.name for c in cmds}
        assert "review" in names
        assert "summarize" in names

    def test_load_plugin_commands_empty_dir(self, tmp_path: Path) -> None:
        from velune.plugins.declarative.command import load_plugin_commands

        d = tmp_path / "empty"
        d.mkdir()
        assert load_plugin_commands(d, "p") == []

    def test_load_plugin_commands_nonexistent(self, tmp_path: Path) -> None:
        from velune.plugins.declarative.command import load_plugin_commands

        assert load_plugin_commands(tmp_path / "nope", "p") == []


# ---------------------------------------------------------------------------
# Skill parser tests
# ---------------------------------------------------------------------------

class TestPluginSkill:
    def test_parse_skill_file_always(self, plugin_with_skills: Path) -> None:
        from velune.plugins.declarative.skill import parse_skill_file

        skill = parse_skill_file(plugin_with_skills / "skills" / "always-on", "my-plugin")
        assert skill is not None
        assert skill.name == "Always On Skill"
        assert skill.always is True

    def test_parse_skill_file_triggered(self, plugin_with_skills: Path) -> None:
        from velune.plugins.declarative.skill import parse_skill_file

        skill = parse_skill_file(plugin_with_skills / "skills" / "code-review", "my-plugin")
        assert skill is not None
        assert skill.name == "Code Review Skill"
        assert skill.always is False
        assert "code review" in skill.triggers

    def test_matches_always(self, plugin_with_skills: Path) -> None:
        from velune.plugins.declarative.skill import parse_skill_file

        skill = parse_skill_file(plugin_with_skills / "skills" / "always-on", "my-plugin")
        assert skill.matches("completely unrelated text") is True

    def test_matches_trigger_phrase(self, plugin_with_skills: Path) -> None:
        from velune.plugins.declarative.skill import parse_skill_file

        skill = parse_skill_file(plugin_with_skills / "skills" / "code-review", "my-plugin")
        assert skill.matches("can you do a code review please") is True
        assert skill.matches("hello world") is False

    def test_context_block_format(self, plugin_with_skills: Path) -> None:
        from velune.plugins.declarative.skill import parse_skill_file

        skill = parse_skill_file(plugin_with_skills / "skills" / "code-review", "my-plugin")
        block = skill.context_block
        assert block.startswith("## Skill: Code Review Skill")
        assert "bugs" in block

    def test_load_plugin_skills(self, plugin_with_skills: Path) -> None:
        from velune.plugins.declarative.skill import load_plugin_skills

        skills = load_plugin_skills(plugin_with_skills / "skills", "my-plugin")
        assert len(skills) == 2
        names = {s.name for s in skills}
        assert "Always On Skill" in names
        assert "Code Review Skill" in names

    def test_load_plugin_skills_nonexistent(self, tmp_path: Path) -> None:
        from velune.plugins.declarative.skill import load_plugin_skills

        assert load_plugin_skills(tmp_path / "nope", "p") == []


# ---------------------------------------------------------------------------
# Scanner tests
# ---------------------------------------------------------------------------

class TestPluginScanner:
    def test_discovers_valid_plugin(self, tmp_path: Path, tmp_plugin: Path) -> None:
        from velune.plugins.declarative.scanner import PluginScanner

        # tmp_plugin is inside tmp_path
        scanner = PluginScanner([tmp_path])
        plugins = scanner.scan()
        assert len(plugins) == 1
        assert plugins[0].name == "my-plugin"

    def test_deduplicates_by_name(self, tmp_path: Path) -> None:
        """Plugin appearing in two paths → only first (higher-priority) loaded."""
        from velune.plugins.declarative.scanner import PluginScanner

        high = tmp_path / "high"
        high.mkdir()
        low = tmp_path / "low"
        low.mkdir()

        for d in (high, low):
            p = d / "duplicate"
            p.mkdir()
            (p / "plugin.json").write_text(
                json.dumps({"name": "duplicate", "version": "1.0" if d == high else "2.0"}),
                encoding="utf-8",
            )

        scanner = PluginScanner([high, low])
        plugins = scanner.scan()
        assert len(plugins) == 1
        assert plugins[0].manifest.version == "1.0"

    def test_skips_disabled_plugins(self, tmp_path: Path) -> None:
        from velune.plugins.declarative.scanner import PluginScanner

        p = tmp_path / "off"
        p.mkdir()
        (p / "plugin.json").write_text(
            json.dumps({"name": "off", "enabled": False}), encoding="utf-8"
        )
        scanner = PluginScanner([tmp_path])
        assert scanner.scan() == []

    def test_skips_directories_without_manifest(self, tmp_path: Path) -> None:
        from velune.plugins.declarative.scanner import PluginScanner

        (tmp_path / "not-a-plugin").mkdir()
        scanner = PluginScanner([tmp_path])
        assert scanner.scan() == []

    def test_default_paths_includes_home(self) -> None:
        from velune.plugins.declarative.scanner import PluginScanner

        scanner = PluginScanner.default_paths(None)
        paths_str = [str(p) for p in scanner._search_paths]
        assert any(".velune" in p and "plugins" in p for p in paths_str)

    def test_add_path(self, tmp_path: Path) -> None:
        from velune.plugins.declarative.scanner import PluginScanner

        scanner = PluginScanner([])
        scanner.add_path(tmp_path)
        assert tmp_path in scanner._search_paths


# ---------------------------------------------------------------------------
# PluginManager tests
# ---------------------------------------------------------------------------

class TestPluginManager:
    def test_load_discovers_plugins(self, tmp_path: Path, tmp_plugin: Path) -> None:
        from velune.plugins.manager import PluginManager

        mgr = PluginManager()
        mgr._scanner._search_paths = [tmp_path]
        plugins = mgr.load()
        assert len(plugins) == 1
        assert mgr.plugin_count == 1

    def test_load_is_idempotent(self, tmp_path: Path, tmp_plugin: Path) -> None:
        from velune.plugins.manager import PluginManager

        mgr = PluginManager()
        mgr._scanner._search_paths = [tmp_path]
        mgr.load()
        mgr.load()  # second call should not double-count
        assert mgr.plugin_count == 1

    def test_enable_disable(self, tmp_path: Path, tmp_plugin: Path) -> None:
        from velune.plugins.manager import PluginManager

        mgr = PluginManager()
        mgr._scanner._search_paths = [tmp_path]
        mgr.load()

        assert mgr.is_enabled("my-plugin") is True
        mgr.disable("my-plugin")
        assert mgr.is_enabled("my-plugin") is False
        mgr.enable("my-plugin")
        assert mgr.is_enabled("my-plugin") is True

    def test_disable_unknown_plugin(self) -> None:
        from velune.plugins.manager import PluginManager

        mgr = PluginManager()
        # Should return False, not raise
        assert mgr.disable("nonexistent") is False

    def test_all_commands_from_enabled_plugin(
        self, tmp_path: Path, plugin_with_commands: Path
    ) -> None:
        from velune.plugins.manager import PluginManager

        mgr = PluginManager()
        mgr._scanner._search_paths = [tmp_path]
        mgr.load()
        cmds = mgr.all_commands()
        assert len(cmds) == 2

    def test_all_commands_excludes_disabled(
        self, tmp_path: Path, plugin_with_commands: Path
    ) -> None:
        from velune.plugins.manager import PluginManager

        mgr = PluginManager()
        mgr._scanner._search_paths = [tmp_path]
        mgr.load()
        mgr.disable("my-plugin")
        assert mgr.all_commands() == []

    def test_matching_skills(
        self, tmp_path: Path, plugin_with_skills: Path
    ) -> None:
        from velune.plugins.manager import PluginManager

        mgr = PluginManager()
        mgr._scanner._search_paths = [tmp_path]
        mgr.load()
        blocks = mgr.matching_skills("can you do a code review?")
        # Both skills should match: always-on always matches, code-review matches trigger
        assert len(blocks) == 2

    def test_matching_skills_no_match(
        self, tmp_path: Path, plugin_with_skills: Path
    ) -> None:
        from velune.plugins.manager import PluginManager

        mgr = PluginManager()
        mgr._scanner._search_paths = [tmp_path]
        mgr.load()
        blocks = mgr.matching_skills("hello world")
        # Only always-on should match
        assert len(blocks) == 1
        assert "Always On Skill" in blocks[0]

    def test_reload_clears_and_reloads(
        self, tmp_path: Path, tmp_plugin: Path
    ) -> None:
        from velune.plugins.manager import PluginManager

        mgr = PluginManager()
        mgr._scanner._search_paths = [tmp_path]
        mgr.load()
        assert mgr.plugin_count == 1

        # Reload all
        mgr.reload()
        assert mgr.plugin_count == 1

    def test_status_returns_list(self, tmp_path: Path, tmp_plugin: Path) -> None:
        from velune.plugins.manager import PluginManager

        mgr = PluginManager()
        mgr._scanner._search_paths = [tmp_path]
        mgr.load()
        s = mgr.status()
        assert len(s) == 1
        assert s[0]["name"] == "my-plugin"
        assert "commands" in s[0]
        assert "skills" in s[0]


# ---------------------------------------------------------------------------
# Hook wiring tests
# ---------------------------------------------------------------------------

class TestWireHooks:
    def test_wire_hooks_injects_bindings(
        self, tmp_path: Path, plugin_with_hooks: Path
    ) -> None:
        from velune.plugins.manager import PluginManager
        from velune.hooks.dispatcher import HookDispatcher

        mgr = PluginManager()
        mgr._scanner._search_paths = [tmp_path]
        mgr.load()

        dispatcher = HookDispatcher(workspace=None)
        # Force load so the cache is a list
        before = len(dispatcher._ensure_loaded())
        count = mgr.wire_hooks(dispatcher)
        after = len(dispatcher._ensure_loaded())

        assert count == 1  # one binding wired
        assert after == before + 1

    def test_wire_hooks_skips_missing_file(
        self, tmp_path: Path, tmp_plugin: Path
    ) -> None:
        from velune.plugins.manager import PluginManager
        from velune.hooks.dispatcher import HookDispatcher

        # tmp_plugin has no hooks file
        mgr = PluginManager()
        mgr._scanner._search_paths = [tmp_path]
        mgr.load()

        dispatcher = HookDispatcher(workspace=None)
        count = mgr.wire_hooks(dispatcher)
        assert count == 0


# ---------------------------------------------------------------------------
# MCP wiring tests
# ---------------------------------------------------------------------------

class TestWireMcp:
    def test_wire_mcp_registers_server(
        self, tmp_path: Path, plugin_with_mcp: Path
    ) -> None:
        from velune.plugins.manager import PluginManager
        from velune.mcp.registry import MCPServerRegistry

        mgr = PluginManager()
        mgr._scanner._search_paths = [tmp_path]
        mgr.load()

        mcp_reg = MCPServerRegistry(workspace=tmp_path)
        count = mgr.wire_mcp(mcp_reg)

        assert count == 1
        # Server is namespaced as "my-plugin:filesystem"
        assert "my-plugin:filesystem" in mcp_reg._entries

    def test_wire_mcp_substitutes_plugin_root(
        self, tmp_path: Path, plugin_with_mcp: Path
    ) -> None:
        from velune.plugins.manager import PluginManager
        from velune.mcp.registry import MCPServerRegistry

        mgr = PluginManager()
        mgr._scanner._search_paths = [tmp_path]
        mgr.load()

        mcp_reg = MCPServerRegistry(workspace=tmp_path)
        mgr.wire_mcp(mcp_reg)

        entry = mcp_reg._entries["my-plugin:filesystem"]
        root_str = str(plugin_with_mcp)
        # ${VELUNE_PLUGIN_ROOT} in args should be substituted
        assert any(root_str in a for a in entry.config.args)

    def test_wire_mcp_skips_missing_file(
        self, tmp_path: Path, tmp_plugin: Path
    ) -> None:
        from velune.plugins.manager import PluginManager
        from velune.mcp.registry import MCPServerRegistry

        mgr = PluginManager()
        mgr._scanner._search_paths = [tmp_path]
        mgr.load()

        mcp_reg = MCPServerRegistry(workspace=tmp_path)
        count = mgr.wire_mcp(mcp_reg)
        assert count == 0


# ---------------------------------------------------------------------------
# YAML parser edge cases
# ---------------------------------------------------------------------------

class TestSimpleYamlParser:
    def test_string_scalar(self) -> None:
        from velune.plugins.declarative.command import _parse_simple_yaml

        r = _parse_simple_yaml("key: value")
        assert r["key"] == "value"

    def test_quoted_string(self) -> None:
        from velune.plugins.declarative.command import _parse_simple_yaml

        r = _parse_simple_yaml('key: "hello world"')
        assert r["key"] == "hello world"

    def test_inline_list(self) -> None:
        from velune.plugins.declarative.command import _parse_simple_yaml

        r = _parse_simple_yaml("items: [a, b, c]")
        assert r["items"] == ["a", "b", "c"]

    def test_block_list(self) -> None:
        from velune.plugins.declarative.command import _parse_simple_yaml

        text = "items:\n  - one\n  - two"
        r = _parse_simple_yaml(text)
        assert r["items"] == ["one", "two"]

    def test_booleans(self) -> None:
        from velune.plugins.declarative.command import _parse_simple_yaml

        r = _parse_simple_yaml("a: true\nb: false")
        assert r["a"] is True
        assert r["b"] is False
