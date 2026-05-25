from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_cognitive_bus(env: RuntimeEnvironment):
    from velune.kernel.bus import CognitiveBus
    return CognitiveBus()

KERNEL_MODULES = [
    SubsystemModule(
        name="cognitive_bus",
        factory=_create_cognitive_bus,
        container_key="runtime.bus",
        lifecycle_key="bus",
    )
]
