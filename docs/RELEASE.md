# Release Process

Velune uses an automated deployment pipeline to distribute versioned releases. This document details how releases are versioned, triggered, built, and published.

## Versioning Scheme
Velune adheres to [Semantic Versioning 2.0.0 (SemVer)](https://semver.org/).
- **Major release**: Architectural shifts or substantial breaking changes.
- **Minor release**: New features, new providers, new agent integrations.
- **Patch release**: Backward-compatible bug fixes, performance optimizations, and security patches.

The canonical version is stored in [velune/__init__.py](file:///c:/Users/surya/OneDrive/Desktop/Velune-CLI/velune/__init__.py):
```python
__version__ = "X.Y.Z"
```

---

## Release Workflow

All package packaging, publishing, and GitHub Release compilation are handled by the GitHub Actions workflow in [.github/workflows/release.yml](file:///c:/Users/surya/OneDrive/Desktop/Velune-CLI/.github/workflows/release.yml).

### Step 1 — Local Release Preparation

1. **Update Version**: Bump `__version__` in `velune/__init__.py`.
2. **Update Changelog**: Add a new version entry in `CHANGELOG.md` under a header matching `## [X.Y.Z] - YYYY-MM-DD`. Ensure all changes are categorized under `Added`, `Changed`, `Fixed`, or `Removed`.
3. **Commit & Tag**:
   ```bash
   git add velune/__init__.py CHANGELOG.md
   git commit -m "release: vX.Y.Z"
   git tag vX.Y.Z
   ```
4. **Push**:
   ```bash
   git push origin main && git push origin --tags
   ```

### Step 2 — Automated CI Pipeline Gate
Once the tag is pushed, GitHub Actions triggers the release workflow. The workflow immediately runs:
- **Ruff Lint & Format** checks.
- **Pyright** type checking.
- **Security Scans** (pip-audit, shell=True checks, and asyncio.run count checks).

### Step 3 — Tag Verification & Reproducible Build
- **Tag Validation**: The workflow extracts the version from the tag (e.g. `v1.2.3` becomes `1.2.3`) and compares it against `velune.__version__`. If there is a mismatch, the release job fails immediately.
- **Reproducible Package Build**: The package is built using Hatchling. The file metadata timestamps are set to the commit timestamp using `SOURCE_DATE_EPOCH` to ensure byte-for-byte reproducibility.
- **Metadata Check**: Twine runs a strict validation scan (`twine check --strict`) on the built artifacts.

### Step 4 — Trusted Publishing to PyPI
- **OIDC Authentication**: Instead of storing long-lived passwords or API tokens, the publisher uses OpenID Connect (OIDC).
- The release job requests a short-lived OIDC token from GitHub Actions and exchanges it with PyPI for a temporary publishing credential scoped to `velune-cli` project.

### Step 5 — GitHub Release Generation
- **Changelog Extraction**: A custom script reads `CHANGELOG.md` and extracts the section corresponding to the released version.
- **Release Creation**: The workflow creates a GitHub Release containing:
  - The extracted changelog section as the release description body.
  - The built distribution artifacts (`.tar.gz` and `.whl`) attached as release assets.
  - Pre-release flagging if the tag version has standard pre-release suffixes (like `alpha`, `beta`, `rc`).
