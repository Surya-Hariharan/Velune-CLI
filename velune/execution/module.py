from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_execution_executor(env: RuntimeEnvironment):
    from velune.execution.executor import ExecutionExecutor

    bus = env.container.get("runtime.bus") if env.container.has("runtime.bus") else None
    return ExecutionExecutor(env.workspace, config=env.config, bus=bus)


EXECUTION_MODULES = [
    SubsystemModule(
        name="execution",
        factory=_create_execution_executor,
        container_key="runtime.execution_executor",
        lifecycle_key="execution",
    )
]
