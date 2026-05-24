import pytest
from pathlib import Path
from velune.kernel.bootstrap import RuntimeEnvironment, RuntimeBootstrapper, SubsystemModule
from velune.kernel.config import get_default_config
from velune.kernel.registry import ServiceContainer
from velune.kernel.lifecycle import LifecycleCoordinator
from velune.memory.module import MEMORY_MODULES

def test_topological_sort_ordering():
    bootstrapper = RuntimeBootstrapper()
    
    # Register in wrong order
    # Module B depends on A
    # Module C depends on B and A
    # Module A has no dependencies
    module_c = SubsystemModule(
        name="C",
        factory=lambda env: "C",
        container_key="runtime.c",
        dependencies=["runtime.b", "runtime.a"]
    )
    module_b = SubsystemModule(
        name="B",
        factory=lambda env: "B",
        container_key="runtime.b",
        dependencies=["runtime.a"]
    )
    module_a = SubsystemModule(
        name="A",
        factory=lambda env: "A",
        container_key="runtime.a",
    )
    
    bootstrapper.register_module(module_c)
    bootstrapper.register_module(module_b)
    bootstrapper.register_module(module_a)
    
    resolved = bootstrapper._topological_sort()
    
    # Must be resolved in order A -> B -> C
    assert resolved[0].name == "A"
    assert resolved[1].name == "B"
    assert resolved[2].name == "C"

def test_circular_dependency_error():
    bootstrapper = RuntimeBootstrapper()
    
    # A depends on B, B depends on A
    module_a = SubsystemModule(
        name="A",
        factory=lambda env: "A",
        container_key="runtime.a",
        dependencies=["runtime.b"]
    )
    module_b = SubsystemModule(
        name="B",
        factory=lambda env: "B",
        container_key="runtime.b",
        dependencies=["runtime.a"]
    )
    
    bootstrapper.register_module(module_a)
    bootstrapper.register_module(module_b)
    
    with pytest.raises(ValueError, match="Circular dependency or missing module dependency"):
        bootstrapper._topological_sort()

def test_partial_memory_bootstrap(tmp_path):
    container = ServiceContainer()
    lifecycle = LifecycleCoordinator()
    config = get_default_config()
    
    env = RuntimeEnvironment(
        workspace=tmp_path,
        config=config,
        container=container,
        lifecycle=lifecycle,
    )
    
    bootstrapper = RuntimeBootstrapper()
    for mod in MEMORY_MODULES:
        bootstrapper.register_module(mod)
        
    bootstrapper.bootstrap(env)
    
    # Verify working memory is registered and works
    working_mem = container.get("runtime.working_memory")
    assert working_mem is not None
    
    # Verify episodic memory works and is registered
    episodic_mem = container.get("runtime.episodic_memory")
    assert episodic_mem is not None

def test_mock_subsystem_declarative_extension(tmp_path):
    container = ServiceContainer()
    lifecycle = LifecycleCoordinator()
    config = get_default_config()
    
    env = RuntimeEnvironment(
        workspace=tmp_path,
        config=config,
        container=container,
        lifecycle=lifecycle,
    )
    
    # Define a completely custom mock module and check that we don't edit build_runtime()
    mock_module = SubsystemModule(
        name="mock_service",
        factory=lambda env: "hello_from_mock",
        container_key="runtime.mock_service",
    )
    
    bootstrapper = RuntimeBootstrapper()
    bootstrapper.register_module(mock_module)
    bootstrapper.bootstrap(env)
    
    assert container.get("runtime.mock_service") == "hello_from_mock"
