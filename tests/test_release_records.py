"""The version in the code, the CHANGELOG and the release record must agree.

A version can be bumped in ``velune/__init__.py`` long before it is tagged and published, and
the history then drifts: a CHANGELOG date for a release that does not exist, or a release table
that claims a tag it never had. These checks need no Git tags (CI checkouts do not have them);
they hold the three records that live in the repository to each other.
"""

from __future__ import annotations

import re
from pathlib import Path

import velune

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
ORIGIN = (ROOT / "docs" / "project-origin.md").read_text(encoding="utf-8")


def _headings() -> list[str]:
    return re.findall(r"^## \[(\d[^\]]*)\]", CHANGELOG, re.MULTILINE)


def test_newest_changelog_version_is_the_version_in_the_code():
    assert _headings()[0] == velune.__version__, (
        "Bumping velune.__version__ needs a matching CHANGELOG section at the top "
        "(and a row in docs/project-origin.md)"
    )


def test_changelog_has_an_unreleased_section_and_link():
    assert re.search(r"^## \[Unreleased\]", CHANGELOG, re.MULTILINE)
    assert re.search(
        r"^\[Unreleased\]: https://github\.com/\S+/compare/v\S+\.\.\.HEAD$", CHANGELOG, re.MULTILINE
    )


def test_release_record_has_a_row_for_the_current_version():
    assert re.search(rf"^\| {re.escape(velune.__version__)} \|", ORIGIN, re.MULTILINE), (
        f"docs/project-origin.md has no release-history row for {velune.__version__}"
    )


def test_changelog_and_release_table_agree_on_whether_the_current_version_is_released():
    heading = re.search(rf"^## \[{re.escape(velune.__version__)}\](.*)$", CHANGELOG, re.MULTILINE)
    row = re.search(rf"^\| {re.escape(velune.__version__)} \|(.*)$", ORIGIN, re.MULTILINE)
    assert heading is not None and row is not None
    changelog_says_unreleased = "unreleased" in heading.group(1).lower()
    table_says_not_released = "not released" in row.group(1).lower()
    assert changelog_says_unreleased == table_says_not_released, (
        "CHANGELOG marks the current version 'Unreleased' exactly when docs/project-origin.md says "
        "'Not released'; update both when it is tagged and published"
    )
    if not changelog_says_unreleased:
        assert re.search(r"\d{4}-\d{2}-\d{2}", heading.group(1)), "a released version needs a date"
