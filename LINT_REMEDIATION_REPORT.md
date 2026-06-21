# Lint Remediation Report

**Date**: 2026-06-21  
**Analyst**: Lead Maintainer  
**CI Job**: `CI / Lint`

---

## Issue

Pyright type checking exited with code 1 due to one error:

```
velune/cognition/orchestrator.py:765:17 - error: Object of type "None" cannot be called
(reportOptionalCall)
```

The `_execute_tiered()` method declares `progress_callback: Callable[[str], None] | None = None`.
At line 765, inside a `try/except Exception: pass` block, the callback was called without a
type-narrowing guard:

```python
# Before
progress_callback(f"[Model Assignment] {_assignment_str}")
```

Pyright correctly identifies this as a potential `None` call. The `try/except` block would catch
the `TypeError` at runtime, but the type error is still real and Pyright flags it.

---

## Tools Checked

| Tool | Command | Status |
|------|---------|--------|
| Ruff lint | `ruff check velune/` | ✅ PASS (no changes needed) |
| Ruff format | `ruff format --check velune/` | ✅ PASS (no changes needed) |
| Pyright | `pyright velune/` | ❌ FAIL → ✅ FIXED |

---

## Fix Applied

**File**: `velune/cognition/orchestrator.py`, line 765  
**Change**: Added `if progress_callback is not None:` guard before the call.

```python
# After
if progress_callback is not None:
    progress_callback(f"[Model Assignment] {_assignment_str}")
```

This is a targeted, minimal fix that:
1. Narrows the type from `Callable | None` to `Callable` before the call
2. Makes the existing `try/except Exception: pass` block redundant for this specific TypeError
3. Preserves all existing behavior — if `progress_callback` is `None`, the message is silently skipped (same as before when `TypeError` was caught)
4. Does not touch any other call sites — the other 9 `progress_callback(...)` calls in the function are either already guarded by `if progress_callback:` or inside narrower try/except blocks that Pyright does not flag

---

## Decisions

### Why not add `reportOptionalCall = false` to pyproject.toml?

That would silence the check globally, hiding future real bugs. The underlying code had a latent
type error that has now been fixed properly.

### Why not add an `_emit()` wrapper for the whole function?

The single targeted fix is sufficient. The other call sites are not flagged by Pyright (they either have
existing guards or pyright's flow analysis resolves the type). Adding a wrapper changes more code than
needed for a focused patch.

### Why not change the parameter default to a no-op lambda?

```python
progress_callback: Callable[[str], None] = lambda _: None
```

That would work but changes the public API — callers currently pass `None` to mean "no callback" and
code elsewhere checks `if progress_callback:`. Keeping the `| None` type is semantically correct.

---

## Verification

```
$ pyright velune/
0 errors, 1 warning, 0 informations
```

The remaining 1 warning is `reportMissingModuleSource` for the optional `docker` package — it is a
warning (not an error) and Pyright exits 0 for warnings alone. It is suppressed in CI context
because `reportMissingImports = false` is set, but `reportMissingModuleSource` is a separate rule
that fires when the package is not installed. This warning does not cause CI failure.

```
$ ruff check velune/
All checks passed!

$ ruff format --check velune/
356 files already formatted
```
