from velune.kernel.bootstrap import SubsystemModule, RuntimeEnvironment

def _create_provider_registry(env: RuntimeEnvironment):
    from velune.providers.registry import ProviderRegistry
    return ProviderRegistry(env.config.providers)

PROVIDER_MODULES = [
    SubsystemModule(
        name="provider_registry",
        factory=_create_provider_registry,
        container_key="runtime.provider_registry",
        lifecycle_key="providers",
    )
]
