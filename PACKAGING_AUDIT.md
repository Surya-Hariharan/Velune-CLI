# Packaging Audit

**Date**: 2026-06-21  
**Version**: Velune CLI 1.0.0  
**Build Backend**: Hatchling

---

## Build System

| Item | Status | Notes |
|------|--------|-------|
| Build backend | ✅ hatchling | Declared in `[build-system]` |
| Version source | ✅ `velune/__init__.py` | `__version__ = "1.0.0"` |
| Reproducible builds | ✅ | `reproducible = true`; honors `SOURCE_DATE_EPOCH` |
| `python -m build` | ✅ PASS | Builds `velune_cli-1.0.0.tar.gz` + `velune_cli-1.0.0-py3-none-any.whl` |
| `twine check --strict dist/*` | ✅ PASS | All metadata valid |
| Pure-python wheel (`py3-none-any`) | ✅ PASS | No compiled extensions |
| CHANGELOG.md in sdist include | ⚠️ Mismatch | File lives at `docs/CHANGELOG.md`, not root `CHANGELOG.md` |

### CHANGELOG Path Discrepancy

`pyproject.toml` `[tool.hatch.build.targets.sdist].include` lists `"CHANGELOG.md"`, but the file is
at `docs/CHANGELOG.md`. Hatchling silently omits missing files during sdist build — the sdist itself
is valid, but the changelog is not bundled. The release workflow's changelog extraction also reads
`CHANGELOG.md` at the root; that step would produce an empty body.

**Recommendation**: Either move the changelog to the project root or update both `pyproject.toml`
and the release workflow to use `docs/CHANGELOG.md`.

---

## Installation

| Scenario | Command | Status |
|----------|---------|--------|
| Editable install | `pip install -e .` | ✅ PASS |
| Editable dev install | `pip install -e ".[dev]"` | ✅ PASS |
| Wheel install (clean env) | `pip install dist/*.whl` | ✅ PASS |
| Module entrypoint | `python -m velune` | ✅ Resolves to `velune.main:app` |
| Console script | `velune --version` / `velune --help` | ✅ PASS |

---

## Entry Points

```toml
[project.scripts]
velune = "velune.main:app"
```

`velune/main.py` imports from `velune.cli.app` and exposes `app`. The `__main__.py` calls `app()`
so `python -m velune` and `velune` are equivalent.

---

## Dependencies

### Core runtime dependencies

| Package | Pinned floor | Purpose |
|---------|-------------|---------|
| pydantic | ≥2.5.0 | Data models |
| typer | ≥0.9.0 | CLI framework |
| rich | ≥13.7.0 | Terminal rendering |
| openai | ≥1.10.0 | OpenAI provider adapter |
| anthropic | ≥0.18.0 | Anthropic provider adapter |
| httpx | ≥0.26.0 | Async HTTP |
| aiosqlite | ≥0.19.0 | Async SQLite |
| qdrant-client | ≥1.7.0 | Vector store |
| lancedb | ≥0.5.0 | Lance-format memory |
| rank-bm25 | ≥0.2.2 | BM25 lexical retrieval |
| tree-sitter | ≥0.21.0 + grammars | AST parsing |
| gitpython | ≥3.1.40 | Git operations |
| networkx | ≥3.2.0 | Dependency graphs |
| psutil | ≥5.9.0 | Hardware profiling |
| mcp | ≥1.0.0 | Model Context Protocol |

### Security constraint

```toml
[tool.uv]
constraint-dependencies = ["msgpack>=1.2.1"]
```

Ensures patched msgpack for GHSA-6v7p-g79w-8964 (out-of-bounds read in transitive deps).

### Optional extras

| Extra | Additional deps | Notes |
|-------|----------------|-------|
| `gguf` | `gguf>=0.6.0` | GGUF metadata reading only |
| `llamacpp` | `llama-cpp-python>=0.2.0` | In-process GGUF inference; **excluded from `all`** due to diskcache pickle RCE advisory |
| `docker` | `docker>=7.0.0` | Docker sandbox isolation |
| `all` | gguf + docker | Deliberately excludes llamacpp |
| `dev` | pytest, ruff, pyright, mypy, bandit, twine, etc. | Development tooling |

---

## MANIFEST.in

```
include README.md LICENSE
```

The sdist include list in `pyproject.toml` supersedes MANIFEST.in for hatchling builds. No
conflict.

---

## Python Version Support

| Python | Declared | Tested |
|--------|----------|--------|
| 3.11 | ✅ | ✅ (local) |
| 3.12 | ✅ | ✅ (local) |
| 3.13 | ✅ | ✅ (local, via .venv) |

`requires-python = ">=3.11"` correctly declared in `[project]`.

---

## CI Build Verification

```
Build: python -m build
  → velune_cli-1.0.0-py3-none-any.whl  ✅
  → velune_cli-1.0.0.tar.gz            ✅

twine check --strict dist/*
  → Checking velune_cli-1.0.0-py3-none-any.whl: PASSED
  → Checking velune_cli-1.0.0.tar.gz: PASSED

Wheel name contains 'py3-none-any': ✅
```

---

## Recommendations

1. **Move CHANGELOG.md to root** or update `pyproject.toml` and `release.yml` to use `docs/CHANGELOG.md`.
2. **Verify PyPI trusted publishing** is configured at pypi.org for `velune-cli` before the first release tag.
3. **Pin minor floors** for security-critical packages (httpx, aiosqlite) once a stable CI baseline is established.
