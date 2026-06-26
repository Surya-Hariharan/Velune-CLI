# Development Guide

## Setup

### Prerequisites
- Python 3.11+
- Git
- pip or uv

### Local Development Environment

```bash
# Clone repository
git clone https://github.com/Surya-Hariharan/Velune-CLI.git
cd Velune-CLI

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Setup pre-commit hooks (auto-lint before commit)
pre-commit install

# Verify setup
ruff --version    # Should print version
pyright --version # Should print version
pytest --version  # Should print version
```

## Code Quality

### Linting

**Run locally before commit:**
```bash
# Check code style
ruff check velune/ tests/

# Auto-fix formatting
ruff format velune/ tests/

# Type checking
pyright velune/

# Architecture validation
python scripts/check_architecture.py
```

**CI will block merge if:**
- Code has style violations
- Type errors found
- Coverage < 70%
- Any test fails

### Type Hints

All code must have type hints:

```python
# ✓ Good
async def infer(self, request: InferenceRequest) -> InferenceResponse:
    """Perform inference."""
    ...

def calculate_score(values: list[float]) -> float:
    """Calculate average score."""
    return sum(values) / len(values)

# ✗ Bad
async def infer(self, request):  # Missing types
    ...

def calculate_score(values):  # Missing return type
    ...
```

Run pyright to find missing types:
```bash
pyright velune/
```

### Testing

#### Unit Tests
```bash
# Run all unit tests
pytest tests/unit/ -v

# Run specific test
pytest tests/unit/test_health_monitor.py::test_manifest_recording -v

# Run with coverage
pytest tests/unit/ --cov=velune --cov-fail-under=70

# Generate HTML coverage report
pytest tests/unit/ --cov=velune --cov-report=html
# Open htmlcov/index.html to see coverage
```

#### Integration Tests
```bash
# Run integration tests (slower)
pytest tests/integration/ -v

# Run specific integration test
pytest tests/integration/test_mcp_server.py -v
```

#### Test Requirements
- All tests must pass
- Must have ≥ 70% code coverage
- Individual unit tests must complete in < 60 seconds
- Individual integration tests must complete in < 300 seconds

## Git Workflow

### Branch Naming
```
feature/description        # New feature
fix/description           # Bug fix
refactor/description      # Code refactoring
docs/description          # Documentation only
chore/description         # Dependencies, tooling
```

### Commit Messages
Follow conventional commits:

```
type: short description

Longer explanation if needed. Explain WHY, not WHAT.

Fixes #123  # Link issue if applicable
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`

Examples:
```
feat: add provider health monitoring

Adds real-time health tracking with CapabilityManifest
and background polling every 30 seconds.

Fixes #456
```

```
fix: handle asyncio timeout in health check

Previous code didn't timeout gracefully when provider
was slow to respond. Add 2-second timeout.
```

### Pull Request Process

1. **Create feature branch**
   ```bash
   git checkout -b feature/my-feature
   ```

2. **Make changes and commit**
   ```bash
   # Make changes, test locally
   pytest tests/unit/ --cov=velune --cov-fail-under=70
   ruff check velune/ tests/
   pyright velune/
   python scripts/check_architecture.py
   
   git add .
   git commit -m "feat: add my feature"
   ```

3. **Push and create PR**
   ```bash
   git push origin feature/my-feature
   # Visit GitHub and create PR
   ```

4. **Wait for CI to pass**
   - GitHub Actions will run all checks
   - All jobs must pass (green checkmarks)
   - Address any failures

5. **Request review**
   - Assign reviewer
   - Wait for approval
   - Address feedback

6. **Merge**
   - Squash merge (one commit per feature)
   - Delete branch after merge

## Architecture Rules

The codebase enforces layer boundaries via `scripts/check_architecture.py`:

**Valid dependencies:**
- CLI can import from any lower layer ✓
- Cognition can import from Memory, Providers, Kernel ✓
- Providers can import from Kernel ✓
- Kernel is lowest infrastructure layer ✓

**Invalid dependencies:**
- Kernel imports from CLI ✗
- Providers imports from Cognition ✗
- Memory imports from CLI ✗

```bash
# Check before committing
python scripts/check_architecture.py
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for full layer diagram.

## Adding a Provider

### Step 1: Create Adapter

Create `velune/providers/adapters/your_provider.py`:

```python
from velune.providers.base import ModelProvider
from velune.core.types.provider import ProviderHealth, ProviderCapabilities
from velune.core.types.model import ModelDescriptor

