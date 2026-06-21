# Version Bump Report — 0.9.0 -> 0.9.1

**Date:** 2026-06-21

---

## Version Source of Truth

Velune CLI uses hatchling dynamic versioning. The single canonical version lives in
velune/__init__.py and is read by [tool.hatch.version] path = "velune/__init__.py".

---

## All Version References Audited

| Location | Status | Notes |
|----------|--------|-------|
| velune/__init__.py line 3 | CORRECT | __version__ = "0.9.1" |
| pyproject.toml [tool.hatch.version] | CORRECT | dynamic - reads __init__.py |
| docs/CHANGELOG.md | CORRECT | [0.9.1] entry present |
| docs/CHANGELOG.md links | CORRECT | compare URL v0.9.0...v0.9.1 |
| .github/workflows/release.yml | CORRECT | reads velune.__version__ at runtime |
| .github/workflows/ci.yml | CORRECT | no hardcoded version |
| README.md | CORRECT | no hardcoded version string |

---

## Conclusion

The version was already set to 0.9.1 in velune/__init__.py from prior release prep work
(commit b459bbf "docs: update changelog for v0.9.1 release"). No version bump commit required.

The release pipeline asserts tag == velune.__version__ before building, ensuring consistency.
