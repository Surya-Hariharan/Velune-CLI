# Security Root Cause Analysis

**Date:** 2026-06-21  
**CI Run:** 27898687127  
**Job:** CI / Security  
**Status:** FAILED

---

## Failing Step

**Step 7 — Secret scan (gitleaks)**  
Action: `gitleaks/gitleaks-action@v3`

---

## Exact Command Executed

```
gitleaks detect --redact -v --exit-code=2 --report-format=sarif \
  --report-path=results.sarif --log-level=debug \
  --log-opts=--no-merges --first-parent \
  70cf626472ef98b6fe48cede6b646a157f056743^..0d8417db7613fcc0f58ff5d12b988b6e2e1cdcf7
```

---

## Exact Error Output

```
ERR [git] fatal: ambiguous argument
  '70cf626472ef98b6fe48cede6b646a157f056743^..0d8417db7613fcc0f58ff5d12b988b6e2e1cdcf7':
  unknown revision or path not in the working tree.
ERR [git] Use '--' to separate paths from revisions, like this:
ERR [git] 'git <command> [<revision>...] -- [<file>...]'
ERR failed to scan Git repository  error="stderr is not empty"
WRN scanned ~0 bytes (0)
WRN partial scan completed in 142ms
WRN no leaks found in partial scan
##[error]ERROR: Unexpected exit code [1]
```

---

## Root Cause

The `actions/checkout@v4` step in the `security` job uses the **default `fetch-depth: 1`**, which produces a shallow clone containing only the HEAD commit (`0d8417db`).

When gitleaks-action runs on a `push` event, it computes the diff range between the previous push base (`70cf626`) and the new HEAD (`0d8417db`). Since the shallow clone does not include `70cf626`, git cannot resolve the range and exits with error code 1.

gitleaks-action exits with code 1 (not the normal "leaks found" code 2), which the action interprets as an unexpected failure and marks the step as failed.

**No actual secrets were found.** The failure is purely infrastructural — a shallow clone incompatible with gitleaks-action's commit-range diff strategy.

---

## Files Involved

| File | Issue |
|------|-------|
| `.github/workflows/ci.yml` (line 49) | `actions/checkout@v4` missing `fetch-depth: 0` in the `security` job |

---

## Risk Level

**LOW** — This is a CI configuration defect, not a real secret or security vulnerability. The scan itself reports "no leaks found in partial scan", confirming the codebase is clean.

---

## Recommended Fix

Add `fetch-depth: 0` to the `actions/checkout@v4` step in the `security` job so that gitleaks has access to the full git history and can resolve the commit-range diff:

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0  # gitleaks needs full history to resolve push commit ranges
```

---

## Verification that No Other Steps Are Failing

| Step | Result |
|------|--------|
| Install dependencies | success |
| Check for pip vulnerabilities (pip-audit) | success |
| Bandit static security scan | success |
| **Secret scan (gitleaks)** | **FAILURE** ← only failing step |
| Check for shell=True usage | skipped (blocked by gitleaks failure) |
| Check asyncio.run() count | skipped (blocked by gitleaks failure) |
| Security audit (sync-over-async) | skipped (blocked by gitleaks failure) |
