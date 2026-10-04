# Project Governance

Velune CLI is an individual-led open-source project. There is no foundation,
company, or other legal entity behind it.

## Canonical repository and maintainer

- Canonical repository: <https://github.com/Surya-Hariharan/Velune-CLI>
- Original author and current maintainer: **Surya HA**
  ([@Surya-Hariharan](https://github.com/Surya-Hariharan))

## Contributions

Contributions are welcome through pull requests (see
[CONTRIBUTING.md](CONTRIBUTING.md)). The maintainer reviews every pull request
and decides what is merged. CI (lint, type checks, tests, and security checks)
must pass before merging.

Contributors are credited through the Git history and pull requests, and in
[CHANGELOG.md](CHANGELOG.md) where relevant. Contribution does not transfer or
create ownership of the overall project.

## Releases

Official releases are made by the maintainer from this repository. A release
consists of a `vX.Y.Z` Git tag, the GitHub Release for that tag, and the
matching `velune-cli` package version on PyPI. The release workflow checks
that the tag matches the package version.

## Changes in maintainership

If maintainership changes, the change will be announced in this repository
(an update to this file, [AUTHORS.md](AUTHORS.md), and the release notes).
Absent such an announcement here, this repository's current maintainer is the
one listed above.

## Forks

Forks and derivative works are permitted under the Apache-2.0 license. A fork
is a separate project: it is not the canonical Velune CLI and should not
present itself as such. The project name and branding are not licensed by
Apache-2.0 (see [NOTICE](NOTICE)).
