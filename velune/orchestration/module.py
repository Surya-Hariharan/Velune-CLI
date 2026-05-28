from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_orchestration_engine(env: RuntimeEnvironment):
    return env.container.get("runtime.council_orchestrator")

ORCHESTRATION_MODULES = [
    SubsystemModule(
        name="orchestration_engine",
        factory=_create_orchestration_engine,
        container_key="runtime.orchestration_engine",
        dependencies=["runtime.council_orchestrator"],
    )
]
