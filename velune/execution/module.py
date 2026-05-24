from velune.kernel.bootstrap import SubsystemModule, RuntimeEnvironment

def _create_execution_executor(env: RuntimeEnvironment):
    from velune.execution.executor import ExecutionExecutor
    return ExecutionExecutor(env.workspace)

EXECUTION_MODULES = [
    SubsystemModule(
        name="execution",
        factory=_create_execution_executor,
        container_key="runtime.execution_executor",
        lifecycle_key="execution",
    )
]
