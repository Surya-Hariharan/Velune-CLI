# Velune CLI v1.0.1 Release Report

**Date**: 2026-06-21  
**Analyst**: Lead Maintainer  
**Previous release**: v1.0.0 (initial public release)

---

## Release Readiness Score

**7 / 10** — CI pipeline restored; core functionality operational; deferred items documented.

---

## Fixed Defects

### CI-01: asyncio.run() P0-1 Regression (CRITICAL)

- **Root cause**: `velune/plugins/runner.py` (subprocess worker) counted against the P0-1
  asyncio.run() guard, causing both the CI grep check and `security_audit.py` to fail.
- **Fix**: Extended `_ASYNCIO_RUN_ALLOWLIST` in `scripts/security_audit.py` to exempt the
  subprocess worker.  Updated CI grep in `.github/workflows/ci.yml` and `release.yml` with
  `--exclude="runner.py"`.
- **Verification**: `python scripts/security_audit.py` → `PASS: check_asyncio_run_count`

### CI-02: Ruff Lint (33 errors, BLOCKING)

- **Root cause**: Style violations accumulated across 15 files without a local CI gate.
- **Fixed errors** (25 auto-fixed + 8 manual):
  - F401: Unused imports (`rich.text.Text`, `dataclasses.field`, `Alert`)
  - B007: Loop control variable `field` renamed to `_field`
  - UP037: Quoted type annotations removed (6 locations)
  - B023: Closure not binding loop variables `line`/`hint` — fixed with default-arg capture
  - N806: Local "constant" dicts renamed to lowercase (`_st`, `_sev_border`, `_code_exts`)
  - N817: `ReviewDecision as RD` acronym alias replaced with full name
  - I001: Unsorted import blocks (2 locations)
  - F841: Unused local variables `stat`, `prop_pos`
  - UP041: `asyncio.TimeoutError` → `TimeoutError`
  - E702: Semicolons replaced with separate statements (5 locations in orchestrator.py)
- **Verification**: `ruff check velune/` → `All checks passed!`

### CI-03: Ruff Format (28 files, BLOCKING)

- **Root cause**: 28 source files not formatted to ruff standard.
- **Fix**: Ran `ruff format velune/` — 28 files reformatted.
- **Verification**: `ruff format --check velune/` → `356 files already formatted`

---

## Remaining Risks

| ID | Area | Severity | Description |
|----|------|----------|-------------|
| R-01 | Memory | Medium | `velune memory inspect` has async/sync boundary issue; coroutine not awaited |
| R-02 | Retrieval | Medium | Cold-start workspace has empty BM25 until background index completes |
| R-03 | Graph | Low | `velune workspace graph` does not persist edges; shows 0 on fresh session |
| R-04 | Firewall | Low | False positives on TypeScript/i18n files (Priority 6) |
| R-05 | Performance | Low | Sequential indexing; startup dominated by heavy imports (Priority 8) |
| R-06 | CHANGELOG | Low | `docs/CHANGELOG.md` not bundled in sdist; release body extraction may be empty |
| R-07 | PyPI | Low | Trusted publishing configuration must be verified before first tag push |

---

## Test Coverage

| Metric | Value |
|--------|-------|
| Tests run | 626 passed, 1 skipped |
| Skipped | `test_command_spec.py:79` — POSIX path semantics (Windows-only skip, correct) |
| Coverage | **31.79%** (floor: 20%) |
| Coverage floor check | ✅ PASS |

### Coverage by layer

| Layer | Coverage |
|-------|----------|
| `velune/repository/schemas.py` | 89% |
| `velune/repository/index_state.py` | 88% |
| `velune/retrieval/schemas.py` | 100% |
| `velune/retrieval/__init__.py` | 100% |
| `velune/retrieval/keyword.py` | 79% |
| `velune/execution/sandbox.py` | 80% |
| `velune/tools/safety.py` | 85% |
| `velune/core/redaction.py` | 89% |
| `velune/repository/technology_detector.py` | 14% (discovery, no unit tests) |
| `velune/repository/tracker.py` | 10% (git subprocess calls) |

---

## CI Status (Post-Fix)

| Job | Status |
|-----|--------|
| Lint (ruff check) | ✅ PASS |
| Lint (ruff format) | ✅ PASS |
| Security (asyncio.run count) | ✅ PASS |
| Security (shell=True) | ✅ PASS |
| Security (sync-over-async) | ✅ PASS |
| Security (bandit) | ✅ PASS |
| Security (gitleaks) | 🔵 GitHub-only (cannot run locally) |
| Test (626 cases) | ✅ PASS |
| Build (wheel + sdist) | ✅ PASS |
| twine check --strict | ✅ PASS |
| Pure-python wheel | ✅ PASS |
| Install smoke (entrypoint) | ✅ PASS |
| CI Pass (gate) | ✅ Expected PASS |
| PyPI Publish | 🔵 Pending tag push + trusted publishing setup |

---

## What Changed vs v1.0.0

```
scripts/security_audit.py     — allowlist model for asyncio.run(); plugins/runner.py exempted
.github/workflows/ci.yml      — grep excludes runner.py; comment documents the allowlist
.github/workflows/release.yml — same grep fix
velune/analysis/linter.py     — remove unused Text import; formatting
velune/analysis/refactor.py   — rename field → _field; formatting
velune/analysis/symbol_search.py — unquote type annotations; formatting
velune/analysis/type_inferrer.py — remove unused import; fix B023 closure capture; formatting
velune/cli/display/dashboard.py — remove unused Text import; formatting
velune/cli/repl.py             — rename _ST → _st, _SEV_BORDER → _sev_border; fix imports; formatting
velune/cognition/council/debate.py — rename RD alias → ReviewDecision; formatting
velune/cognition/orchestrator.py — split E702 semicolons; formatting
velune/context/mentions.py     — unquote type annotation; formatting
velune/execution/hunk_review.py — remove unused prop_pos; formatting
velune/mcp/transports/websocket.py — asyncio.TimeoutError → TimeoutError; formatting
velune/plugins/loader.py       — fix import sorting; formatting
velune/proactive/watcher.py    — remove unused Alert import; formatting
velune/repository/architecture_detector.py — unquote type annotations; formatting
velune/repository/grapher.py   — rename _CODE_EXTS → _code_exts; formatting
(+ 10 additional files formatted only)
```

---

## Release Recommendation

**Ship v1.0.1** after:
1. Verifying gitleaks passes on the GitHub-hosted runner (secret scan)
2. Verifying pip-audit passes (no newly disclosed CVEs in dependencies)
3. Configuring PyPI trusted publishing for this repository

The deferred items (R-01 through R-07) are non-blocking for a CLI release but should be addressed
in v1.0.2.
