import pytest
from velune.kernel.lifecycle import LifecycleCoordinator, Subsystem
from velune.kernel.schemas import ComponentStatus

class DummySubsystem:
    def __init__(self) -> None:
        self.initialized = False
        self.shutdown_completed = False

    async def initialize(self) -> None:
        self.initialized = True

    async def shutdown(self) -> None:
        self.shutdown_completed = True

@pytest.mark.asyncio
async def test_lifecycle_coordinator():
    coord = LifecycleCoordinator()
    sub1 = DummySubsystem()
    sub2 = DummySubsystem()
    
    coord.register("sub1", sub1)
    coord.register("sub2", sub2)
    
    assert coord.get_status("sub1") == ComponentStatus.UNINITIALIZED
    assert coord.get_status("sub2") == ComponentStatus.UNINITIALIZED
    
    # Startup sequence
    await coord.startup()
    
    assert sub1.initialized
    assert sub2.initialized
    assert coord.get_status("sub1") == ComponentStatus.HEALTHY
    assert coord.get_status("sub2") == ComponentStatus.HEALTHY
    
    # Shutdown sequence
    await coord.shutdown()
    
    assert sub1.shutdown_completed
    assert sub2.shutdown_completed
    
    # The registry should be cleared upon successful shutdown
    assert coord.get_status("sub1") == ComponentStatus.UNINITIALIZED
