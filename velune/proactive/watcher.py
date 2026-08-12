"""ProactiveWatcher: subscribes to CognitiveBus events and runs periodic health checks."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from velune.proactive.alerts import AlertSeverity, AlertStore, make_alert

if TYPE_CHECKING:
    from velune.cli.statusbar import StatusBarState
    from velune.core.task_registry import JobRegistry
    from velune.events import CognitiveBus, Subscription

_log = logging.getLogger("velune.proactive.watcher")

# Keyless local providers with a well-known default port that a meaningful
# fraction of users actually have running — "might be there, alert if it goes
# down" is a reasonable assumption for these two, so the periodic health
# check's "unconfigured, so don't nag" skip in `_run_periodic_checks` doesn't
# apply to them.
#
# Deliberately excludes "llamacpp" and "openai-compat":
#   - "llamacpp": LlamaCppProvider.initialize() unconditionally raises
#     ProviderConnectionError — the former [llamacpp] extra was permanently
#     removed (llama-cpp-python pulls in an unpatched unsafe-deserialization
#     dependency; see adapters/llamacpp.py). Its health check can never pass
#     without the user manually installing that package out-of-band, so it
#     is UNAVAILABLE for 100% of installs, forever, by design — treating that
#     as alert-worthy produced a permanent, unconditional "Provider llamacpp
#     unavailable" warning for every user regardless of which provider they
#     actually configured or were using.
#   - "openai-compat": a generic "point this at any self-hosted OpenAI-shaped
#     server" catch-all with no conventional default port anyone is likely to
#     have listening — unlike Ollama (:11434) and LM Studio (:1234), there is
#     no real-world signal that an unreachable default here means anything
#     went wrong for this user.
_KEYLESS_PROVIDERS = frozenset({"ollama", "lmstudio"})


class ProactiveWatcher:
    """Watches bus events and periodic health signals; populates :class:`AlertStore`.

    Lifecycle: call ``await start()`` after the event bus is running, and
    ``await stop()`` before shutdown.  Both are idempotent.
    """

    PERIODIC_INTERVAL_S: float = 15.0

    def __init__(
        self,
        bus: CognitiveBus,
        alert_store: AlertStore,
        job_registry: JobRegistry,
        health_monitor: Any | None = None,
        status_state: StatusBarState | None = None,
        periodic_interval_s: float | None = None,
    ) -> None:
        self._bus = bus
        self._store = alert_store
        self._job_registry = job_registry
        self._health_monitor = health_monitor
        self._status_state = status_state
        self._subscriptions: list[Subscription] = []
        self._periodic_task: asyncio.Task | None = None
        self._running = False
        # Provider ids already alerted as unavailable by `_run_periodic_checks`,
        # so a still-down provider is reported once (not re-queued on every
        # tick) until it either recovers or its manifest disappears — see that
        # method's docstring.
        self._alerted_unavailable: set[str] = set()
        # Instance override of the class default, scaled by hardware tier at
        # construction (see kernel/entrypoint.py) — a weak machine shouldn't
        # run this at the same cadence as a workstation.
        if periodic_interval_s is not None:
            self.PERIODIC_INTERVAL_S = periodic_interval_s

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        self._subscriptions = [
            await self._bus.subscribe("job.failed", self._on_job_failed),
            await self._bus.subscribe("retry.loop_detected", self._on_loop_detected),
            await self._bus.subscribe("provider.health_changed", self._on_provider_health),
            await self._bus.subscribe("context.threshold_crossed", self._on_context_threshold),
        ]
        self._periodic_task = asyncio.create_task(
            self._periodic_loop(), name="proactive-watcher-periodic"
        )
        _log.info("ProactiveWatcher started.")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for sub in self._subscriptions:
            sub.unsubscribe()
        self._subscriptions.clear()
        if self._periodic_task is not None:
            self._periodic_task.cancel()
            try:
                await self._periodic_task
            except asyncio.CancelledError:
                pass
        _log.info("ProactiveWatcher stopped.")

    # ------------------------------------------------------------------
    # Bus event handlers
    # ------------------------------------------------------------------

    async def _on_job_failed(self, event: Any) -> None:
        job_id = event.data.get("job_id", "?")
        error = event.data.get("error", "unknown error")
        self._add(
            AlertSeverity.WARN,
            f"Background job failed: {job_id}",
            error[:120],
            "job_monitor",
        )

    async def _on_loop_detected(self, event: Any) -> None:
        error_type = event.data.get("error_type", "Error")
        preview = event.data.get("error_preview", "")
        count = event.data.get("occurrences", 0)
        self._add(
            AlertSeverity.DANGER,
            f"Error loop detected: {error_type}",
            f"Seen {count}x in 5 min — {preview[:80]}. Use /jobs cancel to clear.",
            "loop_detector",
        )

    async def _on_provider_health(self, event: Any) -> None:
        provider_id = event.data.get("provider_id", "?")
        new_health = event.data.get("health", "?")
        if new_health in ("degraded", "unavailable"):
            severity = AlertSeverity.DANGER if new_health == "unavailable" else AlertSeverity.WARN
            self._add(
                severity,
                f"Provider {provider_id} {new_health}",
                "Run /doctor to diagnose provider connectivity.",
                "health_monitor",
            )

    async def _on_context_threshold(self, event: Any) -> None:
        pct = event.data.get("pct", 0)
        threshold = event.data.get("threshold", 0)
        severity = AlertSeverity.DANGER if threshold >= 90 else AlertSeverity.WARN
        self._add(
            severity,
            f"Context window at {pct:.0f}%",
            f"Crossed {threshold}% threshold. Use /new to start a fresh session.",
            "context_tracker",
        )

    # ------------------------------------------------------------------
    # Periodic checks
    # ------------------------------------------------------------------

    async def _periodic_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.PERIODIC_INTERVAL_S)
                await self._run_periodic_checks()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _log.error("Periodic check error: %s", exc)

    async def _run_periodic_checks(self) -> None:
        """Alert once per provider that goes unavailable, not once per tick.

        `ProviderRegistry` registers a lazy factory for every provider type
        it knows about — including ones the user never added a key for — so
        `health_monitor.get_all_manifests()` reports ``UNAVAILABLE`` for
        those right alongside a genuinely broken configured provider; an
        unconfigured cloud provider failing its health check is expected,
        not actionable. And since nothing upstream currently publishes a
        `provider.health_changed` bus event (`_on_provider_health` above is
        otherwise unreachable), this was the *only* path that ever alerted
        on provider health — re-adding an alert for every still-down
        provider on every tick, forever, with no way for it to ever clear.
        On a fresh install with only one or two providers configured, that
        floods the next keypress (`poll_and_render_alerts` drains on every
        prompt submit) with a wall of "Provider X unavailable" panels for
        providers the user never intended to use.

        Fixed by only alerting for providers that are actually configured
        (a stored API key, or a keyless local endpoint like Ollama/LM
        Studio), and only on the transition into ``UNAVAILABLE`` — tracked
        in `_alerted_unavailable` — clearing once the provider recovers so a
        later, genuinely new outage still alerts again.
        """
        if self._health_monitor is None:
            return
        try:
            manifests = self._health_monitor.get_all_manifests()
        except Exception:
            return

        try:
            from velune.core.types.provider import ProviderHealth
            from velune.providers.keystore import has_key

            still_unavailable: set[str] = set()
            for pid, manifest in manifests.items():
                if manifest.health != ProviderHealth.UNAVAILABLE:
                    continue
                if pid not in _KEYLESS_PROVIDERS and not has_key(pid):
                    continue  # never configured — not the user's concern
                still_unavailable.add(pid)
                if pid in self._alerted_unavailable:
                    continue  # already reported; wait for recovery or shutdown
                self._add(
                    AlertSeverity.WARN,
                    f"Provider {pid} unavailable",
                    "Run /doctor to diagnose.",
                    "periodic_health_check",
                )
            self._alerted_unavailable = still_unavailable
        except Exception as exc:
            _log.debug("Periodic health check skipped: %s", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _add(self, severity: AlertSeverity, title: str, body: str, source: str) -> None:
        alert = make_alert(severity, title, body, source)
        self._store.add(alert)
        if self._status_state is not None:
            try:
                self._status_state.alert_count = self._store.unread_count()
            except Exception:
                pass
        _log.debug("Alert added [%s]: %s", severity, title)
