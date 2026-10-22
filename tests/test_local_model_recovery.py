"""Local models survive drives and servers going away, and come back by themselves.

A real loopback HTTP server stands in for Ollama (``tests/fake_ollama.py``) and
a temp directory stands in for the external SSD holding the model store. No
real drive, model, keyring or provider is touched.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from tests.fake_ollama import FakeOllama
from velune.models.local_models import (
    Availability,
    FailureKind,
    LocalModelRecord,
    LocalModelRegistry,
    LocalModelStoreError,
    check_registration,
)
from velune.models.registry import ModelCapabilityRegistry, is_available
from velune.providers.discovery.ollama import OllamaDiscovery
from velune.providers.discovery.scanner import ModelDiscoveryScanner
from velune.providers.local_import import PathKind, inspect_path, read_gguf_metadata

MODEL = "qwen2.5-coder:7b"


# ---------------------------------------------------------------------------
# Fixtures: an Ollama store on a "drive", a fake daemon, an isolated home
# ---------------------------------------------------------------------------


def make_store(root: Path, names: list[str]) -> Path:
    """Write Ollama's on-disk layout (manifests + content-addressed blobs)."""
    (root / "blobs").mkdir(parents=True, exist_ok=True)
    config = json.dumps(
        {"model_format": "gguf", "model_family": "qwen2", "model_type": "7.6B"}
    ).encode()
    digest = hashlib.sha256(config).hexdigest()
    (root / "blobs" / f"sha256-{digest}").write_bytes(config)
    for name in names:
        model, tag = name.split(":")
        manifest = root / "manifests" / "registry.ollama.ai" / "library" / model / tag
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"config": {"digest": f"sha256:{digest}"}, "layers": []}), encoding="utf-8"
        )
    return root


@pytest.fixture
def env(tmp_path, monkeypatch):
    from velune.providers import ollama_locations
    from velune.providers.discovery import gpu

    monkeypatch.setattr(ollama_locations, "DEFAULT_LOCATIONS_PATH", tmp_path / "locations.json")
    monkeypatch.setattr(ollama_locations, "_default_roots", lambda: [])
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.setattr(gpu.GPUDetector, "detect", lambda self: {})
    from velune.models import registry_cache

    monkeypatch.setattr(registry_cache, "DEFAULT_CACHE_PATH", tmp_path / "cache.json")
    from velune.cli import model_prefs

    monkeypatch.setattr(model_prefs, "DEFAULT_PREFS_PATH", tmp_path / "active_model.json")
    monkeypatch.chdir(tmp_path)

    fake = FakeOllama().start()
    monkeypatch.setenv("OLLAMA_HOST", fake.host)
    ssd = make_store(tmp_path / "ssd" / "models", [MODEL])
    ollama_locations.OllamaLocationRegistry().add(ssd)
    fake.models = [MODEL]  # the daemon serves the SSD store
    yield SimpleNamespace(
        tmp=tmp_path, fake=fake, ssd=ssd, local=LocalModelRegistry(tmp_path / "local.json")
    )
    fake.stop()


async def _no_server() -> bool:
    return False


def make_registry(env, monkeypatch) -> ModelCapabilityRegistry:
    """A registry whose scanner runs only Ollama discovery (no LM Studio/ports/GGUF scans)."""
    from velune.providers.discovery.lmstudio import LMStudioDiscovery
    from velune.providers.discovery.openai_compat import OpenAICompatDiscovery

    monkeypatch.setattr(LMStudioDiscovery, "is_running", classmethod(lambda cls: _no_server()))
    monkeypatch.setattr(OpenAICompatDiscovery, "is_running", classmethod(lambda cls: _no_server()))
    scanner = ModelDiscoveryScanner()
    scanner._discoverers = [OllamaDiscovery()]
    return ModelCapabilityRegistry(scanner, local_models=env.local)


def unplug(env) -> None:
    env.ssd.parent.rename(env.tmp / "ssd-unplugged")
    env.fake.models = []  # the daemon can no longer see the store


def replug(env) -> None:
    (env.tmp / "ssd-unplugged").rename(env.ssd.parent)
    env.fake.models = [MODEL]


