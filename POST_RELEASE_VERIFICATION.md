# Post-Release Verification — Velune CLI v0.9.1

**Date:** 2026-06-21
**Release Engineer:** Claude Sonnet 4.6

---

## GitHub Release

| Check | Result | Detail |
|-------|--------|--------|
| Tag v0.9.1 exists | PASS | git tag -l shows v0.9.1 (annotated) |
| GitHub Release created | PASS | https://github.com/Surya-Hariharan/Velune-CLI/releases/tag/v0.9.1 |
| Release is not draft | PASS | draft: false |
| Release is not prerelease | PASS | prerelease: false |
| Wheel asset attached | PASS | velune_cli-0.9.1-py3-none-any.whl |
| sdist asset attached | PASS | velune_cli-0.9.1.tar.gz |
| Release notes | PASS | Full changelog excerpt present |

## CI Pipeline (run 27906922211)

| Job | Result |
|-----|--------|
| Lint (ruff + pyright) | PASS (18s) |
| Security (bandit, pip-audit, gitleaks, shell=True, asyncio.run) | PASS (53s) |
| Build & Validate Artifacts (twine --strict) | PASS (13s) |
| Wheel Install + REPL Smoke (ubuntu-latest, py3.11) | PASS (29s) |
| Wheel Install + REPL Smoke (ubuntu-latest, py3.13) | PASS (35s) |
| Wheel Install + REPL Smoke (windows-latest, py3.11) | PASS (1m12s) |
| Wheel Install + REPL Smoke (windows-latest, py3.13) | PASS (1m23s) |
| Wheel Install + REPL Smoke (macos-latest, py3.11) | PASS (26s) |
| Wheel Install + REPL Smoke (macos-latest, py3.13) | PASS (37s) |
| CI Pass (aggregate gate) | PASS (3s) |

## Release Workflow (run 27906944629)

| Job | Result | Notes |
|-----|--------|-------|
| Run CI Pipeline | PASS (59s) | Full CI re-run on tag |
| Build Package | PASS (13s) | Reproducible sdist + wheel |
| Publish to PyPI | FAIL | OIDC publisher not configured — see below |
| Create GitHub Release | SKIPPED | Depends on Publish (see workaround below) |

## PyPI Publication Status

**Status:** BLOCKED — Requires one-time PyPI OIDC trusted publisher setup.

**Error:** `invalid-publisher: valid token, but no corresponding publisher`

**Root cause:** PyPI OIDC trusted publishing requires a "Trusted Publisher" record to be
created on PyPI.org before the first publish. This is a per-project account configuration
step that cannot be automated from the repository side.

**Resolution steps (one-time, requires PyPI account login):**

1. Go to https://pypi.org/manage/account/publishing/
   (or if the project exists: https://pypi.org/manage/project/velune-cli/settings/publishing/)

2. Add a new Trusted Publisher with these exact values:
   - Owner:         Surya-Hariharan
   - Repository:    Velune-CLI
   - Workflow name: release.yml
   - Environment:   pypi

3. After setup, re-trigger the release workflow:
   gh workflow run release.yml --ref v0.9.1

   OR push a new patch tag (v0.9.1.post1) after making a trivial commit if PyPI
   requires a fresh version.

**Alternative (manual PyPI publish while OIDC is being configured):**

    pip install twine
    twine upload dist/velune_cli-0.9.1-py3-none-any.whl dist/velune_cli-0.9.1.tar.gz
    # Enter PyPI username/token when prompted

## Local Smoke Test

    velune --version   =>  velune v0.9.1   PASS
    velune --help      =>  Help text        PASS
    twine check --strict dist/*            PASS (both artifacts)

## Summary

| Component | Status |
|-----------|--------|
| Tag v0.9.1 | LIVE |
| CI (all 10 jobs) | GREEN |
| GitHub Release | LIVE with assets |
| PyPI publish | PENDING (OIDC config needed) |

The release is complete from a code and GitHub perspective.
PyPI requires a one-time trusted publisher configuration before the automated
workflow can publish. See resolution steps above.
