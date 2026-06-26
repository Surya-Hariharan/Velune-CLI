from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_model_registry(env: RuntimeEnvironment):
    from velune.models.registry import ModelCapabilityRegistry

    registry = ModelCapabilityRegistry()
    # Register the scanner property for backward compatibility
    env.container.register_instance("runtime.model_discovery", registry.scanner)
    return registry


MODEL_MODULES = [
    SubsystemModule(
        name="model_registry",
        factory=_create_model_registry,
        container_key="runtime.model_registry",
        lifecycle_key="models",
        tier=0,
    )
]