# ---------------------------------------------------------------------------
# The external SSD journey
# ---------------------------------------------------------------------------


async def test_ssd_unplugged_and_replugged_at_the_same_path(env, monkeypatch):
    registry = make_registry(env, monkeypatch)
    await registry.refresh()
    model = registry.get(MODEL, "ollama")
    assert model is not None and is_available(model)
    assert registry.remember_local(model)  # what activating it does
    saved = env.local.get("ollama", MODEL)
    assert saved.source_path == str(env.ssd) and saved.endpoint == env.fake.url

    # Unplug mid-session: kept, marked unavailable, with the path in the reason.
    unplug(env)
    await registry.refresh()
    offline = registry.get(MODEL, "ollama")
    assert offline is not None and not is_available(offline)
    assert offline.metadata["unavailable_kind"] == FailureKind.STORAGE_UNAVAILABLE
    assert str(env.ssd) in offline.metadata["unavailable_reason"]
    record = env.local.get("ollama", MODEL)
    assert record.source_path == str(env.ssd)  # configuration preserved
    assert record.status == Availability.UNAVAILABLE

    # Restart while still unplugged: still listed, still explained.
    restarted = make_registry(env, monkeypatch)
    at_start = restarted.get(MODEL, "ollama")
    assert at_start is not None and not is_available(at_start)

    # Replug at the same path: a recheck restores it with no reconfiguration.
    replug(env)
    back = await restarted.recheck(at_start, force=True)
    assert is_available(back)
    record = env.local.get("ollama", MODEL)
    assert record.status == Availability.AVAILABLE and record.source_path == str(env.ssd)
    assert record.last_verified is not None


async def test_ssd_missing_at_startup(env, monkeypatch):
    registry = make_registry(env, monkeypatch)
    await registry.refresh()
    registry.remember_local(registry.get(MODEL, "ollama"))
    unplug(env)

    cold = make_registry(env, monkeypatch)
    await cold.refresh()
    model = cold.get(MODEL, "ollama")
    assert model is not None and not is_available(model)
    assert model.metadata["unavailable_kind"] == FailureKind.STORAGE_UNAVAILABLE


async def test_server_offline_while_storage_is_present(env):
    env.local.upsert(
        LocalModelRecord(
            "ollama",
            MODEL,
            endpoint=env.fake.url,
            source_path=str(env.ssd),
            source_kind="ollama_store",
        )
    )
    env.fake.stop()
    result = await check_registration(env.local.get("ollama", MODEL))
    assert not result.available
    assert result.kind is FailureKind.ENDPOINT_UNREACHABLE
    assert env.fake.host in result.reason


async def test_storage_present_but_model_not_served(env):
    env.fake.models = []
    record = LocalModelRecord("ollama", MODEL, source_path=str(env.ssd), source_kind="ollama_store")
    result = await check_registration(record)
    assert result.kind is FailureKind.NOT_SERVED

    other = LocalModelRecord(
        "ollama", "llama3:8b", source_path=str(env.ssd), source_kind="ollama_store"
    )
    result = await check_registration(other)
    assert result.kind is FailureKind.MODEL_MISSING


async def test_other_providers_keep_working_while_a_local_model_is_down(env, monkeypatch):
    registry = make_registry(env, monkeypatch)
    from velune.core.types.model import ModelDescriptor

    registry.register(
        ModelDescriptor(
            model_id="gpt-4o",
            provider_id="openai",
            display_name="gpt-4o",
            context_length=1,
            capabilities={},
        )
    )
    unplug(env)
    await registry.refresh()
    assert is_available(registry.get("gpt-4o", "openai"))


# ---------------------------------------------------------------------------
# REPL behavior: activation and prompts against an unavailable model
# ---------------------------------------------------------------------------


class _Container:
    def __init__(self, registry) -> None:
        self._items = {"runtime.model_registry": registry}

    def get(self, key):
        return self._items.get(key)


def fake_repl(registry, active=None):
    console = Console(record=True, width=200, color_system=None)
    repl = SimpleNamespace(
        console=console,
        container=_Container(registry),
        active_model=active,
        _pending_prompt_text="",
        _completer=None,
    )
    repl._require = lambda key, label: repl.container.get(key)
    return repl


