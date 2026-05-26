# Breaking Changes

## Batch 01 — Production Remediation

### `VeluneMemoryError` rename (Fix 1)

**Affected file:** `velune/core/errors/memory.py`

The following exception classes have been **renamed** to avoid shadowing Python's
built-in `MemoryError` (which signals out-of-memory conditions at the interpreter
level). Any code that wrote `except MemoryError:` to catch Velune's custom
exception was silently suppressing real OOM errors raised by CPython.

| Old name                    | New name                           |
|-----------------------------|------------------------------------|
| `MemoryError`               | `VeluneMemoryError`               |
| `MemoryStoreError`          | `VeluneMemoryStoreError`          |
| `MemoryRetrievalError`      | `VeluneMemoryRetrievalError`      |
| `MemoryConsolidationError`  | `VeluneMemoryConsolidationError`  |

#### Migration

```python
# Before
from velune.core.errors import MemoryError, MemoryStoreError

# After
from velune.core.errors import VeluneMemoryError, VeluneMemoryStoreError
```

Any `except MemoryError:` clauses that were intended to catch Velune memory
failures must be updated to `except VeluneMemoryError:`.

---

### `CapabilityLevel` alias removal (Fix 4)

**Affected file:** `velune/core/types/model.py`

The enum aliases `CAPABLE`, `STRONG`, and `EXCEPTIONAL` have been **removed**
from `CapabilityLevel`. These were value-identical duplicates of existing
members, which caused `CapabilityLevel.CAPABLE == CapabilityLevel.INTERMEDIATE`
to evaluate as `True` and made comparisons semantically ambiguous.

| Removed alias              | Use instead                  |
|----------------------------|------------------------------|
| `CapabilityLevel.CAPABLE`  | `CapabilityLevel.INTERMEDIATE` |
| `CapabilityLevel.STRONG`   | `CapabilityLevel.ADVANCED`   |
| `CapabilityLevel.EXCEPTIONAL` | `CapabilityLevel.EXPERT`  |

---

### Removed pip dependencies (Fix 2 + Fix 6)

The following packages have been **removed** from the main `dependencies` list
in `pyproject.toml`:

| Package           | Reason                                                    |
|-------------------|-----------------------------------------------------------|
| `asyncio>=3.4.3`  | Standard library module — pip stub is abandoned (2015)   |
| `langchain`       | Zero imports found in codebase                            |
| `langchain-core`  | Zero imports found in codebase                            |
| `graphiti-core`   | Zero imports; graph layer uses raw SQLite                 |
| `langgraph`       | Moved to optional group `pip install velune[langgraph]`  |

#### Migration

If your deployment requires GGUF model discovery or LangGraph orchestration,
install the relevant extras:

```bash
pip install velune[gguf]        # GGUF model file support
pip install velune[langgraph]   # LangGraph orchestration engine
```
