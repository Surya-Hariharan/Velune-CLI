"""The Velune REPL's provider connection flow — the ``/connect`` command.

This module used to back ``/providers`` as well, hosting a management menu
(manage / test / discover / remove / status) on top of the same connect flow.
``/providers`` was removed as a REPL command because it duplicated
``/connect``'s purpose, and those management screens went with it. The
capabilities themselves were *not* dropped: ``velune provider list|test|
remove|models|status`` in ``cli/commands/providers.py`` drives the identical
subsystem calls (``verifier.reverify``, ``keystore.delete_key``,
``ModelDiscoveryScanner``) from the non-REPL CLI.

What remains here is exactly one path: pick a provider, enter a key, verify it,
persist it, and discover its models.

Every screen here is built from the shared widget kit in
``velune.cli.interactive`` (``single_select`` / ``text_input`` / ``confirm`` /
``run_with_status``), so provider setup looks and behaves exactly like the
onboarding wizard. It previously hand-rolled its own ``prompt_toolkit``
``Application`` menu and a bare ``PromptSession(is_password=True)``, which is
why key entry had no chrome, no spinner, and no verified state.

Provider metadata comes from ``velune.providers.catalog`` — the single source of
truth — not from a private table.

Credential state comes from ``keystore.verification_state()``, so a rejected or
never-checked key is shown as such. The old ``has_key()`` check reported a
revoked key as "configured".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from velune.cli import design
from velune.cli.interactive import (
    BACK,
    CANCEL,
    Option,
    run_with_status,
    single_select,
    text_input,
)
from velune.providers import catalog
from velune.providers.credential_manager import (
    CredentialAddResult,
    add_credential,
    persist_credential,
)
from velune.providers.discovery.scanner import ModelDiscoveryScanner
from velune.providers.keystore import (
    KeyState,
    is_ollama_live,
    verification_state,
)
from velune.providers.validation import ValidationStatus

if TYPE_CHECKING:
    from rich.console import Console

_log = logging.getLogger("velune.cli.provider_ui")

# How a credential state is shown on a provider row: (badge text, color token).
# MISSING has no badge — an empty row reads as "nothing here yet", which is
# exactly right, and a "not configured" badge on two-thirds of the list is noise.
_STATE_BADGE: dict[KeyState, tuple[str, str]] = {
    KeyState.VERIFIED: (f"{design.ICON_SUCCESS} verified", design.OK),
    KeyState.UNVERIFIED: ("unverified", design.WARN),
    KeyState.STALE: ("stale", design.MUTED),
    KeyState.INVALID: (f"{design.ICON_ERROR} invalid", design.DANGER),
    KeyState.ENV: ("env", design.INFO),
    KeyState.MISSING: ("", design.MUTED),
}

_FAILURE_HINTS: dict[ValidationStatus, str] = {
    ValidationStatus.INVALID_KEY: (
        "The provider rejected this key. Check it was copied in full, with no trailing spaces."
    ),
    ValidationStatus.EXPIRED_KEY: "This key has expired. Generate a new one in the console.",
    ValidationStatus.REVOKED_KEY: "This key was revoked. You need to create a replacement.",
    ValidationStatus.RATE_LIMITED: "The API is rate-limited right now. Wait a moment and retry.",
    ValidationStatus.PERMISSION_DENIED: (
        "The key is real but lacks permission for this operation — check its scopes."
    ),
    ValidationStatus.NETWORK_ERROR: (
        "Could not reach the provider. If you're offline you can still save the key "
        "and it will be verified later."
    ),
    ValidationStatus.MALFORMED_KEY: "The key format looks wrong — check you copied all of it.",
}


def _is_local(pid: str) -> bool:
    meta = catalog.get(pid)
    return meta is not None and not meta.requires_key


def _local_status(pid: str) -> str:
    """Liveness of a local, keyless provider: 'running' or 'offline'."""
    if pid == "ollama":
        return "running" if is_ollama_live(timeout=0.5) else "offline"
    if pid == "lmstudio":
        try:
            import httpx

            resp = httpx.get("http://localhost:1234/v1/models", timeout=0.5)
            return "running" if resp.status_code == 200 else "offline"
        except Exception:
            return "offline"
    return "unknown"


def _row(pid: str) -> Option:
    """One provider row, badged with its live credential or liveness state."""
    meta = catalog.get(pid)
    label = meta.display_name if meta else pid
    desc = meta.description if meta else ""

    if _is_local(pid):
        status = _local_status(pid)
        style = design.OK if status == "running" else design.MUTED
        return Option(
            id=pid, label=label, meta=desc, group="Local", badge=status, badge_style=style
        )

    badge, style = _STATE_BADGE[verification_state(pid)]
    return Option(
        id=pid,
        label=label,
        meta=desc,
        group="Cloud",
        badge=badge or None,
        badge_style=style,
    )


def _validate_key_shape(raw: str) -> str | None:
    """Cheap client-side check, run as the user types.

    Deliberately catches only what a *paste* gets wrong — an empty field, or
    embedded whitespace/newlines from selecting too much. Whether the key is
    actually valid is never decided here; only the provider can answer that.
    """
    key = raw.strip()
    if not key:
        return "Enter a key, or press Esc to go back."
    if any(ch.isspace() for ch in key):
        return "That key contains spaces or line breaks — check what you pasted."
    return None


class ProviderPalette:
    """The ``/connect`` provider connection flow, hosted inside the running REPL."""

    def __init__(self, console: Console, container) -> None:
        self.console = console
        self.container = container

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self, provider_id: str = "") -> None:
        """Connect a provider.

        With *provider_id* the picker is skipped and key entry starts straight
        away; without it the user picks from the catalog first. There are no
        other modes — this is the whole surface.
        """
        pid = provider_id.strip().lower()
        await (self._connect(pid) if pid else self._flow_add())

    # ------------------------------------------------------------------
    # Add / connect
    # ------------------------------------------------------------------

    async def _flow_add(self) -> None:
        pid = await single_select(
            "Add Provider",
            [_row(p.id) for p in catalog.list_cloud_providers_alphabetical()],
            subtitle="Choose a provider to connect",
            filterable=True,
            palette=True,
            frame_title="Connect provider",
        )
        if pid in (BACK, CANCEL):
            return
        await self._connect(str(pid))

    async def _connect(self, pid: str) -> None:
        """Key entry → live verification → persist. The core flow."""
        meta = catalog.get(pid)
        if meta is None or not meta.requires_key:
            self.console.print(f"[{design.WARN}]Unknown cloud provider: {pid}[/{design.WARN}]")
            return

        while True:
            entered = await text_input(
                f"{meta.key_label or meta.display_name + ' API key'}",
                hint=f"Get one at {meta.get_key_url}  ·  input is hidden",
                password=True,
                validate=_validate_key_shape,
                palette=True,
                frame_title=f"{meta.display_name} key",
            )
            if entered in (BACK, CANCEL):
                return

            key = str(entered).strip()

            # set_as_first_default=False: the REPL's /providers add has never
            # auto-adopted the first-connected provider as the workspace
            # default — only `velune provider add` (commands/providers.py)
            # does that. Routing both through add_credential must not
            # silently change either one's existing behavior.
            result: CredentialAddResult = await run_with_status(
                add_credential(pid, key, set_as_first_default=False),
                pending=f"Verifying with {meta.display_name}…",
                ok=lambda r: (
                    f"Verified — {len(r.validation.models)} "
                    f"model{'s' if len(r.validation.models) != 1 else ''} available"
                    if r.validation and r.validation.models
                    else "Verified — key accepted"
                ),
                fail=lambda r: (
                    r.validation.human_message() if r.validation else "Verification failed"
                ),
                is_ok=lambda r: r.ok,
            )

            if result.ok:
                self._report_saved(meta.display_name)
                await self._discover_one(pid)
                return

            hint = _FAILURE_HINTS.get(result.validation.status) if result.validation else None
            if hint:
                self.console.print(f"[{design.FAINT}]{hint}[/{design.FAINT}]")

            action = await single_select(
                f"{meta.display_name} — not connected",
                [
                    Option("retry", "Try another key", "Enter a different API key"),
                    Option(
                        "anyway",
                        "Save anyway",
                        "Store it unverified — for when you're offline",
                    ),
                    Option("cancel", "Cancel", "Return without saving"),
                ],
                palette=True,
                frame_title=f"{meta.display_name} — not connected",
            )
            if action == "retry":
                continue
            if action == "anyway":
                # Explicitly NOT verified: the provider never accepted this key,
                # and recording it as verified is exactly the lie this rework
                # exists to remove. It will be re-checked in the background.
                # Persist directly (no re-validation) — the verdict above
                # already told us it failed.
                persist_credential(pid, key, verified=False)
                self.console.print(
                    f"[{design.WARN}]Saved unverified — Velune will re-check it "
                    f"automatically.[/{design.WARN}]"
                )
            return

    def _report_saved(self, label: str) -> None:
        """Say where the key actually went.

        The old copy claimed "saved securely to OS keychain", which was not true:
        the key is AES-GCM-encrypted into credentials.json, and only the *master*
        key lives in the OS keyring — falling back to a machine-derived key when
        no keyring is available.
        """
        from velune.providers.keystore import credentials_file_path

        self.console.print(f"[bold {design.OK}]{label} connected.[/bold {design.OK}]")
        self.console.print(
            f"[{design.MUTED}]Key encrypted (AES-GCM) at {credentials_file_path()}[/{design.MUTED}]"
        )

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------

    async def _discover_one(self, pid: str) -> None:
        meta = catalog.get(pid)
        label = meta.display_name if meta else pid

        async def _scan():
            try:
                return await ModelDiscoveryScanner().scan_provider(pid)
            except Exception as exc:
                _log.debug("Discovery error for %s: %s", pid, exc)
                return []

        models = await run_with_status(
            _scan(),
            pending=f"Discovering {label} models…",
            ok=lambda ms: f"{len(ms)} {label} model(s) available",
            # Not an error: several providers legitimately expose no list endpoint.
            fail=f"No model list from {label} — this is normal for some providers",
            is_ok=lambda ms: bool(ms),
        )
        self._register(models)

    def _register(self, models) -> None:
        """Register discovered models *and* persist the catalog to disk.

        The persist step is what makes a freshly connected provider survive a
        restart. Without it the models existed only in this process: the key
        was saved correctly, but the next launch loaded a cache with no models
        for the provider and reported it as unconnected — the bug this
        reads as "my API key was lost".
        """
        if not models:
            return
        try:
            mr = self.container.get("runtime.model_registry")
            for m in models:
                mr.register(m)
            if not mr.persist():
                _log.debug("Model registry has no disk cache; discovery is session-only.")
        except Exception as exc:
            _log.debug("Could not register discovered models: %s", exc)
