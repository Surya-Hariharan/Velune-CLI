from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Any

from velune.cognition.personality import RepositoryPersonalityAgent

if TYPE_CHECKING:
    from velune.memory.tiers.lineage import LineageMemoryTier


class StyleResolver:
    """Manages asynchronous scanning and caching of style profiles to avoid event loop blockages."""

    def __init__(self, lineage_memory: LineageMemoryTier | None) -> None:
        self.lineage_memory = lineage_memory

    async def get_or_refresh_style_profile(self, target_file: str) -> dict[str, Any] | None:
        """Queries the style profile asynchronously, scanning and caching it if missing/stale."""
        if self.lineage_memory is None:
            return None

        target_dir = os.path.dirname(target_file)
        if not target_dir:
            target_dir = "velune/core"

        profile = await self.lineage_memory.get_personality_style(target_dir)

        is_stale = False
        if profile:
            updated_at = profile.get("updated_at", 0.0)
            if time.time() - updated_at > 86400.0:
                is_stale = True

        if not profile or is_stale:
            try:
                # Wrap the blocking AST Directory analysis in an asyncio thread pool
                profile = await asyncio.to_thread(self._scan_directory, target_dir)
                if profile:
                    await self.lineage_memory.save_personality_style(
                        subsystem=target_dir,
                        naming_conventions=profile["naming_conventions"],
                        type_hinting_strictness=profile["type_hinting_strictness"],
                        preferred_constructs=profile["preferred_constructs"],
                        class_vs_functional=profile["class_vs_functional"],
                        docstring_style=profile["docstring_style"],
                    )
            except Exception as e:
                import logging

                logging.getLogger("velune.cognition.style_resolver").error(
                    "Failed to run RepositoryPersonalityAgent in background: %s", e
                )

        return profile

    def _scan_directory(self, target_dir: str) -> dict[str, Any] | None:
        """Synchronous scanning code executed in a background thread."""
        if os.path.exists(target_dir):
            agent = RepositoryPersonalityAgent()
            return agent.analyze_directory_style(target_dir)
        return None
