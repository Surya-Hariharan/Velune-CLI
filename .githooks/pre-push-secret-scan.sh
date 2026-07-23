#!/usr/bin/env bash
# Local pre-push secret scan — wired via .pre-commit-config.yaml's
# `stages: [pre-push]` hook (see the "gitleaks-pre-push" entry there).
#
# CI's gitleaks step (.github/workflows/ci.yml, "Secret scan (gitleaks)")
# is the actual enforcement gate and always runs regardless of what happens
# locally. This hook exists to catch a secret *before* it ever leaves the
# machine, instead of only after a push already landed on GitHub — a
# leaked key is meaningfully worse once it's live on the remote, even for
# a few minutes before CI fails.
#
# Soft-fails (skips, exit 0) when gitleaks isn't installed locally, rather
# than blocking every push for contributors who haven't installed it —
# CI remains the hard gate either way. Install: https://github.com/gitleaks/gitleaks#installing
set -euo pipefail

if ! command -v gitleaks >/dev/null 2>&1; then
  echo "⚠️  gitleaks not installed locally — skipping pre-push secret scan (CI still runs it)."
  echo "    Install: https://github.com/gitleaks/gitleaks#installing"
  exit 0
fi

# Scan only the commits this push actually introduces, not the full
# repository history — same rationale as CI's SCAN_BASE/SCAN_HEAD range.
# `@{push}` is the commit this branch's upstream would be updated to;
# it fails on a brand-new branch with no upstream configured yet, so fall
# back to just the tip commit in that case.
if git rev-parse @{push} >/dev/null 2>&1; then
  range="@{push}..HEAD"
else
  range="-1"
fi

gitleaks detect --source . --redact -v --log-opts="${range}"
