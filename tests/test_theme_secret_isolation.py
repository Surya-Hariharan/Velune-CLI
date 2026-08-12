"""Secrets never reach configuration, theme state, or any other plaintext sink.

Part 3C / Part 5. The credential-persistence fix in this rework touches the
model-registry cache and the config writer, both of which are plaintext files —
so these tests exist to prove neither route can start carrying key material.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import toml

from velune.kernel.config import VeluneConfig

SECRET = "gsk-super-secret-key-value-do-not-leak-0123456789"


@pytest.fixture
def repl(tmp_path: Path):
    stub = MagicMock()
    stub.container.get.side_effect = lambda key: {
        "runtime.workspace": tmp_path,
        "runtime.config_path": None,
        "runtime.config": VeluneConfig(),
    }.get(key)
    stub._fullscreen_ui = None
    return stub


# --- the config schema has nowhere to put a secret ---------------------------


def test_no_config_section_accepts_an_api_key_field():
    """Part 3C: the fix for credential persistence must not have been "put the
    key in velune.toml". Nothing in the schema is shaped to hold one."""
    dumped = VeluneConfig().model_dump()
    flat: list[str] = []

    def walk(node, prefix=""):
        if isinstance(node, dict):
            for key, value in node.items():
                flat.append(f"{prefix}{key}")
                walk(value, f"{prefix}{key}.")

    walk(dumped)
    for field in flat:
        leaf = field.rsplit(".", 1)[-1].lower()
        assert leaf not in ("api_key", "key", "secret", "token", "password"), (
            f"config field {field!r} looks like a secret holder"
        )


def test_theme_config_holds_only_a_theme_id():
    assert set(VeluneConfig().theme.model_dump()) == {"active"}


def test_provider_config_holds_env_var_names_not_values():
    """Non-secret *reference* information is fine (Part 3C explicitly allows
    it); the value itself must never be here."""
    providers = VeluneConfig().providers
    assert providers.openai is not None
    assert providers.openai.api_key_env == "OPENAI_API_KEY"
    assert not hasattr(providers.openai, "api_key")


# --- writing settings never writes a secret ----------------------------------


def test_persisting_a_theme_writes_no_credential_material(repl, tmp_path: Path, monkeypatch):
    from velune.cli.theme_state import persist_theme

    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    persist_theme(repl, "emerald")

    written = (tmp_path / "velune.toml").read_text(encoding="utf-8")
    assert SECRET not in written
    assert toml.loads(written)["theme"]["active"] == "emerald"


def test_the_model_registry_cache_carries_no_secrets(tmp_path: Path):
    """The cache is plaintext JSON and this rework made /connect write to it,
    so it is newly worth asserting that a descriptor carries no key."""
    from velune.core.types.model import ModelCapabilityProfile, ModelDescriptor
    from velune.models.registry_cache import ModelRegistryCache

    cache_path = tmp_path / "registry.json"
    cache = ModelRegistryCache(cache_path)
    cache.save(
        [
            ModelDescriptor(
                model_id="llama-3.3-70b",
                provider_id="groq",
                display_name="Llama 3.3 70B",
                context_length=8192,
                capabilities=ModelCapabilityProfile(),
                is_local=False,
            )
        ]
    )

    raw = cache_path.read_text(encoding="utf-8")
    assert SECRET not in raw
    payload = json.loads(raw)
    for model in payload["models"]:
        for field in model:
            assert field.lower() not in ("api_key", "key", "secret", "token")


# --- the credential store remains the only home for the secret ---------------


def test_the_secret_free_export_never_contains_a_key(monkeypatch):
    from velune.providers.keystore import export_provider_metadata

    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    snapshot = export_provider_metadata()

    for record in snapshot.values():
        assert record["key"] == "***"
    assert SECRET not in json.dumps(snapshot)


def test_the_credentials_file_is_not_plaintext_json(tmp_path: Path, monkeypatch):
    """If the store were ever written unencrypted this would parse as JSON."""
    from velune.providers.crypto import encrypt_credentials

    blob = encrypt_credentials(json.dumps({"providers": {"groq": {"key": SECRET}}}))
    assert SECRET.encode() not in blob
    with pytest.raises((json.JSONDecodeError, UnicodeDecodeError, ValueError)):
        json.loads(blob.decode("utf-8", errors="ignore"))


# --- diagnostics describe state, never the secret ----------------------------


def test_doctor_credential_check_reports_state_without_the_key(monkeypatch):
    from velune.cli.commands.doctor import _check_credential_chain

    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    result = _check_credential_chain()

    assert SECRET not in result["message"]
    assert result["status"] in ("ok", "warn", "fail")


def test_doctor_credential_check_source_never_reads_a_key_value():
    """Structural, not behavioural: a message built from `get_key(...)` would
    leak on some path even if this run happens not to take it."""
    src = Path("velune/cli/commands/doctor.py").read_text(encoding="utf-8")
    start = src.index("def _check_credential_chain")
    end = src.index("def _check_telemetry")
    body = src[start:end]

    # `get_key` is used, but only ever as a truthiness test.
    assert 'f"{keystore.get_key' not in body
    assert "get_key(pid)}" not in body
    assert 'record.get("key")' not in body


def test_provider_status_helper_exposes_location_not_content(monkeypatch):
    from velune.providers.keystore import get_provider_status

    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    status = get_provider_status("groq")

    assert SECRET not in json.dumps(status)
    assert status["location"] == "$GROQ_API_KEY"
