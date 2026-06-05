"""Tests for FilesystemScanner and SecretFileDetector."""
from pathlib import Path

import pytest

from velune.repository.indexer import SecretFileDetector
from velune.repository.scanner import FilesystemScanner


def test_veluneignore_excludes_env_files(temp_workspace: Path):
    env_file = temp_workspace / ".env"
    env_file.write_text("SECRET_KEY=abc123\nDATABASE_URL=postgres://localhost/db\n")

    (temp_workspace / ".veluneignore").write_text(".env\n")

    scanner = FilesystemScanner(temp_workspace)
    found = scanner.scan()

    found_names = {p.name for p in found}
    assert ".env" not in found_names, f".env should be excluded but found: {found_names}"


def test_secret_detector_catches_env():
    detector = SecretFileDetector()
    assert detector.is_likely_secret(".env", None) is True


def test_secret_detector_catches_private_key():
    detector = SecretFileDetector()
    # .pem extension is in SECRET_EXTENSIONS
    assert detector.is_likely_secret("server.pem", None) is True
    # .key extension is also a secret extension
    assert detector.is_likely_secret("private.key", None) is True
