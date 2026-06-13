# CI/CD Pipeline

Velune utilizes GitHub Actions for continuous integration (CI) and continuous deployment (CD). This document describes the checks that run on every pull request and push to the `main` or `develop` branches, as well as how to run them locally.

## CI Workflow Jobs

The CI pipeline consists of several jobs defined in [.github/workflows/ci.yml](file:///c:/Users/surya/OneDrive/Desktop/Velune-CLI/.github/workflows/ci.yml):

1. **Lint**: Enforces code style and type safety.
   - **Ruff Lint**: Checks codebase style rules and imports (`ruff check velune/`).
   - **Ruff Format**: Validates standard layout formatting (`ruff format --check velune/`).
   - **Pyright**: Performs static type checking using Pyright (`pyright velune/`).

2. **Security**: Audits package dependencies and checks for security vulnerabilities.
   - **pip-audit**: Scans installed package dependencies for known vulnerabilities.
   - **Bandit**: Static analysis tool for finding common security issues in Python code (`bandit -c pyproject.toml -r velune/`).
   - **Gitleaks**: Scans commits for exposed API keys, credentials, or secrets.
   - **Anti-Regression Checks**: Scripted scans to ensure no unsafe patterns are introduced (e.g. blocking `shell=True` and enforcing a maximum of 1 `asyncio.run()` call to prevent loop nesting).

3. **Test**: Runs the test suite across multiple configurations.
   - Matrix covers:
     - **OS**: Ubuntu (Linux), Windows, macOS
     - **Python**: 3.11, 3.12, 3.13
   - **Coverage Floor**: Enforces a minimum code coverage (currently set at **20%** floor over the entire package, excluding interactive/TTY CLI and daemon modules).

4. **Build**: Verifies package reproducibility and metadata.
   - Builds source distribution (`sdist`) and wheel (`wheel`) deterministically using Hatchling with `SOURCE_DATE_EPOCH` pinned.
   - Validates package metadata using `twine check --strict`.
   - Assures that the wheel is a pure-python (`py3-none-any`) package.

5. **Install-Smoke**: Verifies package installability.
   - Performs a clean installation of the built wheel in a fresh environment on all platforms (Ubuntu, Windows, macOS) and test Python versions.
   - Executes entrypoint verification (`velune --version`, `velune --help`, `python -m velune --version`) to guarantee no import-time regressions.

---

## Local Replication Guide

To run the CI checks on your local machine before pushing code, execute these commands:

### Prerequisites
Ensure dev dependencies are installed:
```bash
pip install -e ".[dev]"
```

### 1. Code Quality & Linting
```bash
# Style check
ruff check velune/

# Type check
pyright velune/
```

### 2. Testing
```bash
# Run full suite
pytest tests/ -v

# Run with coverage report
pytest tests/ --cov=velune --cov-report=term-missing --cov-fail-under=20
```

### 3. Security Analysis
```bash
# Bandit scan (gates on medium+ severity issues)
bandit -c pyproject.toml -r velune/ --severity-level medium --confidence-level medium

# Check for shell=True usage
grep -rn "shell=True" velune/

# Check asyncio.run() count
grep -rn "asyncio\.run(" velune/
```

### 4. Build Verification
```bash
# Build
python -m build

# Validate
twine check --strict dist/*
```
