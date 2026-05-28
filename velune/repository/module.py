from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_repository_cognition(env: RuntimeEnvironment):
    from velune.repository.cognition import RepositoryCognitionService
    return RepositoryCognitionService(env.workspace)

REPOSITORY_MODULES = [
    SubsystemModule(
        name="repository_cognition",
        factory=_create_repository_cognition,
        container_key="runtime.repository_cognition",
        lifecycle_key="repository",
    ),
]
