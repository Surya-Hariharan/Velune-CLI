import pytest
from typing import Protocol
from velune.kernel.registry import ComponentRegistry, ServiceContainer, inject, get_container

class SimpleInterface(Protocol):
    def run(self) -> str:
        ...

class ConcreteA:
    def run(self) -> str:
        return "A"

class ConcreteB:
    def run(self) -> str:
        return "B"

def test_component_registry():
    registry = ComponentRegistry()
    impl_a = ConcreteA()
    
    registry.register(SimpleInterface, impl_a)
    assert registry.get(SimpleInterface) is impl_a
    
    # Hot swap
    impl_b = ConcreteB()
    registry.swap(SimpleInterface, impl_b)
    assert registry.get(SimpleInterface) is impl_b

def test_service_container():
    container = ServiceContainer()
    
    # Direct instance registration
    container.register_instance("my_service", "value_1")
    assert container.has("my_service")
    assert container.get("my_service") == "value_1"
    
    # Factory registration
    counter = 0
    def factory():
        nonlocal counter
        counter += 1
        return f"instance_{counter}"
        
    container.register("lazy_singleton", factory, singleton=True)
    assert container.get("lazy_singleton") == "instance_1"
    assert container.get("lazy_singleton") == "instance_1"  # Cached
    
    # Lazy transient factory
    container.register("transient", factory, singleton=False)
    assert container.get("transient") == "instance_2"
    assert container.get("transient") == "instance_3"  # New instance each time
    
    # Hot swap direct instance
    container.hot_swap("my_service", "value_2")
    assert container.get("my_service") == "value_2"
    
    # Hot swap factory
    container.hot_swap("lazy_singleton", "swapped_singleton")
    assert container.get("lazy_singleton") == "swapped_singleton"

def test_inject_decorator():
    # Setup global container
    container = get_container()
    container.clear()
    container.register_instance("database", "postgres_conn")
    
    @inject("database")
    def fetch_data(query: str, database: str = None):
        return f"Ran {query} on {database}"
        
    res = fetch_data("SELECT 1")
    assert res == "Ran SELECT 1 on postgres_conn"
    
    # Check that manual parameter override works
    res_override = fetch_data("SELECT 2", database="sqlite_conn")
    assert res_override == "Ran SELECT 2 on sqlite_conn"
