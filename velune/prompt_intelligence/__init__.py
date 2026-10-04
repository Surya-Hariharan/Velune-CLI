"""Prompt Intelligence: compiles provider-optimized prompts from the same
abstract task intent, context, and tool set — see the Prompt Intelligence design notes.

Nothing in Velune's live REPL or council call paths imports this package
yet; wiring it in is a separate, flag-gated migration (design doc §18).
"""

from __future__ import annotations

from velune.prompt_intelligence.compiler import PromptCompiler, compile_prompt, register_renderer
from velune.prompt_intelligence.intent import TaskIntent, TaskKind
from velune.prompt_intelligence.profiles import get_provider_profile, register_provider_profile

__all__ = [
    "PromptCompiler",
    "compile_prompt",
    "register_renderer",
    "TaskIntent",
    "TaskKind",
    "get_provider_profile",
    "register_provider_profile",
]
