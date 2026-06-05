# Pull Request

## Summary

<!-- What does this PR do and why? Link the related issue if one exists. -->

Closes #

## Changes

<!-- Bullet-point list of the key changes made. -->

-

## Test plan

<!-- How did you verify this works? What edge cases did you check? -->

- [ ] `pytest tests/ -q` passes locally
- [ ] Tested the golden path manually (describe below if non-trivial)

## Checklist

- [ ] Tests added or updated for new behaviour
- [ ] `ruff check velune/` passes with no errors
- [ ] `mypy velune/ --ignore-missing-imports` passes (or failures are pre-existing)
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
- [ ] No secrets, binaries, or generated artefacts committed
- [ ] Branch is rebased onto latest `main`