class YourProvider(ModelProvider):
    """Your provider implementation."""
    
    def __init__(self, api_key: str):
        self._api_key = api_key
    
    @property
    def provider_id(self) -> str:
        return "your_provider"
    
    async def list_models(self) -> list[ModelDescriptor]:
        """List available models."""
        return [
            ModelDescriptor(
                model_id="model-1",
                display_name="Model 1",
                provider_id=self.provider_id,
                context_length=4096,
                is_local=False,
            )
        ]
    
    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Perform inference."""
        # Implementation
        pass
    
    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        """Perform streaming inference."""
        # Implementation
        pass
    
    async def health_check(self) -> ProviderHealth:
        """Check provider health."""
        try:
            # Quick API call to verify connectivity
            return ProviderHealth.HEALTHY
        except Exception:
            return ProviderHealth.UNAVAILABLE
    
    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=True,
        )
```

### Step 2: Register Provider

In `velune/providers/registry.py`, add to `_register_default_providers()`:

```python
self.register_factory(
    "your_provider",
    self._keyed_factory(
        "velune.providers.adapters.your_provider",
        "YourProvider",
        "your_provider",  # keystore key
    ),
)
```

### Step 3: Add Tests

Create `tests/unit/test_your_provider.py`:

```python
import pytest
from velune.providers.adapters.your_provider import YourProvider

@pytest.fixture
def provider():
    return YourProvider(api_key="test-key")

@pytest.mark.asyncio
async def test_list_models(provider):
    models = await provider.list_models()
    assert len(models) > 0
    assert models[0].provider_id == "your_provider"

@pytest.mark.asyncio
async def test_health_check(provider):
    health = await provider.health_check()
    assert health != ProviderHealth.UNKNOWN
```

### Step 4: Update Documentation

Add to provider table in `README.md`:

```markdown
| Your Provider    | Cloud | Cost           | Model | Setup |
|------------------|-------|----------------|-------|-------|
| Your Provider    | Cloud | Pay-per-token  | Model | `velune setup` → enter key |
```

## Adding a Memory Tier

See `velune/memory/tiers/` for examples.

## Adding a CLI Command

Create `velune/cli/commands/your_command.py`:

```python
import typer

def your_command(
    arg: str = typer.Argument(..., help="Argument help")
) -> None:
    """Command description."""
    # Implementation
    pass
```

Register in `velune/cli/app.py`:

```python
from velune.cli.commands.your_command import your_command

app.command()(your_command)
```

## Performance

### Startup Time
Target: < 3 seconds

Check with:
```bash
time velune doctor check
```

### Latency Tracking
All provider calls automatically track latency:

```python
# Your provider adapter
def _record_latency_to_monitor(self, latency_ms: float) -> None:
    from velune.kernel.registry import get_container
    container = get_container()
    if container.has("runtime.provider_health_monitor"):
        monitor = container.get("runtime.provider_health_monitor")
        monitor.record_latency(self.provider_id, int(latency_ms))
```

## Debugging

### Enable Debug Logging
```bash
velune --verbose
```

### Print Debug Info
```python
import logging
logger = logging.getLogger(__name__)
logger.debug("Debug message: %s", value)
```

### Run with Profiler
```bash
python -m cProfile -s cumtime -m velune doctor check
```

## CI/CD Pipeline

All checks run automatically on push/PR:

- **lint** (30s) - ruff + pyright
- **security** (60s) - pip-audit, regression checks
- **architecture** (10s) - layer boundaries
- **test-unit** (60s) - pytest with 70% coverage
- **build-check** (20s) - python -m build
- **test-integration** (5m, PRs/main only)
- **startup-perf** (30s, main only)

See [CI_CD_SETUP.md](../CI_CD_SETUP.md) for details.

## Release Process

### Create a Release

1. **Update version** in `velune/__init__.py`:
   ```python
   __version__ = "1.2.3"
   ```

2. **Update CHANGELOG.md**:
   ```markdown
   ## [1.2.3] - 2026-06-13
   
   ### Added
   - Feature X
   
   ### Fixed
   - Bug Y
   ```

3. **Commit and tag**:
   ```bash
   git commit -am "release: v1.2.3"
   git tag v1.2.3
   git push && git push --tags
   ```

4. **CI automatically**:
   - Runs full pipeline
   - Builds distributions
   - Publishes to PyPI
   - Creates GitHub Release

## Resources

- [ARCHITECTURE.md](ARCHITECTURE.md) - Codebase organization
- [CONTRIBUTING.md](../CONTRIBUTING.md) - Contribution guidelines
- [CI_CD_SETUP.md](../CI_CD_SETUP.md) - Testing and deployment
- [pyproject.toml](../pyproject.toml) - Tool configurations
