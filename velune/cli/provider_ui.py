"""Interactive provider management for the Velune REPL — the /providers and
/connect commands.

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
    confirm,
    run_with_status,
    single_select,
    text_input,
)
from velune.providers import catalog
from velune.providers.discovery.scanner import ModelDiscoveryScanner
from velune.providers.keystore import (
    KeyState,
    delete_key,
    get_key,
    is_ollama_live,
    mark_verified,
    save_key,
    verification_state,
)
from velune.providers.validation import (
    ValidationResult,
    ValidationStatus,
    validate_provider,
)

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
    """Interactive provider management, hosted inside the running REPL."""

    def __init__(self, console: Console, container) -> None:
        self.console = console
        self.container = container

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self, args: str = "") -> None:
        parts = args.strip().split(None, 1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip().lower() if len(parts) > 1 else ""

        if sub == "add":
            await (self._connect(rest) if rest else self._flow_add())
        elif sub == "manage":
            await (self._detail(rest) if rest else self._flow_manage())
        elif sub in ("discover", "refresh"):
            await self._discover_all()
        elif sub == "test":
            await (self._test(rest) if rest else self._flow_test())
        elif sub == "status":
            self._show_status_table()
        elif sub == "remove":
            if rest:
                await self._remove(rest)
            else:
                self.console.print(f"[{design.WARN}]Usage: /providers remove <id>[/{design.WARN}]")
        else:
            await self._main_menu()

    async def _main_menu(self) -> None:
        choice = await single_select(
            "Providers",
            [
                Option("add", "Add Provider", "Connect a cloud provider with an API key"),
                Option("manage", "Manage Providers", "View, update, or remove a provider"),
                Option("test", "Test Connection", "Re-verify a configured provider"),
                Option("discover", "Discover Models", "Re-fetch models from every provider"),
                Option("status", "Provider Status", "Status table for every provider"),
            ],
            subtitle="Manage cloud AI provider connections",
        )
        if choice in (BACK, CANCEL):
            return
        if choice == "add":
            await self._flow_add()
        elif choice == "manage":
            await self._flow_manage()
        elif choice == "test":
            await self._flow_test()
        elif choice == "discover":
            await self._discover_all()
        elif choice == "status":
            self._show_status_table()

    # ------------------------------------------------------------------
    # Add / connect
    # ------------------------------------------------------------------

    async def _flow_add(self) -> None:
        pid = await single_select(
            "Add Provider",
            [_row(p.id) for p in catalog.list_cloud_providers_alphabetical()],
            subtitle="Choose a provider to connect",
            filterable=True,
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
            )
            if entered in (BACK, CANCEL):
                return

            key = str(entered).strip()

            result: ValidationResult = await run_with_status(
                validate_provider(pid, key),
                pending=f"Verifying with {meta.display_name}…",
                ok=lambda r: (
                    f"Verified — {len(r.models)} model{'s' if len(r.models) != 1 else ''} available"
                    if r.models
                    else "Verified — key accepted"
                ),
                fail=lambda r: r.human_message(),
                is_ok=lambda r: r.ok,
            )

            if result.ok:
                save_key(pid, key, verified=True)
                mark_verified(pid, model_count=len(result.models))
                self._report_saved(meta.display_name)
                await self._discover_one(pid)
                return

            hint = _FAILURE_HINTS.get(result.status)
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
            )
            if action == "retry":
                continue
            if action == "anyway":
                # Explicitly NOT verified: the provider never accepted this key,
                # and recording it as verified is exactly the lie this rework
                # exists to remove. It will be re-checked in the background.
                save_key(pid, key, verified=False)
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
    # Manage / detail / remove
    # ------------------------------------------------------------------

    async def _flow_manage(self) -> None:
        while True:
            rows = [_row(p.id) for p in catalog.list_providers_alphabetical()]
            pid = await single_select(
                "Manage Providers",
                rows,
                subtitle="Select a provider to view or change",
                filterable=True,
            )
            if pid in (BACK, CANCEL):
                return
            await self._detail(str(pid))

    async def _detail(self, pid: str) -> None:
        meta = catalog.get(pid)
        if meta is None:
            self.console.print(f"[{design.WARN}]Unknown provider: {pid}[/{design.WARN}]")
            return

        if _is_local(pid):
            options = [
                Option("test", "Test Connection", "Check the local server is reachable"),
                Option("refresh", "Refresh Models", "Re-fetch this server's model list"),
            ]
        elif verification_state(pid) is KeyState.MISSING:
            options = [Option("add", "Connect", "Add an API key to enable this provider")]
        else:
            options = [
                Option("test", "Test Connection", "Re-verify the stored key now"),
                Option("update", "Replace API Key", "Enter a new key for this provider"),
                Option("refresh", "Refresh Models", "Re-fetch the model catalogue"),
                Option("remove", "Remove API Key", "Delete the key and disconnect"),
            ]

        action = await single_select(
            meta.display_name,
            options,
            subtitle=self._detail_subtitle(pid),
        )
        if action in (BACK, CANCEL):
            return
        if action in ("add", "update"):
            await self._connect(pid)
        elif action == "test":
            await self._test(pid)
        elif action == "refresh":
            await self._discover_one(pid)
        elif action == "remove":
            await self._remove(pid)

    def _detail_subtitle(self, pid: str) -> str:
        if _is_local(pid):
            return f"local  ·  {_local_status(pid)}"
        state = verification_state(pid)
        badge = _STATE_BADGE[state][0] or "not configured"
        try:
            mr = self.container.get("runtime.model_registry")
            count = len(mr.get_by_provider(pid))
        except Exception:
            count = 0
        return f"{badge}  ·  {count} cached model(s)" if count else badge

    async def _remove(self, pid: str) -> None:
        meta = catalog.get(pid)
        label = meta.display_name if meta else pid

        ok = await confirm(
            f"Remove the {label} API key?",
            hint=f"This disconnects all {label} models from Velune.",
            default=False,
        )
        if ok is not True:
            self.console.print(f"[{design.MUTED}]Cancelled — key kept.[/{design.MUTED}]")
            return

        delete_key(pid)
        self.console.print(f"[{design.OK}]{label} disconnected.[/{design.OK}]")

        evicted = 0
        try:
            mr = self.container.get("runtime.model_registry")
            for m in list(mr.get_by_provider(pid)):
                if mr.remove(m.model_id, pid):
                    evicted += 1
        except Exception:
            pass
        if evicted:
            self.console.print(
                f"[{design.MUTED}]Removed {evicted} cached model(s).[/{design.MUTED}]"
            )

    # ------------------------------------------------------------------
    # Test
    # ------------------------------------------------------------------

    async def _flow_test(self) -> None:
        configured = [
            p.id
            for p in catalog.list_cloud_providers_alphabetical()
            if verification_state(p.id) is not KeyState.MISSING
        ]
        if not configured:
            self.console.print(
                f"[{design.WARN}]No providers configured yet. "
                f"Run [bold]/connect[/bold] to connect one.[/{design.WARN}]"
            )
            return

        options = [Option("__all__", "Test all", "Re-verify every configured provider")]
        options += [_row(pid) for pid in configured]

        pid = await single_select("Test Connection", options, filterable=True)
        if pid in (BACK, CANCEL):
            return

        if pid == "__all__":
            for one in configured:
                await self._test(one)
        else:
            await self._test(str(pid))

    async def _test(self, pid: str) -> None:
        """Live round-trip against a provider, persisting the verdict."""
        meta = catalog.get(pid)
        if meta is None:
            self.console.print(f"[{design.WARN}]Unknown provider: {pid}[/{design.WARN}]")
            return
        label = meta.display_name

        if meta.requires_key and not get_key(pid):
            self.console.print(f"[{design.WARN}]{label} — no API key configured.[/{design.WARN}]")
            return

        if meta.requires_key:
            # Route through the verifier so the stored state is updated by the
            # same rules the background sweep uses — in particular, a network
            # error must not mark the key invalid.
            from velune.providers.verifier import reverify

            work = reverify(pid)
        else:
            work = validate_provider(pid, "")

        await run_with_status(
            work,
            pending=f"Testing {label}…",
            ok=lambda r: (
                f"{label} — connected ({len(r.models)} model(s))"
                if r.models
                else f"{label} — connected"
            ),
            fail=lambda r: f"{label} — {r.human_message()}",
            is_ok=lambda r: r.ok,
        )

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------

    async def _discover_all(self) -> None:
        async def _scan():
            return await ModelDiscoveryScanner().scan_all()

        models = await run_with_status(
            _scan(),
            pending="Scanning connected providers for models…",
            ok=lambda ms: f"Registered {len(ms)} model(s)",
            fail="No models discovered",
            is_ok=lambda ms: bool(ms),
        )
        self._register(models)

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
        if not models:
            return
        try:
            mr = self.container.get("runtime.model_registry")
            for m in models:
                mr.register(m)
        except Exception as exc:
            _log.debug("Could not register discovered models: %s", exc)

    # ------------------------------------------------------------------
    # Status table
    # ------------------------------------------------------------------

    def _show_status_table(self) -> None:
        from rich.table import Table

        table = Table(border_style=design.FAINT, padding=(0, 1))
        table.add_column("Provider", style=design.WHITE, min_width=14)
        table.add_column("Type", style=design.MUTED, width=6)
        table.add_column("Status", min_width=12)
        table.add_column("Env Var", style=design.FAINT)

        for meta in catalog.list_providers_alphabetical():
            if meta.requires_key:
                state = verification_state(meta.id)
                text, color = _STATE_BADGE[state]
                text = text or "not configured"
                kind = "cloud"
            else:
                status = _local_status(meta.id)
                text = status
                color = design.OK if status == "running" else design.MUTED
                kind = "local"

            table.add_row(
                meta.display_name,
                kind,
                f"[{color}]{text}[/{color}]",
                meta.env_var or "—",
            )

        self.console.print(table)
        self.console.print(
            f"[{design.MUTED}]Stale keys are re-checked automatically. "
            f"[bold]/connect[/bold] to connect a provider.[/{design.MUTED}]"
        )
