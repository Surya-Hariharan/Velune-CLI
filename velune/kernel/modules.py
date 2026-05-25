from velune.cognition.module import COGNITION_MODULES
from velune.execution.module import EXECUTION_MODULES
from velune.kernel.module import KERNEL_MODULES
from velune.memory.module import MEMORY_MODULES
from velune.models.module import MODEL_MODULES
from velune.orchestration.module import ORCHESTRATION_MODULES
from velune.providers.module import PROVIDER_MODULES
from velune.repository.module import REPOSITORY_MODULES
from velune.retrieval.module import RETRIEVAL_MODULES
from velune.tools.module import TOOL_MODULES

ALL_MODULES = (
    KERNEL_MODULES +
    PROVIDER_MODULES +
    MODEL_MODULES +
    REPOSITORY_MODULES +
    MEMORY_MODULES +
    RETRIEVAL_MODULES +
    EXECUTION_MODULES +
    TOOL_MODULES +
    COGNITION_MODULES +
    ORCHESTRATION_MODULES
)