async def test_activation_is_refused_while_unavailable_then_works_after_reconnect(env, monkeypatch):
    from velune.cli.handlers.model import activate_model

    registry = make_registry(env, monkeypatch)
    await registry.refresh()
    model = registry.get(MODEL, "ollama")
    registry.remember_local(model)
    unplug(env)
    await registry.refresh()

    repl = fake_repl(registry, active=None)
    await activate_model(repl, registry.get(MODEL, "ollama"))
    text = repl.console.export_text()
    assert repl.active_model is None
    assert "unavailable" in text and str(env.ssd) in text and "/model recheck" in text

    replug(env)
    await activate_model(repl, registry.get(MODEL, "ollama"))
    assert repl.active_model is not None and repl.active_model.model_id == MODEL


async def test_prompt_is_kept_when_the_active_model_is_unavailable(env, monkeypatch):
    from velune.cli.repl import VeluneREPL

    registry = make_registry(env, monkeypatch)
    await registry.refresh()
    registry.remember_local(registry.get(MODEL, "ollama"))
    env.fake.stop()
    await registry.refresh()

    model = registry.get(MODEL, "ollama")
    repl = fake_repl(registry, active=model)
    sent = await VeluneREPL._ensure_active_model_usable(repl, model, "explain this function")
    assert sent is False
    assert repl._pending_prompt_text == "explain this function"
    assert "Ollama server is not reachable" in repl.console.export_text()


# ---------------------------------------------------------------------------
# Relocation after the path changed
# ---------------------------------------------------------------------------


async def test_relocate_to_the_new_mount_point(env, monkeypatch):
    from velune.cli.handlers.model_local import cmd_model_relocate

    registry = make_registry(env, monkeypatch)
    await registry.refresh()
    registry.remember_local(registry.get(MODEL, "ollama"))
    moved = env.tmp / "E_drive" / "models"
    moved.parent.mkdir()
    env.ssd.rename(moved)  # the OS mounted the SSD under a different path
    await registry.refresh()

    repl = fake_repl(registry)
    await cmd_model_relocate(repl, f"{MODEL} {moved}")
    record = env.local.get("ollama", MODEL)
    assert record.source_path == str(moved)
    assert record.metadata["previous_source_path"] == str(env.ssd)
    assert is_available(registry.get(MODEL, "ollama"))


async def test_relocate_to_a_store_without_the_model_keeps_the_old_path(env, monkeypatch):
    from velune.cli.handlers.model_local import cmd_model_relocate

    registry = make_registry(env, monkeypatch)
    await registry.refresh()
    registry.remember_local(registry.get(MODEL, "ollama"))
    elsewhere = make_store(env.tmp / "other" / "models", ["llama3:8b"])

    repl = fake_repl(registry)
    await cmd_model_relocate(repl, f"{MODEL} {elsewhere}")
    assert env.local.get("ollama", MODEL).source_path == str(env.ssd)
    assert "not in that store" in repl.console.export_text()


# ---------------------------------------------------------------------------
# Registering a GGUF file through Ollama's import API
# ---------------------------------------------------------------------------


def _gguf_string(s: str) -> bytes:
    raw = s.encode()
    return struct.pack("<Q", len(raw)) + raw


def write_gguf(path: Path, *, kv_count_override: int | None = None, truncate: bool = False) -> Path:
    kvs = [
        _gguf_string("general.architecture") + struct.pack("<I", 8) + _gguf_string("llama"),
        _gguf_string("general.name") + struct.pack("<I", 8) + _gguf_string("Tiny Test"),
        _gguf_string("llama.context_length") + struct.pack("<I", 4) + struct.pack("<I", 4096),
        _gguf_string("general.file_type") + struct.pack("<I", 4) + struct.pack("<I", 15),
        # A string array (like tokenizer.ggml.tokens) that must be skipped, not loaded.
        _gguf_string("tokenizer.ggml.tokens")
        + struct.pack("<I", 9)
        + struct.pack("<I", 8)
        + struct.pack("<Q", 3)
        + b"".join(_gguf_string(t) for t in ("a", "b", "c")),
    ]
    header = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0)
    header += struct.pack("<Q", kv_count_override if kv_count_override is not None else len(kvs))
    body = header + b"".join(kvs) + b"\x00" * 64
    path.write_bytes(body[:40] if truncate else body)
    return path


