#!/usr/bin/env python3
"""Verify and checksum release artifacts. Standard library only.

Used by ``.github/workflows/release.yml``; safe to run locally on any ``dist/``.

    release_artifacts.py verify    --dist dist --version 0.9.9
    release_artifacts.py checksums --dist dist --output checksums/SHA256SUMS
    release_artifacts.py check     --dist dist --sums checksums/SHA256SUMS

``verify`` fails unless ``dist/`` holds exactly one non-empty wheel and one
non-empty sdist for the expected version, and both open and carry matching
metadata. ``checksums`` writes a ``sha256sum``-compatible manifest. ``check``
re-verifies a manifest against the files, so tampering or truncation between
workflow jobs is caught before publishing.

A SHA-256 manifest proves the files were not corrupted or swapped *after* it
was generated. It does not prove who produced them: anyone able to replace the
artifacts can replace the manifest too. It is an integrity check, not a
signature.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tarfile
import zipfile
from pathlib import Path

PACKAGE = "velune_cli"  # normalised distribution name (PEP 503 / PEP 625)
CHECKSUM_FILENAME = "SHA256SUMS"


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(dist: Path) -> list[Path]:
    """Regular files in *dist*, in a fixed (bytewise-by-name) order."""
    return sorted((p for p in dist.iterdir() if p.is_file()), key=lambda p: p.name)


def _check_wheel(path: Path, version: str) -> list[str]:
    problems: list[str] = []
    parts = path.name[: -len(".whl")].split("-")
    if len(parts) != 5 or parts[4] != "any" or parts[2:5] != ["py3", "none", "any"]:
        problems.append(f"{path.name}: expected a pure-Python 'py3-none-any' wheel")
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad is not None:
                problems.append(f"{path.name}: corrupt member {bad}")
                return problems
            names = set(archive.namelist())
            if f"{PACKAGE}-{version}.dist-info/METADATA" not in names:
                problems.append(f"{path.name}: missing {PACKAGE}-{version}.dist-info/METADATA")
            else:
                meta = archive.read(f"{PACKAGE}-{version}.dist-info/METADATA").decode("utf-8")
                if not re.search(rf"^Version: {re.escape(version)}$", meta, re.MULTILINE):
                    problems.append(f"{path.name}: METADATA Version is not {version}")
            if "velune/__init__.py" not in names:
                problems.append(f"{path.name}: package velune/__init__.py is missing")
    except zipfile.BadZipFile:
        problems.append(f"{path.name}: not a valid zip archive")
    return problems


def _check_sdist(path: Path, version: str) -> list[str]:
    try:
        with tarfile.open(path, "r:gz") as archive:
            names = archive.getnames()
    except (tarfile.TarError, OSError, EOFError) as exc:
        return [f"{path.name}: not a valid .tar.gz ({type(exc).__name__})"]
    # The top-level directory name varies with the build backend's normalisation.
    if not any(re.fullmatch(r"[^/]+/PKG-INFO", n) for n in names):
        return [f"{path.name}: PKG-INFO is missing"]
    if not any(n.endswith("/velune/__init__.py") for n in names):
        return [f"{path.name}: package velune/__init__.py is missing"]
    return []


def verify_dist(dist: Path, version: str) -> list[str]:
    """Return a list of problems; an empty list means the artifacts are sound."""
    if not dist.is_dir():
        return [f"{dist}: not a directory"]
    entries = sorted(dist.iterdir(), key=lambda p: p.name)
    problems = [f"{p.name}: unexpected non-file entry" for p in entries if not p.is_file()]
    files = [p for p in entries if p.is_file()]
    if not files:
        return [*problems, f"{dist}: no artifacts found"]

    wheels = [p for p in files if p.name.endswith(".whl")]
    sdists = [p for p in files if p.name.endswith(".tar.gz")]
    others = [p for p in files if p not in wheels and p not in sdists]
    problems += [f"{p.name}: unexpected file in dist/" for p in others]

    if len(wheels) != 1:
        problems.append(f"expected exactly 1 wheel, found {len(wheels)}")
    if len(sdists) != 1:
        problems.append(f"expected exactly 1 sdist, found {len(sdists)}")

    for path in (*wheels, *sdists):
        if path.stat().st_size == 0:
            problems.append(f"{path.name}: file is empty")
            continue
        stem = path.name[: -len(".whl")] if path in wheels else path.name[: -len(".tar.gz")]
        pieces = stem.split("-")
        name, found_version = pieces[0], pieces[1] if len(pieces) > 1 else ""
        if path in sdists:
            name, _, found_version = stem.rpartition("-")
        if _normalise(name) != PACKAGE:
            problems.append(f"{path.name}: distribution name is not velune-cli")
        elif found_version != version:
            problems.append(f"{path.name}: version {found_version!r} does not match {version!r}")
        elif path in wheels:
            problems += _check_wheel(path, version)
        else:
            problems += _check_sdist(path, version)
    return problems


def build_checksums(dist: Path) -> str:
    """``sha256sum``-format manifest: ``<hex>  <name>`` per file, sorted by name."""
    return "".join(
        f"{_sha256(path)}  {path.name}\n" for path in _files(dist) if path.name != CHECKSUM_FILENAME
    )


def check_checksums(dist: Path, sums: Path) -> list[str]:
    """Verify every file in *dist* against the manifest, and vice versa."""
    if not sums.is_file() or sums.stat().st_size == 0:
        return [f"{sums}: checksum manifest is missing or empty"]
    expected: dict[str, str] = {}
    first_line: dict[str, int] = {}
    duplicated: set[str] = set()
    problems: list[str] = []
    for number, line in enumerate(sums.read_text(encoding="utf-8").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if not match:
            problems.append(f"{sums.name}:{number}: malformed line")
            continue
        digest, name = match.groups()
        if name in expected:
            # A filename must appear exactly once. Whether the repeat carries
            # the same hash or a different one, neither entry is trusted:
            # picking the first or the last would let a stray line decide the
            # outcome.
            duplicated.add(name)
            problems.append(
                f"{sums.name}:{number}: duplicate entry for {name} "
                f"(first listed on line {first_line[name]})"
            )
            continue
        expected[name] = digest
        first_line[name] = number
    present = {p.name: p for p in _files(dist) if p.name != CHECKSUM_FILENAME}
    for name, digest in expected.items():
        if name in duplicated:
            continue  # already reported; its digest is ambiguous, so don't compare it
        path = present.get(name)
        if path is None:
            problems.append(f"{name}: listed in the manifest but missing from {dist}")
        elif _sha256(path) != digest:
            problems.append(f"{name}: checksum mismatch")
    problems += [
        f"{name}: present in {dist} but not in the manifest"
        for name in sorted(set(present) - set(expected))
    ]
    return problems


def _report(problems: list[str], ok_message: str) -> int:
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1
    print(ok_message)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="check dist/ holds one sound wheel and sdist")
    verify.add_argument("--dist", type=Path, required=True)
    verify.add_argument("--version", required=True)

    checksums = sub.add_parser("checksums", help="write a SHA-256 manifest for dist/")
    checksums.add_argument("--dist", type=Path, required=True)
    checksums.add_argument("--output", type=Path, required=True)

    check = sub.add_parser("check", help="verify dist/ against a SHA-256 manifest")
    check.add_argument("--dist", type=Path, required=True)
    check.add_argument("--sums", type=Path, required=True)

    args = parser.parse_args(argv)

    if args.command == "verify":
        return _report(
            verify_dist(args.dist, args.version),
            f"OK: {args.dist} holds a valid wheel and sdist for {args.version}",
        )
    if args.command == "checksums":
        if not args.dist.is_dir():
            return _report([f"{args.dist}: not a directory"], "")
        text = build_checksums(args.dist)
        if not text:
            return _report([f"{args.dist}: no artifacts to checksum"], "")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n": the manifest must be byte-identical on every OS.
        with args.output.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        print(text, end="")
        return 0
    return _report(
        check_checksums(args.dist, args.sums),
        f"OK: every file in {args.dist} matches {args.sums.name}",
    )


if __name__ == "__main__":
    sys.exit(main())
