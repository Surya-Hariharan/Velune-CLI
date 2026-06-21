# GitHub Release Preview — v0.9.1

## Tag
v0.9.1

## Release Title
Velune CLI v0.9.1 — Stabilization & Trust Recovery

## Release Type
- [x] Stable release

## Assets
| File | Type |
|------|------|
| velune_cli-0.9.1-py3-none-any.whl | Pure-Python wheel |
| velune_cli-0.9.1.tar.gz | Source distribution |

Both artifacts pass twine check --strict.

---

## Release Notes Body

**Installation:**

    pip install velune-cli==0.9.1

This is a stabilization and trust-recovery release. No new user-facing features,
no breaking changes. pip install --upgrade velune-cli is a safe, drop-in update.

### Security
- Windows PATH-hijack guard enforced (was unconditionally True on Windows)
- Interpreter inline-code execution blocked (python -c, node -e, etc.)
- msgpack 1.2.0 -> 1.2.1 (GHSA-6v7p-g79w-8964)
- llama-cpp-python removed -- pip-audit reports no known vulnerabilities
- Bandit + gitleaks CI gates added

### Fixed
- Subprocess pipe-buffer deadlock in execution sandbox (64 KiB pipe limit)
- Memory coroutine never-awaited bugs
- Cold-start BM25 empty retrieval index
- Pyright type error in Cohere adapter
- CI lint/format regressions across 15+ files

### Added
- velune doctor runtime path-safety check
- velune context and velune trace observability commands
- /dashboard and /jobs REPL commands
- New provider adapters (Cohere + reorganized cloud providers)

### Changed
- CI: Ubuntu/Windows/macOS x Python 3.11/3.13
- Release pipeline: OIDC trusted publishing, reproducible builds, tag-version assertion

Full Changelog: https://github.com/Surya-Hariharan/Velune-CLI/compare/v0.9.0...v0.9.1

---

## Workflow Trigger

Pushing tag v0.9.1 triggers .github/workflows/release.yml which:
1. Runs full CI (lint, pyright, security, bandit, pip-audit)
2. Asserts tag == velune.__version__
3. Builds reproducible sdist + wheel
4. Validates with twine check --strict
5. Publishes to PyPI via OIDC trusted publishing
6. Creates GitHub Release with changelog excerpt and dist assets

---

## Rollback Plan

If release fails:

    # Delete tags
    git tag -d v0.9.1
    git push origin :refs/tags/v0.9.1

    # Delete GitHub Release
    gh release delete v0.9.1 --yes

    # If PyPI publish needs yanking
    twine yank velune-cli 0.9.1
