# CI Failure Analysis

**Date**: 2026-06-21  
**Run ID**: 27897853733 ("chore: remove all reports")  
**Branch**: main  
**Analyst**: Lead Maintainer

---

## Summary

| Job | Status | Root Cause |
|-----|--------|-----------|
| Lint | ❌ FAIL | Pyright `reportOptionalCall` error at `orchestrator.py:765` |
| Security | ❌ FAIL | Bandit B324 `hashlib.sha1` without `usedforsecurity=False` at `loop_detector.py:48` |
| CI Pass | ❌ FAIL | Depends on Lint and Security — fails because both upstream jobs failed |
| All other jobs (19) | ✅ PASS | — |

---

## Failure 1 — Lint / Pyright type checking

### Workflow
`CI` → Job: `lint` → Step: `Pyright type checking`

### Failed command
```
pyright velune/
```

### Exact error output
```
velune/cognition/orchestrator.py:765:17 - error: Object of type "None" cannot be called (reportOptionalCall)
1 error, 1 warning, 0 informations
Process completed with exit code 1.
```

### Root cause
`_execute_tiered()` in `velune/cognition/orchestrator.py` declares:
```python
progress_callback: Callable[[str], None] | None = None
```
The parameter is optional (`| None`). At line 765, it is called unconditionally:
```python
progress_callback(f"[Model Assignment] {_assignment_str}")
```
Pyright's `reportOptionalCall` diagnostic correctly identifies that calling a `None`-typed object is a type error. This is not suppressed in `pyproject.toml [tool.pyright]` (unlike most other diagnostics which have been silenced). The call is inside a `try/except Exception: pass` block which would catch the resulting `TypeError` at runtime, but the type error is still real — the function parameter may be `None` and the call is unguarded at the type level.

### Affected file
`velune/cognition/orchestrator.py`, line 765

### Recommended fix
Add a narrowing guard at the top of `_execute_tiered` that creates a safe local callable:
```python
def _emit(msg: str) -> None:
    if progress_callback is not None:
        progress_callback(msg)
```
Replace all `progress_callback(` calls within `_execute_tiered` with `_emit(`. This narrows the type for pyright, eliminates the latent runtime `TypeError`, and preserves existing behavior.

**Alternative (not recommended)**: Add `reportOptionalCall = false` to `[tool.pyright]` in `pyproject.toml`. This would silence the check globally, masking future occurrences.

### Risk level
**Low** — The call is already inside a `try/except` so it cannot crash the program. The fix is a safe mechanical substitution.

---

## Failure 2 — Security / Bandit static security scan

### Workflow
`CI` → Job: `security` → Step: `Bandit static security scan`

### Failed command
```
bandit -c pyproject.toml -r velune/ --severity-level medium --confidence-level medium
```

### Exact error output
```
>> Issue: [B324:hashlib] Use of weak SHA1 hash for security. Consider usedforsecurity=False
   Severity: High   Confidence: High
   CWE: CWE-327 (https://cwe.mitre.org/data/definitions/327.html)
   Location: velune/core/loop_detector.py:48:15

Run metrics:
    Total issues (by severity):
        Low: 130
        Medium: 0
        High: 1
    Total issues (by confidence):
        High: 131

Process completed with exit code 1.
```

### Root cause
`_fingerprint()` in `velune/core/loop_detector.py` uses `hashlib.sha1()` to create a short
deduplication key for exception loop detection:
```python
def _fingerprint(self, exc: BaseException) -> str:
    key = f"{type(exc).__name__}:{str(exc)[:100]}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]
```
Bandit B324 flags any `hashlib.sha1()` or `hashlib.md5()` call without the
`usedforsecurity=False` keyword argument, at Severity: High, Confidence: High.

SHA1 **is not used for security here** — it is used purely as a fast, deterministic
hash for creating a 16-character deduplication key (no cryptographic properties required).
Python 3.9+ accepts `usedforsecurity=False` to suppress both this warning and OS-level
restrictions on FIPS systems.

The CI gate is `--severity-level medium --confidence-level medium`. This finding is
High/High and is the **only** finding that exceeds the gate threshold.

### Affected file
`velune/core/loop_detector.py`, line 48

### Recommended fix
```python
return hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:16]
```
This is the correct, semantically accurate fix: it explicitly declares the intent
(non-security hash) while preserving SHA1 for its speed and determinism.

**Alternative**: Replace with `hashlib.sha256()` which is never flagged. However that
is a heavier operation for a hot path (exception deduplication on every exception).

### Risk level
**Very Low** — The only change is adding `usedforsecurity=False`. Behavior is
identical. Compatible with Python 3.9+ (our floor is 3.11).

---

## Failure 3 — CI Pass

### Workflow
`CI` → Job: `ci-pass`

### Failed command
```bash
if [ "${{ needs.lint.result }}" != "success" ] || \
   [ "${{ needs.security.result }}" != "success" ] || ...
```

### Root cause
Purely downstream: `ci-pass` checks that all upstream jobs succeeded. Because `lint`
and `security` both failed, this gate job fails deterministically.

### Affected file
`.github/workflows/ci.yml` — no change needed here.

### Recommended fix
Fix Lint and Security (Failures 1 and 2). `ci-pass` will recover automatically.

### Risk level
**None** — no code change required.

---

## Additional Observations (non-blocking)

| Observation | Detail | Action |
|-------------|--------|--------|
| Pyright warnings | `reportMissingModuleSource` for `docker`, `psutil`, `networkx`, `toml` | These are **warnings**, not errors. They occur in CI because `pip install ruff pyright` does not install the full package closure. They are suppressed by `reportMissingImports = false` but `reportMissingModuleSource` is a separate rule. No action needed — they do not cause CI failure. |
| Bandit B110/B112 (130 Low findings) | `try/except pass` and `try/except continue` patterns | Severity: Low. These do not trip the `--severity-level medium` gate. Adding B110 and B112 to the `skips` list in `[tool.bandit]` is optional but would clean up the full-report output. |
| `nosec` annotation warning | `velune/observability/context_report.py:190` has `# nosec B608` but bandit no longer triggers B608 there | Stale annotation. Does not fail build but creates a warning. |

---

## Implementation Plan

1. Fix `velune/core/loop_detector.py:48` — add `usedforsecurity=False`
2. Fix `velune/cognition/orchestrator.py` — add `_emit()` wrapper in `_execute_tiered`
3. Verify locally: `pyright velune/` → 0 errors, `bandit -c pyproject.toml -r velune/ --severity-level medium --confidence-level medium` → exit 0
4. Run full test suite — verify no regressions
5. Commit and push
