from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_cognitive_firewall(env: RuntimeEnvironment):
    from velune.cognition.firewall import CognitiveFirewall
    return CognitiveFirewall()

def _create_council_orchestrator(env: RuntimeEnvironment):
    from velune.cognition.orchestrator import CouncilOrchestrator
    from velune.models.specializations import ModelSpecializationMapper

    provider_registry = env.container.get("runtime.provider_registry")
    model_registry = env.container.get("runtime.model_registry")
    model_specialization = ModelSpecializationMapper(model_registry)
    sqlite_manager = env.container.get("runtime.sqlite_manager")

    return CouncilOrchestrator(
        provider_registry,
        model_specialization,
        sqlite_manager=sqlite_manager,
        config=env.config,
    )

COGNITION_MODULES = [
    SubsystemModule(
        name="cognitive_firewall",
        factory=_create_cognitive_firewall,
        container_key="runtime.firewall",
    ),
    SubsystemModule(
        name="council_orchestrator",
        factory=_create_council_orchestrator,
        container_key="runtime.council_orchestrator",
        dependencies=["runtime.provider_registry", "runtime.model_registry", "runtime.sqlite_manager"],
    )
]
