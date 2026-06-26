from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_provider_registry(env: RuntimeEnvironment):
    from velune.core.trust import is_trusted
    from velune.providers.registry import ProviderRegistry

    # Project-supplied base_url overrides are only honored once the user has
    # explicitly trusted this workspace (guards against credential exfiltration
    # from a cloned/downloaded repository's velune.toml).
    trusted = True
    try:
        if env.workspace is not None:
            trusted = is_trusted(env.workspace)
    except Exception:
        trusted = False

    return ProviderRegistry(env.config.providers, trusted=trusted)


def _create_provider_health_monitor(env: RuntimeEnvironment):
    from velune.providers.health_monitor import ProviderHealthMonitor

    registry = env.container.get("runtime.provider_registry")
    monitor = ProviderHealthMonitor(registry)
    # Don't auto-start; let the application start it when needed
    return monitor


def _create_provider_router(env: RuntimeEnvironment):
    from velune.providers.router import ProviderRouter

    registry = env.container.get("runtime.provider_registry")
    router = ProviderRouter(registry)
    # Wire health monitor to router
    monitor = env.container.get("runtime.provider_health_monitor")
    router.set_health_monitor(monitor)
    return router


PROVIDER_MODULES = [
    SubsystemModule(
        name="provider_registry",
        factory=_create_provider_registry,
        container_key="runtime.provider_registry",
        lifecycle_key="providers",
        tier=0,
    ),
    SubsystemModule(
        name="provider_health_monitor",
        factory=_create_provider_health_monitor,
        container_key="runtime.provider_health_monitor",
        lifecycle_key=None,  # Optional module
        dependencies=["runtime.provider_registry"],
        tier=0,
    ),
    SubsystemModule(
        name="provider_router",
        factory=_create_provider_router,
        container_key="runtime.provider_router",
        lifecycle_key=None,
        dependencies=["runtime.provider_registry", "runtime.provider_health_monitor"],
        tier=0,
    ),
]
