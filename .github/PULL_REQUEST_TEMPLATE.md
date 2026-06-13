# Pull Request

## Summary

Brief description of what this PR does and why.

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Provider addition
- [ ] Documentation
- [ ] Refactor
- [ ] Test improvement

## Checklist

- [ ] Tests added or updated for this change
- [ ] All existing tests pass: `pytest tests/ -v`
- [ ] Linting passes: `ruff check velune/`
- [ ] Type checking passes: `pyright velune/`
- [ ] Security checks pass locally: `pip-audit` and `bandit -c pyproject.toml -r velune/`
- [ ] `velune doctor check` passes on a clean install
- [ ] CHANGELOG.md updated under [Unreleased]
- [ ] README.md updated if user-facing behavior changed

## Testing done

Describe how you tested this change manually if applicable.
