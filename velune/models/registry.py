"""Model capability registry with empirical probe evaluation."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.models.local_models import (
    Availability,
    FailureKind,
    LocalModelRegistry,
    LocalModelStoreError,
    RecheckThrottle,
    check_registration,
    descriptor_from_record,
    path_accessible,
    record_from_descriptor,
)
from velune.models.profile_cache import ModelProfileCache
from velune.providers.discovery.scanner import ModelDiscoveryScanner, ScanReport

logger = logging.getLogger("velune.models.registry")

# A previous model is dropped by a refresh, kept unchanged, or kept but marked
# unavailable with a (kind, reason).
_DROP = None
_KEEP = ("keep", None)


def _coerce_capabilities(model: ModelDescriptor) -> None:
    """Some curated catalogs build ``capabilities`` as a plain dict.

    ``_apply_probe_results`` assigns attributes on it, which raised
    AttributeError mid-refresh — after the old code had already cleared the
    catalog, leaving the user with no models at all.
    """
    caps = model.capabilities
    if isinstance(caps, ModelCapabilityProfile):
        return
    if isinstance(caps, dict):
        known = {
            k: CapabilityLevel(int(v))
            for k, v in caps.items()
            if k in ModelCapabilityProfile.model_fields and isinstance(v, int | float)
        }
        model.capabilities = ModelCapabilityProfile(**known)
    else:
        model.capabilities = ModelCapabilityProfile()


def is_available(model: ModelDescriptor) -> bool:
    """False only for a model known to be unusable right now (see refresh)."""
    return (model.metadata or {}).get("availability") != Availability.UNAVAILABLE


def unavailable_reason(model: ModelDescriptor) -> str | None:
    meta = model.metadata or {}
    if meta.get("availability") != Availability.UNAVAILABLE:
        return None
    return meta.get("unavailable_reason") or meta.get("servable_reason") or "Model is unavailable."


def _mark_unavailable(model: ModelDescriptor, kind: str, reason: str) -> ModelDescriptor:
    stale = model.model_copy(deep=True)
    stale.health = "offline"
    stale.metadata["availability"] = Availability.UNAVAILABLE
    stale.metadata["unavailable_reason"] = reason
    stale.metadata["unavailable_kind"] = kind
    stale.metadata["stale"] = True
    return stale


class ModelCapabilityRegistry:
    """Unified database cataloging discovered models and capabilities with empirical routing.

    Refreshes are non-destructive: the catalog is replaced only after a scan
    finishes, and a provider whose listing failed (offline, timeout, daemon
    stopped, drive unplugged) keeps its last known models instead of losing
    them. Registered local models (:mod:`velune.models.local_models`) are
    always listed, marked unavailable with a reason when they cannot be used.
    """

    def __init__(
        self,
        scanner: ModelDiscoveryScanner | None = None,
        *,
        local_models: LocalModelRegistry | None = None,
    ) -> None:
        self.scanner = scanner or ModelDiscoveryScanner()
        self._models: dict[str, ModelDescriptor] = {}
        self._local = local_models or LocalModelRegistry()
        self._throttle = RecheckThrottle()
        #: provider id -> why its last listing failed (for /model and diagnostics)
        self.last_scan_failures: dict[str, str] = {}
        # Pre-populate from disk cache so `models list` works without a scan
        try:
            from velune.models.registry_cache import ModelRegistryCache

            self._registry_cache = ModelRegistryCache()
            for model in self._registry_cache.load():
                self._index(self._models, model)
        except Exception:
            self._registry_cache = None
        try:
            models = {(m.provider_id, m.model_id): m for m in self.list_all()}
            self._overlay_registrations(models, ScanReport(), write=False)
            self._replace(models.values())
        except Exception as exc:
            logger.debug("Could not overlay local model registrations: %s", exc)

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    @staticmethod
    def _index(table: dict[str, ModelDescriptor], model: ModelDescriptor) -> None:
        table[f"{model.provider_id}/{model.model_id}"] = model
        if model.model_id not in table:
            table[model.model_id] = model

    def _replace(self, models) -> None:
        """Swap the whole catalog in one assignment (never leaves it half-built)."""
        table: dict[str, ModelDescriptor] = {}
        for model in models:
            self._index(table, model)
        self._models = table

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    async def _scan(self) -> ScanReport:
        detailed = getattr(self.scanner, "scan_all_detailed", None)
        if detailed is not None:
            return await detailed()
        # A scanner without per-provider status (test doubles, plugins): every
        # provider it returned is taken as refreshed.
        models = await self.scanner.scan_all()
        return ScanReport(models=list(models), refreshed={m.provider_id for m in models})

    async def refresh(self) -> None:
        """Scan all providers and refresh the local catalog cache with empirical profiles."""
        try:
            report = await self._scan()
        except Exception as e:
            # Keep the catalog exactly as it was: a failed scan is not an empty one.
            logger.error("Failed to discover models during catalog refresh: %s", e)
            return

        self.last_scan_failures = dict(report.failed)
        merged = self._merge(self.list_all(), report)
        try:
            self._overlay_registrations(merged, report, write=True)
        except Exception as exc:
            logger.debug("Could not reconcile local model registrations: %s", exc)

        fresh = [m for m in report.models if (m.provider_id, m.model_id) in merged]
        for model in merged.values():
            _coerce_capabilities(model)
        try:
            await self._probe(fresh)
        except Exception as exc:  # probing is best-effort; never lose the catalog over it
            logger.debug("Model probing failed: %s", exc)

        self._replace(merged.values())
        logger.info(
            "Indexed %d models (%d validated)",
            len(merged),
            sum(1 for m in merged.values() if m.metadata.get("validated", True)),
        )
        # Persist to disk cache for next session
        try:
            if self._registry_cache is not None:
                self._registry_cache.save(self.list_all())
        except Exception as cache_exc:
            logger.debug("Registry cache save failed: %s", cache_exc)

    def _merge(
        self, previous: list[ModelDescriptor], report: ScanReport
    ) -> dict[tuple[str, str], ModelDescriptor]:
        """Combine a scan with the previous catalog without losing what we couldn't check."""
        prev_by_provider: dict[str, int] = {}
        for old in previous:
            prev_by_provider[old.provider_id] = prev_by_provider.get(old.provider_id, 0) + 1

        merged: dict[tuple[str, str], ModelDescriptor] = {}
        for model in report.models:
            meta = model.metadata
            if (
                meta.get("catalog_source") == "fallback"
                and model.provider_id in report.failed
                and prev_by_provider.get(model.provider_id)
            ):
                # The listing failed and we already know this provider's real
                # models: keep those (below) instead of the curated fallback.
                continue
            if meta.get("servable") is False:
                meta["availability"] = Availability.UNAVAILABLE
                meta.setdefault("unavailable_reason", meta.get("servable_reason"))
                meta.setdefault("unavailable_kind", FailureKind.NOT_SERVED)
            else:
                meta.setdefault("availability", Availability.AVAILABLE)
            merged[(model.provider_id, model.model_id)] = model

        for old in previous:
            key = (old.provider_id, old.model_id)
            if key in merged or old.metadata.get("registration_only"):
                continue
            verdict = self._keep_verdict(old, report)
            if verdict is _DROP:
                continue
            if verdict == _KEEP:
                merged[key] = old
                continue
            kind, reason = verdict
            if old.is_local:
                merged[key] = _mark_unavailable(old, kind, reason)
            else:
                # A failed cloud listing says nothing about whether the model
                # works — keep it usable, just note the list may be out of date.
                kept = old.model_copy(deep=True)
                kept.metadata["listing_stale"] = reason
                merged[key] = kept
        return merged

    @staticmethod
    def _keep_verdict(old: ModelDescriptor, report: ScanReport):
        pid = old.provider_id
        root = old.metadata.get("root")
        if old.is_local and root and not path_accessible(root):
            return (FailureKind.STORAGE_UNAVAILABLE, f"Model storage path is unavailable: {root}")
        if pid in report.failed:
            why = report.failed[pid]
            lowered = why.lower()
            if "timed out" in lowered:
                kind = FailureKind.TIMEOUT
            elif "not reachable" in lowered or "not running" in lowered or "responding" in lowered:
                kind = FailureKind.ENDPOINT_UNREACHABLE
            else:
                kind = FailureKind.UNKNOWN
            return (kind, why)
        if pid in report.refreshed:
            return _DROP  # the provider listed its models and this one is gone
        if pid in report.skipped and not old.is_local:
            from velune.providers.keystore import KeyState, verification_state

            if verification_state(pid) is KeyState.INVALID:
                return (FailureKind.UNKNOWN, f"{pid} rejected its stored credential")
            return _DROP  # the key was removed
        return _KEEP  # not covered by this scan at all

    # ------------------------------------------------------------------
    # Local model registrations
    # ------------------------------------------------------------------

    def _overlay_registrations(
        self,
        models: dict[tuple[str, str], ModelDescriptor],
        report: ScanReport,
        *,
        write: bool,
    ) -> None:
        """Make every registered local model visible, with its current availability."""
        for record in self._local.load():
            key = record.key
            model = models.get(key)
            if model is not None:
                model.metadata["registered"] = True
                if model.metadata.get("root") is None and record.source_path:
                    model.metadata["root"] = record.source_path
                if not write:
                    continue
                if is_available(model):
                    if record.status != Availability.AVAILABLE:
                        self._safe_update(record, available=True, model=model)
                else:
                    kind = model.metadata.get("unavailable_kind") or FailureKind.UNKNOWN
                    reason = unavailable_reason(model) or ""
                    if record.last_error != reason:
                        self._safe_update(record, available=False, kind=kind, reason=reason)
                continue

            if write:
                kind, reason = self._classify_missing(record, report)
                if kind is not None and record.last_error != reason:
                    self._safe_update(record, available=False, kind=kind, reason=reason)
                    record.status = Availability.UNAVAILABLE
                    record.last_error, record.last_error_kind = reason, str(kind)
            descriptor = descriptor_from_record(record)
            descriptor.metadata["registration_only"] = True
            models[key] = descriptor

    @staticmethod
    def _classify_missing(record, report: ScanReport):
        if record.source_path and not path_accessible(record.source_path):
            return FailureKind.STORAGE_UNAVAILABLE, (
                f"Model storage path is unavailable: {record.source_path}"
            )
        if record.provider_id in report.failed:
            why = report.failed[record.provider_id]
            kind = (
                FailureKind.TIMEOUT
                if "timed out" in why.lower()
                else FailureKind.ENDPOINT_UNREACHABLE
            )
            return kind, why
        if record.provider_id in report.refreshed:
            return FailureKind.MODEL_MISSING, (
                f"{record.provider_id} is running but does not list {record.model_id}."
            )
        return None, None

    def _safe_update(self, record, *, available: bool, model=None, kind=None, reason=None) -> None:
        try:
            if available:
                fresh = record_from_descriptor(model, origin=record.origin)
                self._local.update_status(
                    record.provider_id,
                    record.model_id,
                    available=True,
                    metadata=fresh.metadata,
                    endpoint=fresh.endpoint,
                    source_path=fresh.source_path,
                    source_kind=fresh.source_kind,
                )
            else:
                self._local.update_status(
                    record.provider_id, record.model_id, available=False, kind=kind, reason=reason
                )
        except (LocalModelStoreError, OSError) as exc:
            logger.debug("Could not update local model registration: %s", exc)

    def remember_local(self, model: ModelDescriptor, *, origin: str = "active") -> bool:
        """Persist a registration for local *model* (e.g. when it is activated).

        Returns False if it could not be written; the model still works for
        this session.
        """
        if not model.is_local:
            return False
        try:
            existing = self._local.get(model.provider_id, model.model_id)
            record = record_from_descriptor(model, origin=origin)
            if existing is not None:
                # Keep the saved configuration (path, endpoint) when the current
                # descriptor is a stale or registration-only one lacking them.
                record.source_path = record.source_path or existing.source_path
                record.source_kind = record.source_kind or existing.source_kind
                record.endpoint = record.endpoint or existing.endpoint
                record.metadata = {**existing.metadata, **record.metadata}
            self._local.upsert(record)
            model.metadata["registered"] = True
            return True
        except (LocalModelStoreError, OSError) as exc:
            logger.warning("Could not save local model registration: %s", exc)
            return False

    def registration(self, provider_id: str, model_id: str):
        return self._local.get(provider_id, model_id)

    @property
    def local_models(self) -> LocalModelRegistry:
        return self._local

    async def recheck(self, model: ModelDescriptor, *, force: bool = False) -> ModelDescriptor:
        """Re-test a local model's availability now (bounded: see RecheckThrottle).

        Returns the descriptor now in the catalog. Restores a model to usable
        when its drive/server is back; never deletes anything.
        """
        if not model.is_local:
            return model
        key = (model.provider_id, model.model_id)
        if not self._throttle.allow(key, force=force):
            return self.get(model.model_id, model.provider_id) or model

        record = self._local.get(*key) or record_from_descriptor(model)
        result = await check_registration(record)
        updated = model.model_copy(deep=True)
        if result.available:
            updated.health = "unknown"
            updated.metadata["availability"] = Availability.AVAILABLE
            for stale_key in (
                "unavailable_reason",
                "unavailable_kind",
                "stale",
                "registration_only",
            ):
                updated.metadata.pop(stale_key, None)
            if result.endpoint:
                updated.location = result.endpoint
        else:
            updated = _mark_unavailable(updated, str(result.kind), result.reason)

        if self._local.get(*key) is not None:
            try:
                self._local.update_status(
                    *key,
                    available=result.available,
                    kind=result.kind,
                    reason=result.reason,
                    endpoint=result.endpoint,
                )
            except (LocalModelStoreError, OSError) as exc:
                logger.debug("Could not record recheck result: %s", exc)

        models = {(m.provider_id, m.model_id): m for m in self.list_all()}
        models[key] = updated
        self._replace(models.values())
        return updated

    # ------------------------------------------------------------------
    # Probing (unchanged behavior, isolated per model)
    # ------------------------------------------------------------------

    async def _probe(self, discovered: list[ModelDescriptor]) -> None:
        profile_cache = ModelProfileCache(Path(".velune") / "model_profiles.json")
        from velune.models.probes import FastProbe

        fast_probe = FastProbe()

        probing_tasks = []
        models_to_probe = []

        for model in discovered:
            if not is_available(model):
                continue
            try:
                cached = profile_cache.get(model.model_id, model.provider_id)
                if cached:
                    # Apply cached probe results to capability profile
                    self._apply_probe_results(model, cached["probes"])
                    continue
            except Exception as exc:
                logger.debug("Cached probe results unusable for %s: %s", model.model_id, exc)
                continue

            # Check if provider is available to probe
            provider = None
            try:
                from velune.kernel.registry import get_container

                provider_reg = get_container().get("runtime.provider_registry")
                provider = provider_reg.get(model.provider_id)
            except Exception:
                pass

            if provider:
                models_to_probe.append(model)
                probing_tasks.append(fast_probe.ping(provider, model.model_id))

        # Execute fast probes concurrently
        if probing_tasks:
            results = await asyncio.gather(*probing_tasks, return_exceptions=True)
            for model, responsive in zip(models_to_probe, results, strict=False):
                if isinstance(responsive, Exception):
                    responsive = False

                if responsive:
                    model.metadata["validated"] = True
                    try:
                        from velune.daemon.client import DaemonClient

                        if DaemonClient.is_running():
                            # Delegate background probing to the active persistent Velune daemon!
                            # Fire-and-forget the IPC dispatch, but track it so the
                            # detached task isn't garbage-collected before it sends.
                            from velune.core.task_registry import track

                            track(
                                asyncio.create_task(
                                    DaemonClient.send_command(
                                        "probe_model",
                                        model_id=model.model_id,
                                        provider_id=model.provider_id,
                                    ),
                                    name=f"daemon_probe_dispatch_{model.model_id}",
                                )
                            )
                            logger.info(
                                "Delegated full probing of model %s to the active Velune daemon process.",
                                model.model_id,
                            )
                        else:
                            from velune.kernel.registry import get_container

                            task_reg = get_container().get("runtime.task_registry")
                            task_reg.submit(
                                name=f"full_probe_{model.model_id}",
                                coro=self._probe_model_background(model, profile_cache),
                                timeout_seconds=120.0,
                            )
                    except Exception:
                        pass
                else:
                    model.metadata["validated"] = False
                    logger.info("Model %s is not responding, skipping probe", model.model_id)

    # ------------------------------------------------------------------
    # Catalog API
    # ------------------------------------------------------------------

    def register(self, descriptor: ModelDescriptor) -> None:
        """Explicitly register a custom model descriptor.

        In-memory only. Callers registering a batch that should outlive the
        process (``/connect``'s post-verification discovery, for example) must
        follow up with :meth:`persist` — see the note there.
        """
        self._index(self._models, descriptor)

    def persist(self) -> bool:
        """Write the current catalog to the on-disk cache. True if it was saved.

        Previously only :meth:`refresh` ever saved, so models discovered
        through :meth:`register` — which is the path ``/connect`` takes right
        after verifying a new API key — lived in memory and died with the
        process. The next launch loaded a cache that had never heard of the
        provider, showed no models for it, and read to the user as the
        connection having been lost.
        """
        if self._registry_cache is None:
            return False
        try:
            self._registry_cache.save(self.list_all())
            return True
        except Exception as exc:
            logger.debug("Registry cache save failed: %s", exc)
            return False

    def remove(self, model_id: str, provider_id: str | None = None) -> bool:
        """Remove a model from the catalog and any local registration for it.

        Returns True if anything was removed. This is the explicit,
        user-requested path; nothing else deletes a registration.
        """
        removed = False
        targets = [
            m
            for m in self.list_all()
            if m.model_id == model_id and (provider_id is None or m.provider_id == provider_id)
        ]
        for model in targets:
            try:
                removed |= self._local.remove(model.provider_id, model.model_id)
            except (LocalModelStoreError, OSError) as exc:
                logger.warning("Could not remove local model registration: %s", exc)
        if provider_id:
            removed |= self._models.pop(f"{provider_id}/{model_id}", None) is not None
        # Drop the bare-id alias and any provider-qualified keys for this id.
        for key in [
            k
            for k, m in list(self._models.items())
            if m.model_id == model_id and (provider_id is None or m.provider_id == provider_id)
        ]:
            if self._models.pop(key, None) is not None:
                removed = True
        return removed

    def get(self, model_id: str, provider_id: str | None = None) -> ModelDescriptor | None:
        """Look up a model descriptor by ID and optional provider prefix."""
        if provider_id:
            from velune.providers.catalog import canonical_provider_id

            key = f"{canonical_provider_id(provider_id)}/{model_id}"
            return self._models.get(key)

        # Try direct match
        if model_id in self._models:
            return self._models[model_id]

        # Try searching values
        for model in self._models.values():
            if model.model_id == model_id:
                return model
        return None

    def list_all(self) -> list[ModelDescriptor]:
        """List all currently indexed model descriptors."""
        # Return unique descriptors
        seen = set()
        unique = []
        for model in self._models.values():
            ref = (model.provider_id, model.model_id)
            if ref not in seen:
                seen.add(ref)
                unique.append(model)
        return unique

    def get_by_provider(self, provider_id: str) -> list[ModelDescriptor]:
        """List all models registered under a specific provider."""
        return [model for model in self.list_all() if model.provider_id == provider_id]

    def _apply_probe_results(self, model: ModelDescriptor, probes: dict) -> None:
        """Map float probe scores (0.0-1.0) to CapabilityLevel and update model descriptor.

        Score mapping (empirical calibration):
        - score > 0.85 → EXPERT (100)
        - score > 0.70 → ADVANCED (75)
        - score > 0.50 → INTERMEDIATE (50)
        - else → BASIC (25)
        """
        if not model.capabilities:
            model.capabilities = ModelCapabilityProfile()
        _coerce_capabilities(model)

        def score_to_level(score: float) -> CapabilityLevel:
            if score > 0.85:
                return CapabilityLevel.EXPERT
            elif score > 0.70:
                return CapabilityLevel.ADVANCED
            elif score > 0.50:
                return CapabilityLevel.INTERMEDIATE
            elif score > 0.0:
                return CapabilityLevel.BASIC
            return CapabilityLevel.NONE

        coding_data = probes.get("coding", {})
        coding_score = (
            coding_data.score if hasattr(coding_data, "score") else coding_data.get("score", 0.0)
        )

        reasoning_data = probes.get("reasoning", {})
        reasoning_score = (
            reasoning_data.score
            if hasattr(reasoning_data, "score")
            else reasoning_data.get("score", 0.0)
        )

        instruction_data = probes.get("instruction", {})
        instruction_score = (
            instruction_data.score
            if hasattr(instruction_data, "score")
            else instruction_data.get("score", 0.0)
        )

        model.capabilities.coding = score_to_level(coding_score)
        model.capabilities.reasoning = score_to_level(reasoning_score)
        model.capabilities.instruction_following = score_to_level(instruction_score)

        # Infer other capabilities from primary scores
        if model.capabilities.reasoning >= CapabilityLevel.INTERMEDIATE:
            model.capabilities.planning = CapabilityLevel.INTERMEDIATE
        if model.capabilities.instruction_following >= CapabilityLevel.INTERMEDIATE:
            model.capabilities.tool_use = CapabilityLevel.INTERMEDIATE

        logger.debug(
            "Applied probe results to %s: coding=%s (%.2f), reasoning=%s (%.2f), instruction=%s (%.2f)",
            model.model_id,
            model.capabilities.coding.name,
            coding_score,
            model.capabilities.reasoning.name,
            reasoning_score,
            model.capabilities.instruction_following.name,
            instruction_score,
        )

    async def _probe_model_background(
        self, model: ModelDescriptor, cache: ModelProfileCache
    ) -> None:
        """Run probes in background, update model in registry when done."""
        try:
            from velune.kernel.registry import get_container

            container = get_container()
            if not container.has("runtime.provider_registry"):
                logger.debug(
                    "No provider registry registered yet, skipping background probe for %s.",
                    model.model_id,
                )
                return

            provider_registry = container.get("runtime.provider_registry")
            provider = provider_registry.get(model.provider_id)
            if not provider:
                logger.debug("No active provider found for %s, skipping probe.", model.model_id)
                return

            from velune.models.probes import ModelProber

            prober = ModelProber(provider, model.model_id)
            results = await prober.run_all_probes()
            cache.set(model.model_id, model.provider_id, results)
            self._apply_probe_results(model, results)
            logger.info(
                "Successfully probed %s: coding=%.2f reasoning=%.2f",
                model.model_id,
                results["coding"].score,
                results["reasoning"].score,
            )
        except Exception as e:
            logger.debug("Background probe failed for %s: %s", model.model_id, e)
