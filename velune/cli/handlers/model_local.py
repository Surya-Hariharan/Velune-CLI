"""``/model register``, ``/model recheck`` and ``/model relocate``.

Local models live on drives and behind servers that come and go. These
commands are the recovery surface for that: register a model from a path,
re-test one that is unavailable (drive reconnected, daemon restarted), and
point a registration at a new location after a drive was remounted elsewhere.
Velune never guesses a new location and never copies model files without an
explicit confirmation that states the size.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import TYPE_CHECKING

from velune.cli import design

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL
    from velune.core.types.model import ModelDescriptor

_log = logging.getLogger("velune.cli.handlers.model_local")

REGISTER_USAGE = (
    "/model register <path> [--name <model-name>]  — a .gguf file, a safetensors "
    "model folder, or an Ollama model store"
)
RELOCATE_USAGE = "/model relocate <model-id> <new-path>"


# ---------------------------------------------------------------------------
# Shared rendering
# ---------------------------------------------------------------------------


def availability_lines(model: ModelDescriptor) -> list[str]:
    """Rich-markup lines describing why *model* is unavailable and what to do."""
    from velune.models.local_models import NEXT_ACTIONS, FailureKind
    from velune.models.registry import unavailable_reason

    reason = unavailable_reason(model)
    if reason is None:
        return []
    try:
        kind = FailureKind(model.metadata.get("unavailable_kind") or FailureKind.UNKNOWN)
    except ValueError:
        kind = FailureKind.UNKNOWN
    return [
        f"[{design.WARN}]{reason}[/{design.WARN}]",
        f"[{design.FAINT}]→ {NEXT_ACTIONS[kind]}[/{design.FAINT}]",
    ]


def print_unavailable(repl: VeluneREPL, model: ModelDescriptor, *, headline: str) -> None:
    repl.console.print(f"[{design.WARN}]{headline}[/{design.WARN}]")
    for line in availability_lines(model):
        repl.console.print(line)


def _registry(repl: VeluneREPL):
    return repl._require("runtime.model_registry", "model registry")


def _find_model(registry, model_id: str):
    model = registry.get(model_id)
    if model is None and "/" in model_id:
        provider, _, bare = model_id.partition("/")
        model = registry.get(bare, provider)
    return model


# ---------------------------------------------------------------------------
# /model recheck
# ---------------------------------------------------------------------------


async def cmd_model_recheck(repl: VeluneREPL, args: str) -> None:
    """Re-test a model now: a local model's server/storage, or a cloud model's key."""
    from velune.cli.interactive import run_with_status
    from velune.models.registry import is_available

    registry = _registry(repl)
    if registry is None:
        return
    target = args.strip()
    model = _find_model(registry, target) if target else repl.active_model
    if model is None:
        repl.console.print(
            f"[{design.WARN}]Usage: /model recheck [model-id][/{design.WARN}] "
            f"[{design.FAINT}](defaults to the active model)[/{design.FAINT}]"
        )
        return

    if not model.is_local:
        await _recheck_cloud(repl, model.provider_id)
        return

    updated = await run_with_status(
        registry.recheck(model, force=True),
        pending=f"Checking {model.model_id}…",
        ok=lambda m: f"{m.model_id} is available",
        fail=lambda m: f"{m.model_id} is still unavailable",
        is_ok=is_available,
    )
    if repl.active_model is not None and (
        repl.active_model.provider_id,
        repl.active_model.model_id,
    ) == (updated.provider_id, updated.model_id):
        repl.active_model = updated
    if is_available(updated):
        repl.console.print(
            f"[{design.OK}]{updated.model_id} is reachable again — no reconfiguration needed.[/{design.OK}]"
        )
    else:
        for line in availability_lines(updated):
            repl.console.print(line)


async def _recheck_cloud(repl: VeluneREPL, provider_id: str) -> None:
    from velune.cli.interactive import run_with_status
    from velune.providers.keystore import key_source
    from velune.providers.verifier import reverify

    source = key_source(provider_id)
    if source.source == "none":
        repl.console.print(
            f"[{design.WARN}]No credential for {provider_id}.[/{design.WARN}] "
            f"[{design.FAINT}]→ /connect {provider_id}[/{design.FAINT}]"
        )
        return
    result = await run_with_status(
        reverify(provider_id),
        pending=f"Re-verifying the {provider_id} credential ({source.describe()})…",
        ok=lambda r: "Credential verified",
        fail=lambda r: r.message,
        is_ok=lambda r: r.ok,
    )
    if not result.ok and not result.rejects_key:
        repl.console.print(
            f"[{design.FAINT}]The saved key was kept; this check was inconclusive and "
            f"will be retried automatically.[/{design.FAINT}]"
        )


# ---------------------------------------------------------------------------
# /model register
# ---------------------------------------------------------------------------


def _parse_path_and_name(args: str) -> tuple[str, str | None]:
    """Split ``<path> [--name X]``; tolerant of unquoted Windows paths with spaces."""
    name = None
    text = args.strip()
    marker = text.find("--name")
    if marker != -1:
        name = text[marker + len("--name") :].strip().strip("\"'") or None
        text = text[:marker].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text, name


async def cmd_model_register(repl: VeluneREPL, args: str) -> None:
    """Identify a local model path and register or import it."""
    from velune.providers.local_import import PathKind, inspect_path

    raw_path, name = _parse_path_and_name(args)
    if not raw_path:
        repl.console.print(f"[{design.WARN}]Usage: {REGISTER_USAGE}[/{design.WARN}]")
        return

    inspection = await asyncio.to_thread(inspect_path, raw_path)
    repl.console.print(f"[{design.FAINT}]Detected:[/{design.FAINT}] {inspection.summary}")

    if inspection.kind is PathKind.OLLAMA_STORE:
        await _register_store(repl, inspection)
    elif inspection.kind is PathKind.OLLAMA_BLOB:
        if inspection.suggested_root is not None:
            from velune.cli.interactive import confirm

            if (
                await confirm(
                    f"Register the Ollama store at {inspection.suggested_root} instead?",
                    default=True,
                )
                is True
            ):
                await _register_store(
                    repl, await asyncio.to_thread(inspect_path, inspection.suggested_root)
                )
    elif inspection.kind is PathKind.GGUF_DIR:
        chosen = await _choose_gguf(inspection)
        if chosen is not None:
            await _import_flow(repl, await asyncio.to_thread(inspect_path, chosen), name)
    elif inspection.importable:
        await _import_flow(repl, inspection, name)
    else:
        repl.console.print(
            f"[{design.FAINT}]Nothing was registered. {REGISTER_USAGE}[/{design.FAINT}]"
        )


async def _choose_gguf(inspection):
    from velune.cli.interactive import BACK, CANCEL, Option, single_select
    from velune.providers.local_import import human_size

    options = [
        Option(str(p), p.name, human_size(p.stat().st_size) if p.exists() else "")
        for p in inspection.candidates
    ]
    picked = await single_select(
        "Choose a GGUF file", options, filterable=True, palette=True, frame_title="Register model"
    )
    return None if picked in (BACK, CANCEL) else str(picked)


async def _register_store(repl: VeluneREPL, inspection) -> None:
    """Register an Ollama store in place (nothing is copied) and show its models."""
    from velune.models.registry import is_available
    from velune.providers.ollama_locations import OllamaLocationRegistry

    result = OllamaLocationRegistry().add(inspection.path)
    style = design.OK if result.ok else design.DANGER
    repl.console.print(f"[{style}]{result.message}[/{style}]")
    if not result.ok:
        return
    registry = _registry(repl)
    if registry is None:
        return
    await registry.refresh()
    names = set(inspection.store_models)
    found = [m for m in registry.list_all() if m.provider_id == "ollama" and m.model_id in names]
    served = [m for m in found if is_available(m)]
    if served:
        repl.console.print(
            f"[{design.FAINT}]Ready to use: {', '.join(m.model_id for m in served[:8])}"
            f"{' …' if len(served) > 8 else ''}  → /model to switch[/{design.FAINT}]"
        )
    unserved = [m for m in found if not is_available(m)]
    if unserved:
        print_unavailable(
            repl,
            unserved[0],
            headline=f"{len(unserved)} model(s) here are found on disk but not being served.",
        )


async def _import_flow(repl: VeluneREPL, inspection, name: str | None, *, replaces=None):
    """Confirm, import into Ollama, verify, then persist the registration.

    *replaces* is an existing registration being relocated; it is only updated
    once the import has succeeded, so a failed import leaves it untouched.
    Returns the model name on success, else None.
    """
    from velune.cli.interactive import confirm, run_with_status
    from velune.models.local_models import Availability, LocalModelRecord
    from velune.providers.endpoints import ollama_base_url
    from velune.providers.local_import import (
        ModelImportError,
        PathKind,
        capabilities_from_ollama,
        human_size,
        import_into_ollama,
        suggest_model_name,
    )

    registry = _registry(repl)
    if registry is None:
        return None
    model_name = name or (replaces.model_id if replaces else suggest_model_name(inspection))
    base = ollama_base_url()
    what = "convert and copy" if inspection.kind is PathKind.SAFETENSORS_DIR else "copy"
    repl.console.print(
        f"[{design.FAINT}]Importing will {what} {human_size(inspection.total_bytes)} into the "
        f"model store of the Ollama server at {base}. Your original files are not moved or "
        f"changed; make sure that drive has the space.[/{design.FAINT}]"
    )
    answer = await confirm(f"Import as {model_name}?", default=False)
    if answer is not True:
        repl.console.print(
            f"[{design.FAINT}]Cancelled — nothing was copied or registered.[/{design.FAINT}]"
        )
        return None

    try:
        result = await run_with_status(
            import_into_ollama(inspection, model_name),
            pending=f"Importing {inspection.path.name} into Ollama (large files take a while)…",
            ok=lambda r: r.message,
            fail=lambda r: r.message,
            is_ok=lambda r: r.listed,
        )
    except ModelImportError as exc:
        repl.console.print(f"[{design.DANGER}]Import failed:[/{design.DANGER}] {exc}")
        repl.console.print(
            f"[{design.FAINT}]Nothing was registered; any previous configuration is unchanged.[/{design.FAINT}]"
        )
        return None

    meta = inspection.metadata
    details = (result.metadata or {}).get("details") or {}
    record = LocalModelRecord(
        provider_id="ollama",
        model_id=result.model_name,
        endpoint=base,
        source_path=None,  # the model now lives in Ollama's own store
        source_kind=str(inspection.kind),
        metadata={
            k: v
            for k, v in {
                "display_name": result.model_name,
                "context_length": meta.get("context_length"),
                "family": details.get("family") or meta.get("architecture"),
                "quantization": details.get("quantization_level") or meta.get("quantization"),
                "imported_from": str(inspection.path),
            }.items()
            if v
        },
        status=Availability.AVAILABLE,
        origin="user",
    )
    if replaces is not None:
        record.metadata["previous_source_path"] = replaces.source_path
    try:
        registry.local_models.upsert(record)
    except OSError as exc:
        repl.console.print(
            f"[{design.WARN}]Imported, but the registration could not be saved: {exc}[/{design.WARN}]"
        )
    await registry.refresh()

    caps = (result.metadata or {}).get("capabilities")
    model = registry.get(result.model_name, "ollama")
    if model is not None and caps:
        model.capabilities = capabilities_from_ollama(result.metadata)
    repl.console.print(
        f"[{design.OK}]Registered {result.model_name}.[/{design.OK}] "
        f"[{design.FAINT}]Capabilities: "
        f"{', '.join(caps) if caps else 'not reported by Ollama — shown as unknown'}[/{design.FAINT}]"
    )
    if model is not None and replaces is None:
        if await confirm(f"Switch to {result.model_name} now?", default=True) is True:
            from velune.cli.handlers.model import activate_model

            await activate_model(repl, model)
    return result.model_name


# ---------------------------------------------------------------------------
# /model relocate
# ---------------------------------------------------------------------------


def _split_model_and_path(args: str) -> tuple[str, str]:
    try:
        parts = shlex.split(args, posix=False)
    except ValueError:
        parts = args.split()
    if len(parts) < 2:
        return (parts[0] if parts else ""), ""
    model_id = parts[0]
    path = args.strip()[len(model_id) :].strip().strip("\"'")
    return model_id, path


async def cmd_model_relocate(repl: VeluneREPL, args: str) -> None:
    """Point a registered local model at a new storage location.

    The previous configuration is kept until the new location has been
    validated (and, for files, imported) successfully.
    """
    from velune.models.local_models import record_from_descriptor
    from velune.providers.local_import import PathKind, inspect_path
    from velune.providers.ollama_locations import OllamaLocationRegistry

    model_id, raw_path = _split_model_and_path(args)
    if not model_id or not raw_path:
        repl.console.print(f"[{design.WARN}]Usage: {RELOCATE_USAGE}[/{design.WARN}]")
        return
    registry = _registry(repl)
    if registry is None:
        return
    model = _find_model(registry, model_id)
    if model is None or not model.is_local:
        repl.console.print(
            f"[{design.WARN}]{model_id} is not a registered local model.[/{design.WARN}] "
            f"[{design.FAINT}]Use /model register <path> to add one.[/{design.FAINT}]"
        )
        return
    record = registry.registration(model.provider_id, model.model_id) or record_from_descriptor(
        model
    )

    inspection = await asyncio.to_thread(inspect_path, raw_path)
    repl.console.print(f"[{design.FAINT}]Detected:[/{design.FAINT}] {inspection.summary}")

    if inspection.kind is PathKind.OLLAMA_STORE:
        if model.model_id not in inspection.store_models:
            listed = ", ".join(inspection.store_models[:6]) or "no models"
            repl.console.print(
                f"[{design.WARN}]{model.model_id} is not in that store (it has: {listed}).[/{design.WARN}] "
                f"[{design.FAINT}]The previous location was kept.[/{design.FAINT}]"
            )
            return
        added = OllamaLocationRegistry().add(inspection.path)
        if not added.ok:
            repl.console.print(f"[{design.DANGER}]{added.message}[/{design.DANGER}]")
            return
        previous = record.source_path
        record.source_path = str(inspection.path)
        record.source_kind = "ollama_store"
        if previous and previous != record.source_path:
            record.metadata["previous_source_path"] = previous
        record.origin = "user"
        try:
            registry.local_models.upsert(record)
        except OSError as exc:
            repl.console.print(
                f"[{design.DANGER}]Could not save the new location: {exc}[/{design.DANGER}]"
            )
            return
        repl.console.print(
            f"[{design.OK}]{model.model_id} now points at {inspection.path}.[/{design.OK}]"
        )
        await registry.refresh()
        refreshed = registry.get(model.model_id, model.provider_id) or model
        await cmd_model_recheck(repl, f"{refreshed.provider_id}/{refreshed.model_id}")
        return

    if inspection.kind is PathKind.GGUF_DIR:
        chosen = await _choose_gguf(inspection)
        if chosen is None:
            return
        inspection = await asyncio.to_thread(inspect_path, chosen)
    if inspection.importable:
        await _import_flow(repl, inspection, model.model_id, replaces=record)
        return
    repl.console.print(
        f"[{design.FAINT}]The previous configuration was kept. {RELOCATE_USAGE}[/{design.FAINT}]"
    )