def test_gguf_metadata_is_read_from_the_header():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        meta = read_gguf_metadata(write_gguf(Path(d) / "tiny.gguf"))
    assert meta == {
        "gguf_version": 3,
        "architecture": "llama",
        "context_length": 4096,
        "name": "Tiny Test",
        "quantization": "Q4_K_M",
    }


def test_path_kinds_are_identified(tmp_path):
    assert inspect_path(tmp_path / "nope").kind is PathKind.MISSING
    store = make_store(tmp_path / "store", [MODEL])
    assert inspect_path(store).kind is PathKind.OLLAMA_STORE
    blob = next((store / "blobs").iterdir())
    blob_info = inspect_path(blob)
    assert blob_info.kind is PathKind.OLLAMA_BLOB and blob_info.suggested_root == store

    assert inspect_path(write_gguf(tmp_path / "m.gguf")).kind is PathKind.GGUF_FILE
    fake = tmp_path / "fake.gguf"
    fake.write_bytes(b"not a model")
    assert inspect_path(fake).kind is PathKind.UNSUPPORTED
    assert inspect_path(write_gguf(tmp_path / "t.gguf", truncate=True)).kind is PathKind.UNSUPPORTED
    assert (
        inspect_path(write_gguf(tmp_path / "k.gguf", kv_count_override=10**9)).kind
        is PathKind.UNSUPPORTED
    )

    pt = tmp_path / "pt"
    pt.mkdir()
    (pt / "config.json").write_text("{}")
    (pt / "pytorch_model.bin").write_bytes(b"x")
    assert inspect_path(pt).kind is PathKind.UNSUPPORTED

    st = tmp_path / "st"
    st.mkdir()
    (st / "config.json").write_text(json.dumps({"architectures": ["LlamaForCausalLM"]}))
    (st / "model.safetensors").write_bytes(b"x" * 10)
    info = inspect_path(st)
    assert (
        info.kind is PathKind.SAFETENSORS_DIR
        and info.metadata["architecture"] == "LlamaForCausalLM"
    )

    folder = tmp_path / "ggufs"
    folder.mkdir()
    write_gguf(folder / "a.gguf")
    write_gguf(folder / "b.gguf")
    assert inspect_path(folder).kind is PathKind.GGUF_DIR


@pytest.fixture
def auto_confirm(monkeypatch):
    import velune.cli.interactive as interactive

    async def yes(*_a, **_k):
        return True

    monkeypatch.setattr(interactive, "confirm", yes)


async def test_register_gguf_imports_verifies_and_survives_restart(env, monkeypatch, auto_confirm):
    from velune.cli.handlers.model_local import cmd_model_register

    gguf = write_gguf(env.tmp / "tiny.gguf")
    registry = make_registry(env, monkeypatch)
    repl = fake_repl(registry)
    await cmd_model_register(repl, f"{gguf} --name tiny:latest")

    assert "tiny:latest" in env.fake.models
    assert any(path.startswith("/api/blobs/sha256:") for _m, path in env.fake.requests)
    record = env.local.get("ollama", "tiny:latest")
    assert record is not None and record.origin == "user"
    assert record.metadata["imported_from"] == str(gguf)
    assert gguf.exists(), "the original file must not be moved"

    restarted = make_registry(env, monkeypatch)
    assert restarted.get("tiny:latest", "ollama") is not None


