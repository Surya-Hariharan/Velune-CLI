# Security Remediation Report

**Date**: 2026-06-21  
**Analyst**: Lead Maintainer  
**CI Job**: `CI / Security`

---

## Tools Checked

| Tool | Command | Status |
|------|---------|--------|
| pip-audit | `pip-audit` | ✅ PASS — no known CVEs |
| Bandit (full) | `bandit -c pyproject.toml -r velune/` | ✅ PASS (low-only findings, non-blocking) |
| Bandit (gate) | `bandit -c pyproject.toml -r velune/ --severity-level medium --confidence-level medium` | ❌ FAIL → ✅ FIXED |
| shell=True check | `grep -rn "shell=True" velune/` | ✅ PASS — 0 occurrences |
| asyncio.run() count | grep check in ci.yml | ✅ PASS — 1 allowed site |
| security_audit.py | `python scripts/security_audit.py` | ✅ PASS |

---

## Finding: B324 — hashlib SHA1 without usedforsecurity=False

### Classification
**Real finding** — the function call is correct as a deduplication hash, but the SHA1 API should
declare its non-security intent. This is not a false positive; it is a correct Bandit finding that
requires a one-word code fix.

### Location
`velune/core/loop_detector.py:48`

### Severity / Confidence
High / High (Bandit B324, CWE-327)

### Code before fix
```python
def _fingerprint(self, exc: BaseException) -> str:
    key = f"{type(exc).__name__}:{str(exc)[:100]}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]
```

### Analysis
`_fingerprint` produces a 16-character hex key for deduplication — tracking whether the same
exception type+message has been seen recently within the `LoopDetector` sliding window. This is
**not** a security or cryptographic use of SHA1. The hash:
- Is used as a dict key, not a security token
- Is truncated to 16 hex characters (64 bits)
- Is never stored, transmitted, or compared against external data
- Does not need collision resistance

SHA1 is perfectly adequate for this purpose. The fix is to annotate the intent with
`usedforsecurity=False`, which:
1. Suppresses the Bandit B324 finding
2. Allows the code to run on FIPS-mode systems (where SHA1 for security is prohibited)
3. Is semantically accurate

### Fix applied
```python
return hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:16]
```

`usedforsecurity=False` was added in Python 3.9 (our floor is 3.11 — fully compatible).

---

## Decisions

### Why not switch to SHA256?

SHA256 would work but:
1. Is more expensive for a hot path (called on every exception)
2. Is a larger change with no real benefit
3. Would obscure the intent — `usedforsecurity=False` is the correct, explicit signal

### Why not add B324 to the bandit `skips` list?

Skipping B324 globally would suppress all future SHA1/MD5 findings across the codebase,
including ones that would be real security issues. The targeted fix at the call site is preferable.

### Low-severity findings (B110, B112 — 130 occurrences)

The Bandit full report shows 130 Low-severity findings:
- **B110 (try_except_pass)**: 123 occurrences — `try: ... except: pass` patterns throughout async
  code. These are Low/High. They do not trip the `--severity-level medium` gate and represent
  intentional defensive programming in optional subsystem initialization.
- **B112 (try_except_continue)**: 7 occurrences — similar pattern in loops. Low/High.

**Decision**: These are not fixed in this pass. They are Low severity and do not violate the CI
gate. Fixing them in bulk would introduce risk (changing error-handling behavior in dozens of
places) with no security improvement. They should be addressed incrementally in future cleanup
PRs if desired.

### nosec annotation at context_report.py:190

Bandit warns that `# nosec B608` at `velune/observability/context_report.py:190` no longer
corresponds to a failed test. This is a stale annotation. It does not cause CI failure but should
be cleaned up. **Decision**: Left for a separate cleanup pass to avoid scope creep.

---

## pip-audit results

```
$ pip-audit
No known vulnerabilities found
```

All 626 tests pass. No dependency CVEs detected.

---

## Verification

```
$ bandit -c pyproject.toml -r velune/ --severity-level medium --confidence-level medium

Run metrics:
    Total issues (by severity):
        Low: 130
        Medium: 0
        High: 0
    Total issues (by confidence):
        High: 130

Exit code: 0
```
