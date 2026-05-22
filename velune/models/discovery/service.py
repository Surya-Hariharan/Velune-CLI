"""Model discovery orchestration service."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from velune.models.discovery.registry import ModelRegistry
from velune.models.discovery.scanners import GGUFScanner, HuggingFaceScanner, LMStudioScanner, ModelScanner, OllamaScanner
from velune.models.discovery.schemas import DiscoverySource, ModelRecord


class ModelDiscoveryService:
    """Coordinates scanners, classification, and registry updates."""

    def __init__(
        self,
        registry: Optional[ModelRegistry] = None,
        scanners: Optional[Iterable[ModelScanner]] = None,
        workspace: Optional[Path] = None,
    ) -> None:
        self.registry = registry or ModelRegistry()
        self.workspace = workspace
        self.scanners = list(scanners or self._default_scanners())

    def _default_scanners(self) -> list[ModelScanner]:
        gguf_scanner = GGUFScanner()
        huggingface_scanner = HuggingFaceScanner()
        if self.workspace:
            gguf_scanner.search_roots = [self.workspace, *gguf_scanner.search_roots]
            huggingface_scanner.search_roots = [self.workspace, *huggingface_scanner.search_roots]
        return [
            OllamaScanner(),
            LMStudioScanner(),
            gguf_scanner,
            huggingface_scanner,
        ]

    async def discover(self, provider_id: Optional[str] = None) -> list[ModelRecord]:
        """Discover models and update the registry."""

        scanners = [scanner for scanner in self.scanners if provider_id is None or scanner.provider_id == provider_id]
        results = await asyncio.gather(*(scanner.discover() for scanner in scanners), return_exceptions=True)

        records: list[ModelRecord] = []
        for result in results:
            if isinstance(result, Exception):
                continue
            records.extend(result)

        self.registry.register_many(records)
        return records

    def summary(self) -> dict[str, object]:
        return self.registry.summary()

    def records_for_source(self, source: DiscoverySource) -> list[ModelRecord]:
        return [record for record in self.registry.list() if record.source == source]