async def test_failed_import_leaves_the_previous_registration_unchanged(
    env, monkeypatch, auto_confirm
):
    from velune.cli.handlers.model_local import cmd_model_relocate

    registry = make_registry(env, monkeypatch)
    await registry.refresh()
    registry.remember_local(registry.get(MODEL, "ollama"))
    before = env.local.get("ollama", MODEL)

    env.fake.create_status = 500
    env.fake.create_error = "unsupported architecture"
    gguf = write_gguf(env.tmp / "replacement.gguf")
    repl = fake_repl(registry)
    await cmd_model_relocate(repl, f"{MODEL} {gguf}")

    after = env.local.get("ollama", MODEL)
    assert after.source_path == before.source_path and after.source_kind == before.source_kind
    assert "unsupported architecture" in repl.console.export_text()


async def test_declining_the_import_copies_nothing(env, monkeypatch):
    import velune.cli.interactive as interactive
    from velune.cli.handlers.model_local import cmd_model_register

    async def no(*_a, **_k):
        return False

    monkeypatch.setattr(interactive, "confirm", no)
    registry = make_registry(env, monkeypatch)
    await cmd_model_register(fake_repl(registry), str(write_gguf(env.tmp / "tiny.gguf")))
    assert not any(method == "POST" for method, _p in env.fake.requests)


# ---------------------------------------------------------------------------
# The registrations file itself
# ---------------------------------------------------------------------------


def test_unreadable_registrations_file_is_never_overwritten(tmp_path):
    path = tmp_path / "local.json"
    path.write_text("{ not json", encoding="utf-8")
    reg = LocalModelRegistry(path)
    assert reg.load() == []
    with pytest.raises(LocalModelStoreError):
        reg.upsert(LocalModelRecord("ollama", MODEL))
    assert path.read_text(encoding="utf-8") == "{ not json"


def test_registration_round_trips_every_field(tmp_path):
    reg = LocalModelRegistry(tmp_path / "local.json")
    record = LocalModelRecord(
        "ollama",
        MODEL,
        endpoint="http://h:1",
        source_path="E:/models",
        source_kind="ollama_store",
        metadata={"context_length": 32768},
        origin="user",
    )
    reg.upsert(record)
    reg.update_status(
        "ollama", MODEL, available=False, kind=FailureKind.STORAGE_UNAVAILABLE, reason="gone"
    )
    loaded = LocalModelRegistry(tmp_path / "local.json").get("ollama", MODEL)
    assert loaded.endpoint == "http://h:1" and loaded.source_path == "E:/models"
    assert loaded.metadata == {"context_length": 32768}
    assert loaded.last_error == "gone" and loaded.last_error_kind == "storage_unavailable"
    assert "Reconnect the drive" in loaded.next_action()


# ---------------------------------------------------------------------------
# Wrong paths, hostile files, and the cost of recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["nonexistent", "plain_file"])
async def test_relocating_to_a_wrong_path_keeps_the_old_registration(env, monkeypatch, kind):
    from velune.cli.handlers.model_local import cmd_model_relocate

    registry = make_registry(env, monkeypatch)
    await registry.refresh()
    registry.remember_local(registry.get(MODEL, "ollama"))
    before = env.local.get("ollama", MODEL)

    if kind == "nonexistent":
        target = env.tmp / "typo" / "models"
    else:
        target = env.tmp / "notes.txt"
        target.write_text("not a model store", encoding="utf-8")

    repl = fake_repl(registry)
    await cmd_model_relocate(repl, f"{MODEL} {target}")

    after = env.local.get("ollama", MODEL)
    assert after.source_path == before.source_path == str(env.ssd)
    assert after.source_kind == before.source_kind
    assert repl.console.export_text().strip(), "the user must be told why nothing changed"


