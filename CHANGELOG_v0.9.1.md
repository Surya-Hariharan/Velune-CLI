# Changelog — Velune CLI v0.9.1

**Release Date:** 2026-06-21
**Release Type:** Stable (stabilization + trust-recovery)
**PyPI:** pip install velune-cli==0.9.1

This is a stabilization and trust-recovery release. No new user-facing features,
no breaking changes. pip install --upgrade velune-cli is a safe, drop-in update.

---

## Security

- Windows PATH-hijack guard now enforced. _is_trusted_path previously returned True
  unconditionally on Windows. Resolved binary must now live under a trusted system
  root, interpreter environment, or workspace venv.
  (velune/execution/command_spec.py)

- Interpreter inline-code execution blocked. python -c, node -e and similar flags
  are now rejected; running a file is still permitted.

- Removed llama-cpp-python transitive vulnerability. Eliminates diskcache <= 5.6.3
  unsafe pickle deserialization. pip-audit reports no known vulnerabilities.

- Execution-model documentation corrected. SECURITY.md now describes execution layer
  as a managed, resource-limited environment -- explicitly NOT an OS-level sandbox.

- Bandit static analysis added to CI (gates on medium+ severity).
  gitleaks secret scanning with full-history fetch added to CI.

- Bandit high/medium findings resolved: workspace-slug SHA-1 marked with
  usedforsecurity=False; Ollama HTTP client given bounded default timeout (60s/5s).

- msgpack upgraded 1.2.0 -> 1.2.1 (fixes GHSA-6v7p-g79w-8964 out-of-bounds read).

## Fixed

- Subprocess pipe-buffer deadlock in execution sandbox. Children writing more than
  ~64 KiB were killed as false timeouts with all output lost. Both pipes now drained
  concurrently on dedicated threads into memory-bounded buffers (default 10 MiB).
  (velune/execution/sandbox.py)

- Gitleaks CI fetch-depth fix. Added fetch-depth: 0 so gitleaks can resolve its
  required commit range base^..head. (.github/workflows/ci.yml)

- Memory coroutine never-awaited bugs in memory.py and repl.py.

- Cold-start BM25 empty retrieval index. _background_apply now writes a minimal
  retrieval_index.json when none exists.

- CHANGELOG path corrected from root CHANGELOG.md to docs/CHANGELOG.md.

- Pyright type error in Cohere adapter: _to_cohere_messages return type corrected
  from tuple[str, list[dict]] to tuple[str, list[dict], str].
  (velune/providers/adapters/cohere.py)

- Ruff lint regressions: 33 errors fixed across 15 files plus 3 additional issues
  (B007, N806, UP042) fixed in this release prep.

- CI regression fix: orchestrator.py progress_callback guarded with None check
  to fix Pyright reportOptionalCall.

## Added

- velune doctor runtime path-safety check -- new Security-category diagnostic
  validates each allowlisted executable against _is_trusted_path guard.
  (velune/cli/commands/doctor.py)

- velune context and velune trace observability commands, proving indexing and
  execution are real from on-disk state.
  (velune/observability/, velune/commands/context.py, velune/commands/trace.py)

- New provider adapters -- Cohere and additional cloud providers reorganized.
  (velune/providers/adapters/)

- /dashboard command -- live full-screen Rich layout with jobs table, alerts,
  and provider health panels at 500ms refresh. (velune/cli/display/dashboard.py)

- /jobs command -- list or cancel background jobs; JobRegistry with JobRecord
  and JobStatus enum. (velune/core/task_registry.py)

- velune doctor Council panel -- shows role assignment coverage or warnings.

## Changed

- CI test matrix expanded to Ubuntu/Windows/macOS x Python 3.11/3.13.

- Release pipeline uses OIDC trusted publishing (no long-lived PyPI token).
  Removed continue-on-error that silently swallowed failed publishes.

- Reproducible builds: SOURCE_DATE_EPOCH pinned to commit; hatch reproducible=true.

- Release pipeline tag-version assertion: build fails fast on tag/version mismatch.

- Pydantic v1 -> v2: Event model migrated from class Config to ConfigDict.

- Dependabot: groups minor/patch bumps; correct reviewer handle.

- Design system consolidated into velune/cli/design.py with semantic tokens.

- Provider code reorganized into velune/providers/adapters/.

## Developer Experience

- New CI build + install-smoke jobs: cross-platform wheel-install smoke tests.

- Python 3.13 classifier and Typing :: Typed classifier added to pyproject.toml.

- Unit tests for execution/validator.py raised from 16% to 90% coverage.

- 12 CI regression tests in test_ci_regression.py prevent recurrence of findings.

## Packaging

- pyproject.toml license classifier corrected to Apache Software License.
- [tool.uv] constraint-dependencies floor: msgpack>=1.2.1.
- [llamacpp] extra excluded from [all] -- install explicitly if needed.
- MANIFEST.in and uv.lock included in repository.

## Known Limitations

- No OS-level sandbox. The execution layer is managed and resource-limited,
  not an OS sandbox. Docker extra provides stronger isolation when available.
- Test suite currently maintained in a separate git worktree.
- Startup time ~3.6s (target 3.0s; optimization deferred to v1.0.0).
- Cloud provider integration incomplete for some APIs.

---

## Upgrade Guide

    pip install --upgrade velune-cli==0.9.1
    velune --version  # should print: velune v0.9.1

No configuration changes required. All .velune/ workspace files are forward-compatible.

Full diff: https://github.com/Surya-Hariharan/Velune-CLI/compare/v0.9.0...v0.9.1
