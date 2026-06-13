# Contributing to Velune

Thank you for helping improve Velune. This guide covers development setup,
how to add new providers, agents, and commands, and the review process.

For anything larger than a small fix, open an issue first to discuss
scope before writing code.

---

## Table of contents

- [Development setup](#development-setup)
- [Running tests and linting](#running-tests-and-linting)
- [How to add a new cloud provider](#how-to-add-a-new-cloud-provider)
- [How to add a new slash command](#how-to-add-a-new-slash-command)
- [How to add a new council agent](#how-to-add-a-new-council-agent)
- [Pull request checklist](#pull-request-checklist)
- [Commit message format](#commit-message-format)
- [Code style](#code-style)
- [Reporting bugs](#reporting-bugs)

---

## Development setup

```bash
git clone https://github.com/Surya-Hariharan/Velune-CLI.git
cd Velune-CLI

python -m venv .venv
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\activate             # Windows

pip install -e ".[dev]"
```

Verify the install:

```bash
velune --version
velune doctor
```

---

## Running tests and linting

```bash
# Fast security tests
pytest tests/security -v

# Full suite (includes security and integration tests)
pytest tests/ -v

# Lint check (must pass before merging)
ruff check velune/

# Type check (must pass before merging)
pyright velune/

# Coverage (must pass the 20% floor benchmark)
pytest tests/ --cov=velune --cov-report=term-missing --cov-fail-under=20
```

All `ruff`, `pyright`, and `pytest` checks must pass before a PR will be merged.

---

## How to add a new cloud provider

This example adds a fictional **Cohere** provider.
Follow the same pattern for any new cloud API.

### Step 1 — Create the adapter

`velune/providers/adapters/cohere.py`

Implement the `ModelProvider` protocol. If the API is OpenAI-compatible,
inherit from `OpenAIProvider` and override only `provider_id` and the
base URL:

```python
from velune.providers.adapters.openai import OpenAIProvider

class CohereProvider(OpenAIProvider):
    provider_id = "cohere"

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(
            api_key=api_key,
            base_url="https://api.cohere.com/compatibility/v1",
        )
```

If the API is not OpenAI-compatible, inherit from `ModelProvider` directly
and implement `infer()`, `stream()`, `list_models()`, and `health_check()`.

### Step 2 — Create the discovery module

`velune/providers/discovery/cohere.py`

The discovery module returns a hardcoded list of `ModelDescriptor` objects
so the model registry can surface them without a live API call:

```python
from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.keystore import has_key

COHERE_MODELS: list[ModelDescriptor] = [
    ModelDescriptor(
        model_id="command-r-plus",
        provider_id="cohere",
        display_name="Command R+",
        context_length=128000,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.ADVANCED,
            reasoning=CapabilityLevel.ADVANCED,
        ),
        is_local=False,
        speed_tier="medium",
        cost_per_1k_tokens=0.003,
    ),
]


class CohereDiscovery:
    def discover(self) -> list[ModelDescriptor]:
        if not has_key("cohere"):
            return []
        return COHERE_MODELS
```

### Step 3 — Register in the provider registry

`velune/providers/registry.py` → `_register_default_providers()`

```python
self.register_factory(
    "cohere",
    self._keyed_factory(
        "velune.providers.adapters.cohere", "CohereProvider", "cohere"
    ),
)
```

### Step 4 — Add to the setup wizard

`velune/cli/commands/setup.py` → `PROVIDER_METADATA` dict

```python
"cohere": {
    "label": "Cohere (cloud — Command R+, paid)",
    "requires_key": True,
    "free": False,
    "key_label": "Cohere API key",
    "get_key_url": "https://dashboard.cohere.com/api-keys",
},
```

### Step 5 — Add the environment variable fallback

`velune/providers/keystore.py` → `_ENV_VARS` dict

```python
_ENV_VARS: dict[str, str] = {
    ...
    "cohere": "COHERE_API_KEY",
}
```

### Step 6 — Add cost data

`velune/telemetry/token_tracker.py` → `PROVIDER_COSTS` dict

```python
PROVIDER_COSTS: dict[str, dict[str, float]] = {
    ...
    "cohere": {
        "command-r-plus": 0.003,
        "command-r":      0.0005,
    },
}
```

### Step 7 — Write tests

`tests/test_providers.py`

```python
def test_cohere_discovery_skips_without_key(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "")
    from velune.providers.discovery.cohere import CohereDiscovery
    assert CohereDiscovery().discover() == []

def test_cohere_discovery_returns_models(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "test-key")
    from velune.providers.discovery.cohere import CohereDiscovery
    models = CohereDiscovery().discover()
    assert any(m.model_id == "command-r-plus" for m in models)

def test_cohere_cost_table_entry():
    from velune.telemetry.token_tracker import PROVIDER_COSTS
    assert "cohere" in PROVIDER_COSTS
    assert "command-r-plus" in PROVIDER_COSTS["cohere"]
```

### Step 8 — Update the README

Add `Cohere` to the Providers table in [README.md](README.md).

---

## How to add a new slash command

### Step 1 — Register in the command registry

`velune/cli/repl.py` → `VeluneREPL._build_registry()`

```python
registry.register(SlashCommand(
    name="yourcommand",
    aliases=["yc"],
    description="One-line description of what it does",
    usage="/yourcommand [optional-arg]",
    handler=self._cmd_yourcommand,
))
```

### Step 2 — Implement the handler

Add the async method to `VeluneREPL` (anywhere in the "Command handlers"
section of the class):

```python
async def _cmd_yourcommand(self, args: str) -> None:
    # args is everything after "/yourcommand " — may be empty
    self.console.print(f"[cyan]You typed:[/cyan] {args!r}")
```

Handlers must be `async`. Use `self.console.print()` for all output
(never `print()`). Access the container for services:

```python
model_registry = self.container.get("runtime.model_registry")
```

### Step 3 — Add to autocomplete

`velune/cli/autocomplete.py` → `SLASH_COMMANDS` list

```python
SLASH_COMMANDS: list[tuple[str, str]] = [
    ...
    ("yourcommand", "One-line description of what it does"),
]
```

### Step 4 — Write a test

`tests/test_repl.py`

```python
@pytest.mark.asyncio
async def test_cmd_yourcommand(mock_runtime):
    repl = VeluneREPL(mock_runtime)
    await repl._cmd_yourcommand("some-arg")
    mock_runtime.console.print.assert_called()
```

---

## How to add a new council agent

### Step 1 — Create the agent

`velune/cognition/council/your_agent.py`

Inherit from `BaseCouncilAgent` and implement the required interface.
Use an existing agent (e.g., `reviewer.py`) as a template:

```python
from velune.cognition.council.base import BaseCouncilAgent
from velune.cognition.council.messages import CouncilMessage

class YourAgent(BaseCouncilAgent):
    role_id = "your_agent"

    @property
    def system_prompt(self) -> str:
        return (
            "You are a specialized agent that ..."
        )

    async def process(self, message: CouncilMessage) -> CouncilMessage:
        response = await self._infer(message.content)
        return message.with_response(self.role_id, response)
```

### Step 2 — Register in the agent factory

`velune/cognition/council/factory.py`

Add the agent to the `_build_agents()` method so the factory can
instantiate it at runtime:

```python
from velune.cognition.council.your_agent import YourAgent

def _build_agents(self, ...) -> list[BaseCouncilAgent]:
    return [
        ...
        YourAgent(provider=self._provider, model_id=self._model_id),
    ]
```

### Step 3 — Choose which tiers include the agent

`velune/cognition/council/tiers.py`

The `CouncilTier` enum and `classify_task_tier()` function control
which agents run per tier:

```python
class CouncilTier(str, Enum):
    INSTANT  = "instant"   # Coder only
    MINIMAL  = "minimal"   # Planner + Coder
    STANDARD = "standard"  # Coder + Reviewer
    FULL     = "full"      # All agents
```

Add `YourAgent` to the agent list for the tiers where it should fire.
Prefer `FULL` for expensive agents and `STANDARD` or higher for
lightweight ones.

### Step 4 — Write tests

`tests/test_council.py` (or create it if it does not exist):

```python
@pytest.mark.asyncio
async def test_your_agent_processes_message(mock_provider):
    from velune.cognition.council.your_agent import YourAgent
    from velune.cognition.council.messages import CouncilMessage

    agent = YourAgent(provider=mock_provider, model_id="test-model")
    msg = CouncilMessage(content="refactor auth module")
    result = await agent.process(msg)
    assert result.responses.get("your_agent") is not None
```

---

## Pull request checklist

Before requesting review:

- [ ] All existing tests pass: `pytest tests/ -q`
- [ ] New code has tests — unit tests for pure logic, integration
      tests for provider/agent wiring
- [ ] `ruff check velune/` shows zero issues
- [ ] `velune doctor` passes on a clean install (check CI)
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]`
- [ ] README updated if a user-facing feature was added
- [ ] No secrets, `.env` files, or API keys committed
- [ ] Branch targets `main` and is rebased to latest `main`

---

## Commit message format

Use [Conventional Commits](https://www.conventionalcommits.org/):

```text
feat: add Cohere provider adapter
fix: handle Ollama connection timeout gracefully
docs: update MCP integration guide
test: add tests for token tracker edge cases
refactor: extract model selector into separate module
chore: bump httpx to 0.27
```

Subject line: imperative tense, ≤ 72 characters, no trailing period.

For breaking changes add `!` after the type: `feat!: rename /run to /exec`

---

## Code style

- **Python 3.11+** — use `X | Y` not `Optional[X]`, `list[str]` not
  `List[str]`
- **async/await throughout** — no blocking I/O inside `async` functions;
  use `asyncio.to_thread()` for CPU-bound work if needed
- **All user-facing output** goes through `self.console.print()` (Rich);
  never `print()`
- **No `shell=True`** in subprocess calls — always pass a list of args
- **API keys** always via `velune.providers.keystore.get_key()` — never
  `os.getenv()` directly in provider code
- **File writes** inside the workspace go through the diff preview
  system — never `path.write_text()` directly in council agents
- **No comments** explaining what the code does — use clear names.
  Comment only when explaining a non-obvious invariant or workaround.

---

## Reporting bugs

Open a GitHub issue with:

```text
velune --version
velune doctor
```

Include the exact command that failed, the full error message (run
with `--verbose` for stack traces), and your OS and Python version.

Report security vulnerabilities via
[GitHub Security Advisories](https://github.com/Surya-Hariharan/Velune-CLI/security/advisories/new),
not public issues.

---

## Review expectations

- Keep changes focused — one concern per PR.
- Explain the *why* in the PR description; the code explains the *what*.
- Address review comments within a few days; stale PRs may be closed.
- Reviewers run `pytest` locally before approving non-trivial changes.

---

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
Violations may be reported to the maintainer.

---

Apache License 2.0 — Copyright 2026 Surya HA
