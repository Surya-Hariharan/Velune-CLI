# Dependency Security Report

**Date:** 2026-06-21  
**Tool:** pip-audit 2.10.1  
**CI Run:** 27898687127, Step 5 — "Check for pip vulnerabilities"  
**Result:** ✅ PASS — No vulnerable packages

---

## pip-audit Result

pip-audit was run against the full runtime dependency closure of `velune-cli` (installed via `pip install -e .`). No vulnerabilities were reported.

**CI step conclusion:** `success`

---

## Notable Dependency Constraint

The `[tool.uv]` section of `pyproject.toml` pins a security floor for `msgpack`:

```toml
[tool.uv]
# Security: force patched floors for transitive deps flagged by Dependabot.
# msgpack <=1.2.0: GHSA-6v7p-g79w-8964 (out-of-bounds read / Unpacker reuse crash)
constraint-dependencies = ["msgpack>=1.2.1"]
```

pip-audit confirmed `msgpack 1.2.1` is installed in the CI environment — the patched version.

---

## Optional Dependency Advisory (documented, not a CI failure)

The `[llamacpp]` optional extra is excluded from `[all]` due to a known advisory:

> **diskcache ≤5.6.3** — unsafe pickle deserialization. An attacker with write access to the cache directory can achieve arbitrary code execution. No patched version exists as of 2026-06.

This is documented in `pyproject.toml` lines 89–98. The advisory is:
- Not a vulnerability in velune-cli itself
- Only triggered if `pip install velune-cli[llamacpp]` is used in a multi-user or untrusted environment
- Correctly excluded from the default install and `[all]` extra

**Action:** No action required. Advisory is already documented and the extra is not included in CI scans.

---

## Packages Upgraded During This Fix

**None.** No dependency upgrades were required. pip-audit reported zero findings.

---

## Bandit Summary

Bandit 1.9.4 was run against `velune/` with:
- Severity level: medium+
- Confidence level: medium+
- Skipped rules: B101 (assert), B404/B603/B607 (subprocess — intentional, hardened)

**CI step conclusion:** `success` — Zero medium/high findings after skips.

The full low-severity report was printed for visibility but does not gate the build (the step uses `|| true` for the first pass and only fails on medium+).
