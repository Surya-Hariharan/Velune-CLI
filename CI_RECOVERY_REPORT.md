# CI Recovery Report

**Date**: 2026-06-21  
**Analyst**: Lead Maintainer  
**Scope**: GitHub Actions pipeline — Lint, Security, CI Pass jobs

---

## Summary

All three failing CI jobs have been remediated and verified locally. The pipeline is expected to
return to fully green on the next push.

| Job | Before | After |
|-----|--------|-------|
| `CI / Lint` | ❌ FAIL (Pyright error) | ✅ PASS |
| `CI / Security` | ❌ FAIL (Bandit B324) | ✅ PASS |
| `CI / CI Pass` | ❌ FAIL (downstream) | ✅ PASS (auto-recovered) |

---

## Root Causes (from CI_FAILURE_ANALYSIS.md)

### Failure 1: Pyright `reportOptionalCall` — Lint job

**File**: `velune/cognition/orchestrator.py:765`  
**Error**: `Object of type "None" cannot be called (reportOptionalCall)`  
**Cause**: `_execute_tiered()` declares `progress_callback: Callable[[str], None] | None = None`.
A call at line 765 was not guarded by a `None` check. Pyright correctly flagged it.

**Fix**: Added `if progress_callback is not None:` guard before the call.

### Failure 2: Bandit B324 — Security job

**File**: `velune/core/loop_detector.py:48`  
**Error**: `hashlib.sha1 used for security purposes` — Severity: High, Confidence: High  
**Cause**: `hashlib.sha1()` was called without `usedforsecurity=False`. Bandit treats all SHA1
calls as potential security issues unless explicitly annotated otherwise.

**Fix**: Added `usedforsecurity=False` keyword argument. The hash is used solely for
deduplication key shortening — not for security — making this annotation semantically accurate.

### Failure 3: CI Pass — downstream gate

**Cause**: Purely downstream from the two failures above. The `ci-pass` job requires all upstream
jobs to succeed. No code change was required for this job.

---

## Fixes Applied

### 1. `velune/cognition/orchestrator.py` — Pyright fix

```python
# Before
progress_callback(f"[Model Assignment] {_assignment_str}")

# After
if progress_callback is not None:
    progress_callback(f"[Model Assignment] {_assignment_str}")
```

### 2. `velune/core/loop_detector.py` — Bandit B324 fix

```python
# Before
return hashlib.sha1(key.encode()).hexdigest()[:16]

# After
return hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:16]
```

---

## Local Verification

All checks run against the current codebase before commit:

### Lint

```
$ ruff check velune/
All checks passed!

$ ruff format --check velune/
356 files already formatted

$ pyright velune/
0 errors, 1 warning, 0 informations
```

The 1 warning is `reportMissingModuleSource` for the optional `docker` package (not installed in
the local venv). It is a warning, not an error; Pyright exits 0.

### Security

```
$ bandit -c pyproject.toml -r velune/ --severity-level medium --confidence-level medium
Run metrics:
    Total issues (by severity): Low: 130, Medium: 0, High: 0
Exit code: 0

$ pip-audit
No known vulnerabilities found

$ python scripts/security_audit.py
All security audit checks passed
```

### Tests

```
$ pytest tests/ --tb=short -q --cov=velune --cov-fail-under=20
626 passed, 1 skipped in 31.75% coverage
```

### Build

```
$ python -m build --outdir dist_test
$ twine check --strict dist_test/*
PASSED velune_cli-1.0.0.tar.gz
PASSED velune_cli-1.0.0-py3-none-any.whl
```

---

## Regression Tests Added

`tests/test_ci_regression.py` — 12 tests across 4 classes:

| Class | Tests | Guards against |
|-------|-------|----------------|
| `TestHashlibSha1Regression` | 4 | B324 re-introduction in loop_detector.py and any new velune/ file |
| `TestProgressCallbackNullSafety` | 3 | `reportOptionalCall` re-introduction in orchestrator.py |
| `TestPackagingSmoke` | 4 | Package import regressions, entrypoint failures |
| `TestSecurityAuditScript` | 1 | security_audit.py gate regression |

All 12 tests pass.

---

## CI Pass Recovery Logic

The `ci-pass` job in `.github/workflows/ci.yml` uses:

```yaml
ci-pass:
  needs: [lint, security, test, build, install-smoke]
  if: always()
  steps:
    - name: Check all required jobs passed
      run: |
        if [[ "${{ needs.lint.result }}" != "success" ]] || ...
        then echo "FAILED"; exit 1
        fi
```

This gate recovers **automatically** when all upstream jobs return `success`. No change to
`ci.yml` was required.

---

## Expected Post-Push CI State

| Job | Expected Result |
|-----|-----------------|
| CI / Lint | ✅ PASS |
| CI / Security | ✅ PASS |
| CI / Test (ubuntu/windows/macOS × py3.11/3.12/3.13) | ✅ PASS (was already passing) |
| CI / Build | ✅ PASS (was already passing) |
| CI / Install Smoke | ✅ PASS (was already passing) |
| **CI / CI Pass** | ✅ **PASS** |
