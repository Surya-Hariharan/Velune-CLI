# Releasing Velune

This document covers the one-time PyPI setup and the per-release checklist.

---

## One-time setup

### 1. Configure PyPI trusted publishing

Velune uses OIDC trusted publishing — no `PYPI_TOKEN` secret is needed.

1. Go to <https://pypi.org/manage/account/publishing/> (log in first).
2. Add a new publisher:
   - **PyPI project name:** `velune`
   - **Owner:** `Surya-Hariharan`
   - **Repository:** `Velune-CLI`
   - **Workflow filename:** `release.yml`
   - **Environment:** *(leave blank, or `pypi` if you add a GitHub environment)*
3. Click **Add**.

That's it. The release workflow authenticates via OIDC automatically — no
secrets to rotate.

### 2. (Optional) Add a GitHub deployment environment

For extra protection (required reviewers before publish):

1. GitHub repo → **Settings → Environments → New environment**.
2. Name it `pypi`.
3. Add required reviewers if you want a manual approval gate.
4. Uncomment the `environment: pypi` line in `.github/workflows/release.yml`.

---

## Per-release checklist

### Step 1 — Update the version

Edit [velune/__init__.py](../velune/__init__.py):

```python
__version__ = "x.y.z"
```

### Step 2 — Update CHANGELOG.md

In [CHANGELOG.md](../CHANGELOG.md):

1. Rename `## [Unreleased]` to `## [x.y.z] — YYYY-MM-DD`.
2. Add a new empty `## [Unreleased]` section at the top.
3. Add a comparison link at the bottom:

```markdown
[x.y.z]: https://github.com/Surya-Hariharan/Velune-CLI/compare/vPREV...vx.y.z
```

### Step 3 — Commit, tag, push

```bash
git add velune/__init__.py CHANGELOG.md
git commit -m "chore: release vx.y.z"
git tag vx.y.z
git push origin main --tags
```

### Step 4 — Watch the pipeline

Go to the **Actions** tab on GitHub. The release workflow runs:

1. **Validate Release Tag** — confirms `vx.y.z` matches `__version__`.
2. **Full Test Suite** — reruns the complete CI matrix.
3. **Publish to PyPI** — builds the wheel and sdist, publishes via OIDC.
4. **Create GitHub Release** — creates the GitHub Release with the
   CHANGELOG excerpt and attaches the wheel and sdist.

Total time: ~8–12 minutes.

---

## Version numbering

Velune follows [Semantic Versioning](https://semver.org/):

| Change | Version bump |
|--------|-------------|
| Breaking CLI change, removed command | MAJOR (`x+1.0.0`) |
| New command, new provider, new feature | MINOR (`x.y+1.0`) |
| Bug fix, doc fix, security patch | PATCH (`x.y.z+1`) |

Pre-release tags: append `-alpha.N`, `-beta.N`, or `-rc.N` to the version
tag (e.g. `v1.0.0-rc.1`). The release workflow automatically marks these
as pre-releases on GitHub.

---

## Verifying a release

After the pipeline completes:

```bash
pip install velune==x.y.z
velune --version   # should print x.y.z
velune doctor
```

Check the [PyPI project page](https://pypi.org/project/velune/) and the
[GitHub Releases page](https://github.com/Surya-Hariharan/Velune-CLI/releases)
to confirm the artifacts are present.