async def test_recovery_does_not_scan_the_blob_store(env, monkeypatch):
    """Replugging a drive re-checks a few known paths; it never walks the blobs."""
    import os

    registry = make_registry(env, monkeypatch)
    await registry.refresh()
    registry.remember_local(registry.get(MODEL, "ollama"))
    for i in range(300):  # stand-in for a store with thousands of layers
        (env.ssd / "blobs" / f"sha256-{i:064x}").write_bytes(b"x")
    blobs = (env.ssd / "blobs").resolve()

    touched: list[str] = []

    def _watch(real):
        def wrapper(*args, **kwargs):
            target = args[0] if args else kwargs.get("path", ".")
            try:
                if Path(os.fspath(target)).resolve().is_relative_to(blobs):
                    touched.append(os.fspath(target))
            except (OSError, ValueError):
                pass
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(os, "scandir", _watch(os.scandir))
    monkeypatch.setattr(os, "walk", _watch(os.walk))
    real_iterdir, real_rglob = Path.iterdir, Path.rglob
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda self: (touched.append(str(self)) if self == blobs else None) or real_iterdir(self),
    )
    monkeypatch.setattr(
        Path,
        "rglob",
        lambda self, pattern: (
            (touched.append(str(self)) if self == blobs else None) or real_rglob(self, pattern)
        ),
    )

    unplug(env)
    await registry.refresh()
    assert not is_available(registry.get(MODEL, "ollama"))
    replug(env)
    await registry.refresh()
    assert is_available(registry.get(MODEL, "ollama"))

    assert touched == []
    list(blobs.iterdir())  # the watcher is live: a real scan of the blobs would have been seen
    assert touched


def test_a_header_that_claims_more_strings_than_the_file_holds_is_rejected_at_once(tmp_path):
    import time

    def s(x: str) -> bytes:
        raw = x.encode()
        return struct.pack("<Q", len(raw)) + raw

    evil = tmp_path / "evil.gguf"
    evil.write_bytes(
        b"GGUF"
        + struct.pack("<I", 3)
        + struct.pack("<Q", 0)
        + struct.pack("<Q", 1)
        + s("tokenizer.ggml.tokens")
        + struct.pack("<I", 9)
        + struct.pack("<I", 8)
        + struct.pack("<Q", 2**40)
        + b"\x00" * 1024
    )
    started = time.perf_counter()
    assert inspect_path(evil).kind is PathKind.UNSUPPORTED
    assert time.perf_counter() - started < 1.0


def test_inspecting_a_slow_header_stops_at_its_time_budget(tmp_path, monkeypatch):
    from velune.providers import local_import

    def s(x: str) -> bytes:
        raw = x.encode()
        return struct.pack("<Q", len(raw)) + raw

    slow = tmp_path / "slow.gguf"
    count = 200_000  # fits in the file, so only the time budget can stop it
    slow.write_bytes(
        b"GGUF"
        + struct.pack("<I", 3)
        + struct.pack("<Q", 0)
        + struct.pack("<Q", 1)
        + s("tokenizer.ggml.tokens")
        + struct.pack("<I", 9)
        + struct.pack("<I", 8)
        + struct.pack("<Q", count)
        + b"\x00" * (8 * count)
    )
    monkeypatch.setattr(local_import, "_GGUF_TIME_BUDGET_SECONDS", -1.0)
    with pytest.raises(ValueError, match="too long"):
        read_gguf_metadata(slow)
    assert inspect_path(slow).kind is PathKind.UNSUPPORTED


def test_empty_and_directory_gguf_names_are_unsupported_not_crashes(tmp_path):
    empty = tmp_path / "empty.gguf"
    empty.write_bytes(b"")
    assert inspect_path(empty).kind is PathKind.UNSUPPORTED

    (tmp_path / "dir.gguf").mkdir()  # a folder named like a model file
    assert inspect_path(tmp_path / "dir.gguf").kind is not PathKind.GGUF_FILE

    assert inspect_path(tmp_path / "model.onnx").kind is PathKind.MISSING
    onnx = tmp_path / "model.onnx"
    onnx.write_bytes(b"\x08\x07")
    assert inspect_path(onnx).kind is PathKind.UNSUPPORTED


def test_hashing_a_large_model_file_does_not_hold_it_in_memory(tmp_path):
    import tracemalloc

    from velune.providers.local_import import _sha256

    big = tmp_path / "big.gguf"
    with open(big, "wb") as fh:
        fh.write(b"GGUF")
        for _ in range(48):  # 48 MiB, written in pieces so the test itself stays small
            fh.write(b"\x00" * (1024 * 1024))

    tracemalloc.start()
    try:
        digest = _sha256(big)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(digest) == 64
    assert peak < 16 * 1024 * 1024, f"hashing peaked at {peak / 1e6:.1f} MB for a 48 MB file"
