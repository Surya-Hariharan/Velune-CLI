"""Tests for LocalModelResolver's cross-platform GGUF discovery.

The scan-path algorithm must resolve correctly regardless of whose machine it
runs on: a different username, a Windows profile living on a drive other than
C:\\, or a home directory anywhere else on disk. Nothing in it should be
hardcoded to this developer's environment.
"""

from __future__ import annotations

from velune.providers.local_resolver import LocalModelResolver


def test_scan_paths_uses_localappdata_env_var_not_hardcoded_drive(monkeypatch):
    """Regression: previously assumed the Windows profile lives on C:\\Users."""
    monkeypatch.setenv("LOCALAPPDATA", "D:\\Users\\someoneelse\\AppData\\Local")

    paths = LocalModelResolver._scan_paths()
    lm_studio_paths = [p for p in paths if "LM Studio" in str(p) and "AppData" in str(p)]

    assert len(lm_studio_paths) == 1
    assert str(lm_studio_paths[0]).startswith("D:\\Users\\someoneelse")
    assert not any(str(p).startswith("C:/Users") or str(p).startswith("C:\\Users") for p in lm_studio_paths)


def test_scan_paths_falls_back_without_localappdata(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    paths = LocalModelResolver._scan_paths()
    # Non-Windows fallback locations should be present instead.
    assert any(p.as_posix() == "/usr/share/ollama/models" for p in paths)


def test_scan_paths_includes_home_based_defaults(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    paths = LocalModelResolver._scan_paths()
    suffixes = {".ollama/models", ".ollama\\models"}
    assert any(str(p).endswith(tuple(suffixes)) for p in paths)


def test_scan_gguf_files_finds_nested_files(tmp_path, monkeypatch):
    root = tmp_path / "models"
    nested = root / "sub" / "dir"
    nested.mkdir(parents=True)
    (nested / "model.gguf").write_bytes(b"fake")
    (root / "not_a_model.txt").write_bytes(b"fake")

    monkeypatch.setattr(LocalModelResolver, "_scan_paths", staticmethod(lambda: [root]))

    resolver = LocalModelResolver()
    found = resolver.scan_gguf_files()

    assert len(found) == 1
    assert found[0].name == "model.gguf"


def test_scan_gguf_files_skips_nonexistent_roots(monkeypatch, tmp_path):
    missing = tmp_path / "does_not_exist"
    monkeypatch.setattr(LocalModelResolver, "_scan_paths", staticmethod(lambda: [missing]))

    resolver = LocalModelResolver()
    assert resolver.scan_gguf_files() == []


def test_resolve_model_path_absolute(tmp_path):
    gguf = tmp_path / "my_model.gguf"
    gguf.write_bytes(b"fake")

    resolver = LocalModelResolver()
    assert resolver.resolve_model_path(str(gguf)) == gguf


def test_resolve_model_path_stem_fuzzy_match(tmp_path, monkeypatch):
    root = tmp_path / "models"
    root.mkdir()
    gguf = root / "llama3-8b-q4_k_m.gguf"
    gguf.write_bytes(b"fake")

    monkeypatch.setattr(LocalModelResolver, "_scan_paths", staticmethod(lambda: [root]))

    resolver = LocalModelResolver()
    resolved = resolver.resolve_model_path("llama3-8b-q4_k_m")
    assert resolved == gguf


def test_extract_quantization_and_family_from_filename():
    resolver = LocalModelResolver()
    assert resolver._extract_quantization("deepseek-coder-6.7b-q4_k_m") == "Q4_K_M"
    assert resolver._extract_family("deepseek-coder-6.7b-q4_k_m") == "deepseek"
