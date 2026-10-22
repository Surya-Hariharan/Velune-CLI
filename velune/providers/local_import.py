"""Identify a local model path and register or import it the supported way.

Formats are not interchangeable, and this module never pretends they are:

* **Ollama store** (a directory with ``manifests/`` and ``blobs/``) — Ollama's
  own layout. Registered *in place* as a model location; nothing is copied.
  Its models are served by Ollama only if the daemon runs with
  ``OLLAMA_MODELS`` pointing at it.
* **Ollama blob** (``blobs/sha256-…``) — one content-addressed layer of a
  model, not a model. Rejected, with the store root to register instead.
* **GGUF file** — a single-file model. Imported into Ollama through its blob
  and ``/api/create`` API. That *copies* the file into the Ollama server's
  store, so the size is reported and confirmation is required first.
* **Safetensors directory** — ``config.json`` plus ``*.safetensors`` weights.
  Ollama can convert some architectures on import (again a copy, plus
  conversion); the server decides, and its error is shown if it can't.
* **Anything else** (PyTorch ``.bin`` checkpoints, arbitrary files) is reported
  as unsupported with the reason.

Inspection is bounded: one directory listing (no recursion), a few bytes of
magic, a capped header read for GGUF metadata, and a capped ``config.json``.
Capabilities are never guessed from a file name; what the metadata does not
say is reported as unknown.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velune._compat import StrEnum

GGUF_MAGIC = b"GGUF"
_MAX_DIR_ENTRIES = 2000
_MAX_GGUF_HEADER_BYTES = 64 * 1024 * 1024
_MAX_GGUF_KV = 4096
_MAX_CONFIG_BYTES = 1024 * 1024
_HASH_CHUNK = 4 * 1024 * 1024

# Files Ollama needs next to safetensors weights to build a model.
_SAFETENSORS_SIDECARS = (
    "config.json",
    "generation_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "tokenizer.model",
    "special_tokens_map.json",
    "vocab.json",
    "merges.txt",
)


class PathKind(StrEnum):
    MISSING = "missing"
    NOT_ACCESSIBLE = "not_accessible"
    OLLAMA_STORE = "ollama_store"
    OLLAMA_BLOB = "ollama_blob"
    GGUF_FILE = "gguf_file"
    GGUF_DIR = "gguf_dir"
    SAFETENSORS_DIR = "safetensors_dir"
    UNSUPPORTED = "unsupported"


@dataclass
class PathInspection:
    path: Path
    kind: PathKind
    summary: str
    #: Files that would be imported (GGUF/safetensors), with their sizes.
    files: list[Path] = field(default_factory=list)
    total_bytes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Names of models found in an Ollama store.
    store_models: list[str] = field(default_factory=list)
    #: For GGUF_DIR: the candidate files to choose from.
    candidates: list[Path] = field(default_factory=list)
    #: For OLLAMA_BLOB: the store root to register instead.
    suggested_root: Path | None = None

    @property
    def importable(self) -> bool:
        return self.kind in (PathKind.GGUF_FILE, PathKind.SAFETENSORS_DIR)


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{n} B"


# ---------------------------------------------------------------------------
# GGUF header
# ---------------------------------------------------------------------------

_GGUF_SCALARS = {
    0: "<B",
    1: "<b",
    2: "<H",
    3: "<h",
    4: "<I",
    5: "<i",
    6: "<f",
    7: "<?",
    10: "<Q",
    11: "<q",
    12: "<d",
}
_GGUF_STRING, _GGUF_ARRAY = 8, 9

# GGUF general.file_type -> quantization label (llama.cpp's LLAMA_FTYPE enum).
_GGUF_FILE_TYPES = {
    0: "F32",
    1: "F16",
    2: "Q4_0",
    3: "Q4_1",
    7: "Q8_0",
    8: "Q5_0",
    9: "Q5_1",
    10: "Q2_K",
    11: "Q3_K_S",
    12: "Q3_K_M",
    13: "Q3_K_L",
    14: "Q4_K_S",
    15: "Q4_K_M",
    16: "Q5_K_S",
    17: "Q5_K_M",
    18: "Q6_K",
    32: "BF16",
}


class _Reader:
    """Sequential reader over a file that refuses to read past a byte budget."""

    def __init__(self, fh, budget: int) -> None:
        self._fh = fh
        self._budget = budget

    def read(self, n: int) -> bytes:
        if n < 0 or self._fh.tell() + n > self._budget:
            raise ValueError("GGUF header exceeds the inspection budget")
        data = self._fh.read(n)
        if len(data) != n:
            raise ValueError("GGUF header is truncated")
        return data

    def skip(self, n: int) -> None:
        if n < 0 or self._fh.tell() + n > self._budget:
            raise ValueError("GGUF header exceeds the inspection budget")
        self._fh.seek(n, os.SEEK_CUR)

    def unpack(self, fmt: str):
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))[0]


def _read_string(r: _Reader, length_fmt: str) -> str:
    return r.read(r.unpack(length_fmt)).decode("utf-8", errors="replace")


def _read_value(r: _Reader, vtype: int, length_fmt: str, *, keep: bool):
    if vtype in _GGUF_SCALARS:
        return r.unpack(_GGUF_SCALARS[vtype])
    if vtype == _GGUF_STRING:
        if keep:
            return _read_string(r, length_fmt)
        r.skip(r.unpack(length_fmt))
        return None
    if vtype == _GGUF_ARRAY:
        item_type = r.unpack("<I")
        count = r.unpack(length_fmt)
        if item_type in _GGUF_SCALARS:
            r.skip(struct.calcsize(_GGUF_SCALARS[item_type]) * count)
        elif item_type == _GGUF_STRING:
            for _ in range(count):
                r.skip(r.unpack(length_fmt))
        else:
            raise ValueError(f"unsupported GGUF array type {item_type}")
        return None
    raise ValueError(f"unknown GGUF value type {vtype}")


def read_gguf_metadata(path: Path, *, budget: int = _MAX_GGUF_HEADER_BYTES) -> dict[str, Any]:
    """Read the identifying keys from a GGUF header without loading the model.

    Returns a dict that may contain ``architecture``, ``name``, ``context_length``,
    ``quantization``, ``size_label``, ``parameter_count`` and ``gguf_version``.
    Raises ``ValueError`` for a file that is not valid GGUF.
    """
    wanted_suffixes = ("context_length",)
    out: dict[str, Any] = {}
    with open(path, "rb") as fh:
        r = _Reader(fh, budget)
        if r.read(4) != GGUF_MAGIC:
            raise ValueError("not a GGUF file (bad magic)")
        version = r.unpack("<I")
        if version not in (1, 2, 3):
            raise ValueError(f"unsupported GGUF version {version}")
        count_fmt = "<I" if version == 1 else "<Q"
        r.unpack(count_fmt)  # tensor count
        kv_count = r.unpack(count_fmt)
        if kv_count > _MAX_GGUF_KV:
            raise ValueError("GGUF header has an implausible number of keys")
        raw: dict[str, Any] = {}
        for _ in range(kv_count):
            key = _read_string(r, count_fmt)
            vtype = r.unpack("<I")
            keep = key.startswith("general.") or key.endswith(wanted_suffixes)
            value = _read_value(r, vtype, count_fmt, keep=keep)
            if keep and value is not None:
                raw[key] = value
    out["gguf_version"] = version
    arch = raw.get("general.architecture")
    if isinstance(arch, str):
        out["architecture"] = arch
        ctx = raw.get(f"{arch}.context_length")
        if isinstance(ctx, int) and ctx > 0:
            out["context_length"] = ctx
    if isinstance(raw.get("general.name"), str):
        out["name"] = raw["general.name"]
    if isinstance(raw.get("general.size_label"), str):
        out["size_label"] = raw["general.size_label"]
    if isinstance(raw.get("general.parameter_count"), int):
        out["parameter_count"] = raw["general.parameter_count"]
    file_type = raw.get("general.file_type")
    if isinstance(file_type, int):
        out["quantization"] = _GGUF_FILE_TYPES.get(file_type, f"type {file_type}")
    return out


def _has_gguf_magic(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == GGUF_MAGIC
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def _list_dir(path: Path) -> list[os.DirEntry]:
    entries: list[os.DirEntry] = []
    with os.scandir(path) as it:
        for entry in it:
            entries.append(entry)
            if len(entries) >= _MAX_DIR_ENTRIES:
                break
    return entries


def _size(entry_or_path) -> int:
    try:
        st = entry_or_path.stat() if hasattr(entry_or_path, "stat") else os.stat(entry_or_path)
        return int(st.st_size)
    except OSError:
        return 0


def inspect_path(raw: str | os.PathLike[str]) -> PathInspection:
    """Classify *raw* as one of :class:`PathKind`. Bounded; never loads a model."""
    from velune.providers.ollama_store import OllamaModelStore

    path = Path(raw).expanduser()
    try:
        exists = path.exists()
    except OSError:
        exists = False
    if not exists:
        return PathInspection(
            path, PathKind.MISSING, f"Path does not exist or is not mounted: {path}"
        )
    if not os.access(path, os.R_OK):
        return PathInspection(path, PathKind.NOT_ACCESSIBLE, f"Path is not readable: {path}")

    if path.is_file():
        if path.parent.name == "blobs" and path.name.startswith("sha256"):
            root = path.parent.parent
            return PathInspection(
                path,
                PathKind.OLLAMA_BLOB,
                "This is one content-addressed layer inside an Ollama model store, not a "
                f"standalone model. Register the store instead: {root}",
                suggested_root=root if OllamaModelStore.is_valid_root(root) else None,
            )
        if _has_gguf_magic(path):
            try:
                meta = read_gguf_metadata(path)
            except (OSError, ValueError, struct.error) as exc:
                return PathInspection(
                    path, PathKind.UNSUPPORTED, f"GGUF file with an unreadable header: {exc}"
                )
            size = _size(path)
            details = ", ".join(
                str(v)
                for v in (
                    meta.get("architecture"),
                    meta.get("size_label"),
                    meta.get("quantization"),
                )
                if v
            )
            return PathInspection(
                path,
                PathKind.GGUF_FILE,
                f"GGUF model file ({details or 'architecture not recorded'}), {human_size(size)}",
                files=[path],
                total_bytes=size,
                metadata=meta,
            )
        if path.suffix.lower() == ".gguf":
            return PathInspection(
                path, PathKind.UNSUPPORTED, "File has a .gguf name but is not GGUF."
            )
        return PathInspection(
            path,
            PathKind.UNSUPPORTED,
            f"Unsupported file type ({path.suffix or 'no extension'}). Supported: a .gguf "
            "file, a safetensors model folder, or an Ollama model store.",
        )

    if OllamaModelStore.is_valid_root(path):
        try:
            names = [m.name for m in OllamaModelStore(path).list_models()]
        except OSError as exc:
            return PathInspection(path, PathKind.NOT_ACCESSIBLE, f"Could not read the store: {exc}")
        return PathInspection(
            path,
            PathKind.OLLAMA_STORE,
            f"Ollama model store with {len(names)} model(s)",
            store_models=names,
        )

    try:
        entries = _list_dir(path)
    except OSError as exc:
        return PathInspection(path, PathKind.NOT_ACCESSIBLE, f"Could not list the folder: {exc}")
    files = {e.name: e for e in entries if e.is_file()}
    ggufs = sorted(
        (Path(e.path) for name, e in files.items() if name.lower().endswith(".gguf")),
        key=lambda p: p.name,
    )
    safetensors = sorted(
        (Path(e.path) for name, e in files.items() if name.lower().endswith(".safetensors")),
        key=lambda p: p.name,
    )

    if safetensors and "config.json" in files:
        meta: dict[str, Any] = {}
        try:
            config_path = path / "config.json"
            if _size(config_path) <= _MAX_CONFIG_BYTES:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                archs = config.get("architectures") or []
                meta = {
                    "architecture": archs[0] if archs else config.get("model_type"),
                    "model_type": config.get("model_type"),
                    "context_length": config.get("max_position_embeddings"),
                }
        except (OSError, ValueError):
            meta = {}
        sidecars = [path / n for n in _SAFETENSORS_SIDECARS if n in files]
        import_files = [*safetensors, *sidecars]
        total = sum(_size(p) for p in import_files)
        return PathInspection(
            path,
            PathKind.SAFETENSORS_DIR,
            f"Safetensors model folder ({meta.get('architecture') or 'architecture unknown'}, "
            f"{len(safetensors)} weight file(s)), {human_size(total)}",
            files=import_files,
            total_bytes=total,
            metadata=meta,
        )
    if ggufs:
        return PathInspection(
            path,
            PathKind.GGUF_DIR,
            f"Folder with {len(ggufs)} GGUF file(s) — choose one",
            candidates=ggufs,
        )
    if "config.json" in files and any(n.endswith((".bin", ".pt", ".pth")) for n in files):
        return PathInspection(
            path,
            PathKind.UNSUPPORTED,
            "PyTorch checkpoint folder (.bin/.pt weights). Velune can import GGUF or "
            "safetensors; convert this model to one of those first.",
        )
    return PathInspection(
        path,
        PathKind.UNSUPPORTED,
        "No supported model found here. Choose a .gguf file, a folder with config.json "
        "and .safetensors weights, or an Ollama store (with manifests/ and blobs/).",
    )


def suggest_model_name(inspection: PathInspection) -> str:
    """An Ollama-valid model name derived from metadata or the file/folder name."""
    base = inspection.metadata.get("name") or (
        inspection.path.stem if inspection.path.is_file() else inspection.path.name
    )
    name = re.sub(r"[^a-z0-9._-]+", "-", str(base).lower()).strip("-.") or "imported-model"
    return f"{name[:60]}:latest"


# ---------------------------------------------------------------------------
# Import into Ollama
# ---------------------------------------------------------------------------


class ModelImportError(Exception):
    """The import could not be completed; nothing was registered."""


@dataclass
class ImportResult:
    model_name: str
    listed: bool
    #: True/False if a 1-token generation was attempted; None if skipped.
    inference_ok: bool | None
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _sha256(path: Path, progress: Callable[[str], None] | None = None) -> str:
    digest = hashlib.sha256()
    total = _size(path)
    done = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            digest.update(chunk)
            done += len(chunk)
            if progress and total:
                progress(f"Checksumming {path.name}: {done * 100 // total}%")
    return digest.hexdigest()


async def _upload_blob(client, base: str, path: Path, digest: str, progress) -> None:
    head = await client.head(f"{base}/api/blobs/sha256:{digest}")
    if head.status_code == 200:
        return  # the server already has this exact content
    total = _size(path)

    async def _chunks():
        sent = 0
        with open(path, "rb") as fh:
            while True:
                chunk = await asyncio.to_thread(fh.read, _HASH_CHUNK)
                if not chunk:
                    break
                sent += len(chunk)
                if progress and total:
                    progress(f"Uploading {path.name} to Ollama: {sent * 100 // total}%")
                yield chunk

    resp = await client.post(f"{base}/api/blobs/sha256:{digest}", content=_chunks())
    if resp.status_code not in (200, 201):
        raise ModelImportError(
            f"Ollama refused the upload of {path.name} (HTTP {resp.status_code})."
        )


async def import_into_ollama(
    inspection: PathInspection,
    model_name: str,
    *,
    progress: Callable[[str], None] | None = None,
    test_inference: bool = True,
    inference_timeout: float = 120.0,
) -> ImportResult:
    """Copy *inspection*'s files into the Ollama server and create *model_name*.

    Uses the documented API: ``POST /api/blobs/sha256:<digest>`` per file, then
    ``POST /api/create`` with a ``files`` map. Afterwards the model must appear
    in ``/api/tags``; when *test_inference* is set, a 1-token generation checks
    it actually loads. Raises :class:`ModelImportError` when the import fails.
    """
    import httpx

    from velune.providers.endpoints import ollama_base_url

    if not inspection.importable:
        raise ModelImportError(f"{inspection.path} is not an importable model ({inspection.kind}).")
    base = ollama_base_url()

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None, write=None)) as client:
        try:
            (await client.get(f"{base}/api/version")).raise_for_status()
        except httpx.HTTPError as exc:
            raise ModelImportError(
                f"Ollama server is not reachable at {base} ({type(exc).__name__}). "
                "Start it with `ollama serve` and try again."
            ) from exc

        files_map: dict[str, str] = {}
        for path in inspection.files:
            digest = await asyncio.to_thread(_sha256, path, progress)
            await _upload_blob(client, base, path, digest, progress)
            files_map[path.name] = f"sha256:{digest}"

        if progress:
            progress(f"Creating {model_name} in Ollama…")
        resp = await client.post(
            f"{base}/api/create",
            json={"model": model_name, "files": files_map, "stream": False},
        )
        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", "")
            except ValueError:
                detail = resp.text[:200]
            raise ModelImportError(
                f"Ollama could not create {model_name} (HTTP {resp.status_code}): {detail}"
                if detail
                else f"Ollama could not create {model_name} (HTTP {resp.status_code})."
            )

        tags = (await client.get(f"{base}/api/tags")).json()
        names = {m.get("name") for m in tags.get("models", []) if isinstance(m, dict)}
        listed = model_name in names or f"{model_name}:latest" in names
        if not listed:
            raise ModelImportError(f"Ollama reported success but does not list {model_name}.")

        details: dict[str, Any] = {}
        try:
            show = (await client.post(f"{base}/api/show", json={"model": model_name})).json()
            details = {
                "capabilities": show.get("capabilities"),
                "details": show.get("details"),
            }
        except (httpx.HTTPError, ValueError):
            pass

        inference_ok: bool | None = None
        if test_inference:
            if progress:
                progress(f"Loading {model_name} for a test generation…")
            try:
                gen = await client.post(
                    f"{base}/api/generate",
                    json={
                        "model": model_name,
                        "prompt": "Reply with OK.",
                        "stream": False,
                        "options": {"num_predict": 1},
                    },
                    timeout=inference_timeout,
                )
                inference_ok = gen.status_code == 200
            except httpx.HTTPError:
                inference_ok = False

    message = f"Imported {model_name} into Ollama at {base}."
    if inference_ok is False:
        message += " It is listed, but a test generation did not succeed (it may need more memory)."
    return ImportResult(model_name, listed, inference_ok, message, details)


def capabilities_from_ollama(details: dict[str, Any]):
    """A capability profile from Ollama's own ``/api/show`` capability list.

    Only what the server reports is set; everything else stays NONE (unknown).
    """
    from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile

    profile = ModelCapabilityProfile()
    reported = set(details.get("capabilities") or [])
    if "tools" in reported:
        profile.tool_use = CapabilityLevel.INTERMEDIATE
    if "vision" in reported:
        profile.vision = CapabilityLevel.ADVANCED
        profile.multimodal = CapabilityLevel.ADVANCED
    if "embedding" in reported:
        profile.embedding = CapabilityLevel.ADVANCED
    return profile
