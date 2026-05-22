"""Model registry for discovered and classified models."""

from __future__ import annotations

from typing import Any, Optional

from velune.core.types import CapabilityLevel, ModelCapability, ModelDescriptor
from velune.models.discovery.schemas import ModelRecord, ModelSpecialization


class ModelRegistry:
    """In-memory registry for discovered models."""

    def __init__(self) -> None:
        self._records: dict[str, ModelRecord] = {}

    @staticmethod
    def _key(provider_id: str, model_id: str) -> str:
        return f"{provider_id}:{model_id}"

    def register(self, record: ModelRecord) -> None:
        self._records[self._key(record.provider_id, record.model_id)] = record

    def register_many(self, records: list[ModelRecord]) -> None:
        for record in records:
            self.register(record)

    def get(self, model_id: str, provider_id: Optional[str] = None) -> Optional[ModelRecord]:
        if provider_id:
            return self._records.get(self._key(provider_id, model_id))
        for record in self._records.values():
            if record.model_id == model_id:
                return record
        return None

    def list(self) -> list[ModelRecord]:
        return sorted(self._records.values(), key=lambda record: (record.provider_id, record.model_id))

    def list_descriptors(self) -> list[ModelDescriptor]:
        return [record.descriptor for record in self.list()]

    def find_by_specialization(self, specialization: ModelSpecialization) -> list[ModelRecord]:
        return [record for record in self.list() if record.classification.specialization == specialization]

    def find_by_capability(
        self,
        capability: ModelCapability,
        minimum_level: CapabilityLevel = CapabilityLevel.CAPABLE,
    ) -> list[ModelRecord]:
        matches: list[ModelRecord] = []
        for record in self.list():
            level = record.classification.capabilities.get(capability, CapabilityLevel.NONE)
            if level >= minimum_level:
                matches.append(record)
        return matches

    def best_for(self, capability: ModelCapability, provider_id: Optional[str] = None) -> Optional[ModelRecord]:
        records = self.find_by_capability(capability)
        if provider_id:
            records = [record for record in records if record.provider_id == provider_id]
        if not records:
            return None

        return sorted(
            records,
            key=lambda record: (
                record.classification.capabilities.get(capability, CapabilityLevel.NONE),
                record.classification.context_length,
                record.classification.reasoning_quality,
                record.classification.coding_quality,
            ),
            reverse=True,
        )[0]

    def summary(self) -> dict[str, Any]:
        return {
            "total": len(self._records),
            "providers": sorted({record.provider_id for record in self._records.values()}),
            "specializations": sorted({record.classification.specialization.value for record in self._records.values()}),
        }