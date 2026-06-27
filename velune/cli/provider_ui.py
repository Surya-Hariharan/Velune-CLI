"""Interactive Provider Management UI for the Velune REPL.

Implements the /providers command: a searchable palette for adding, managing,
testing, and discovering models from cloud AI providers — all inside the REPL
without requiring separate CLI commands.

Architecture:
- ProviderPalette is the entry point, constructed with the REPL's console and
  service container.
- All interactive menus use prompt_toolkit Application (same pattern as
  VeluneREPL._show_model_picker), so they interoperate cleanly with the REPL's
  own prompt session.
- Validation, keystore, and discovery reuse existing modules; no duplication.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from velune.cli import design
from velune.providers.discovery.scanner import ModelDiscoveryScanner
from velune.providers.keystore import (
    delete_key,
    get_key,
    has_key,
    is_ollama_live,
    save_key,
)
from velune.providers.validation import (
    ValidationStatus,
    validate_provider_sync,
)

if TYPE_CHECKING:
    from rich.console import Console

_log = logging.getLogger("velune.cli.provider_ui")

# ---------------------------------------------------------------------------
# Provider catalogue
# ---------------------------------------------------------------------------

CLOUD_PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic",
        "description": "Creator of Claude — leading AI safety research and models.",
        "env": "ANTHROPIC_API_KEY",
        "url": "https://console.anthropic.com",
    },
    "openai": {
        "label": "OpenAI",
        "description": "GPT-4, o1, and more — the most widely integrated AI platform.",
        "env": "OPENAI_API_KEY",
        "url": "https://platform.openai.com/api-keys",
    },
    "google": {
        "label": "Google Gemini",
        "description": "Gemini Pro, Flash, Ultra — Google's multimodal AI family.",
        "env": "GOOGLE_API_KEY",
        "url": "https://aistudio.google.com/app/apikey",
    },
    "groq": {
        "label": "Groq",
        "description": "Ultra-fast LPU inference — open models at blazing speed.",
        "env": "GROQ_API_KEY",
        "url": "https://console.groq.com/keys",
    },
    "openrouter": {
        "label": "OpenRouter",
        "description": "Unified access to 100+ models from all major providers.",
        "env": "OPENROUTER_API_KEY",
        "url": "https://openrouter.ai/keys",
    },
    "nvidia": {
        "label": "NVIDIA NIM",
        "description": "Optimized inference on NVIDIA hardware — enterprise AI at scale.",
        "env": "NVIDIA_API_KEY",
        "url": "https://build.nvidia.com/",
    },
    "xai": {
        "label": "xAI (Grok)",
        "description": "xAI's Grok models — built for real-time reasoning.",
        "env": "XAI_API_KEY",
        "url": "https://console.x.ai",
    },
    "together": {
        "label": "Together.AI",
        "description": "Open model ecosystem — fine-tuning, inference, and embedding.",
        "env": "TOGETHER_API_KEY",
        "url": "https://api.together.ai/settings/api-keys",
    },
    "fireworks": {
        "label": "Fireworks.AI",
        "description": "Production open model inference — fast, cheap, and reliable.",
        "env": "FIREWORKS_API_KEY",
        "url": "https://fireworks.ai/account/api-keys",
    },
    "deepseek": {
        "label": "DeepSeek",
        "description": "DeepSeek-V3, R1 and Coder — powerful reasoning and coding models.",
        "env": "DEEPSEEK_API_KEY",
        "url": "https://platform.deepseek.com/api_keys",
    },
    "cohere": {
        "label": "Cohere",
        "description": "Enterprise NLP — Command R+, embeddings, and reranking.",
        "env": "COHERE_API_KEY",
        "url": "https://dashboard.cohere.com/api-keys",
    },
    "huggingface": {
        "label": "HuggingFace",
        "description": "70,000+ models via HF Inference API and Inference Endpoints.",
        "env": "HF_TOKEN",
        "url": "https://huggingface.co/settings/tokens",
    },
    "mistral": {
        "label": "Mistral AI",
        "description": "European AI — Mistral Large, Codestral, and Mistral Nemo.",
        "env": "MISTRAL_API_KEY",
        "url": "https://console.mistral.ai/api-keys",
    },
}

LOCAL_PROVIDERS: dict[str, dict] = {
    "ollama": {
        "label": "Ollama",
        "description": "Run models locally — zero cloud dependency.",
        "url": "https://ollama.com",
        "local": True,
    },
    "lmstudio": {
        "label": "LM Studio",
        "description": "GUI for local model management and inference.",
        "url": "https://lmstudio.ai",
        "local": True,
    },
}

ALL_PROVIDER_META: dict[str, dict] = {
    **CLOUD_PROVIDERS,
    **LOCAL_PROVIDERS,
}


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------


def provider_status(pid: str) -> str:
    """Return a short human-readable status for a provider."""
    meta = ALL_PROVIDER_META.get(pid, {})
    if meta.get("local"):
        if pid == "ollama":
            return "running" if is_ollama_live(timeout=0.5) else "offline"
        if pid == "lmstudio":
            try:
                import httpx

                r = httpx.get("http://localhost:1234/v1/models", timeout=0.5)
                return "running" if r.status_code == 200 else "offline"
            except Exception:
                return "offline"
        return "unknown"
    return "configured" if has_key(pid) else "not configured"


def status_style(status: str) -> str:
    """Map a status string to a design colour token."""
    return {
        "configured": design.OK,
        "running": design.OK,
        "not configured": design.MUTED,
        "offline": design.WARN,
        "unknown": design.MUTED,
        "invalid": design.DANGER,
        "rate limited": design.WARN,
        "quota exceeded": design.WARN,
        "expired": design.WARN,
        "permission denied": design.DANGER,
    }.get(status, design.MUTED)


def validation_status_label(vs: ValidationStatus) -> str:
    """Convert a ValidationStatus to a concise human label."""
    return {
        ValidationStatus.OK: "Connected",
        ValidationStatus.INVALID_KEY: "Invalid Key",
        ValidationStatus.EXPIRED_KEY: "Expired Key",
        ValidationStatus.REVOKED_KEY: "Revoked Key",
        ValidationStatus.RATE_LIMITED: "Rate Limited",
        ValidationStatus.NETWORK_ERROR: "Network Error",
        ValidationStatus.MALFORMED_KEY: "Malformed Key",
        ValidationStatus.PERMISSION_DENIED: "Permission Denied",
        ValidationStatus.UNKNOWN_ERROR: "Unknown Error",
    }.get(vs, str(vs))


# ---------------------------------------------------------------------------
# ProviderPalette
# ---------------------------------------------------------------------------


class ProviderPalette:
    """Interactive provider management system, designed to run inside the REPL.

    Call ``await palette.run(args)`` from a slash command handler. All menus
    use prompt_toolkit Application (non-blocking, non-full-screen) just like
    VeluneREPL._show_model_picker, so they stack cleanly inside the existing
    REPL event loop.
    """

    def __init__(self, console: Console, container) -> None:
        self.console = console
        self.container = container

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self, args: str = "") -> None:
        """Dispatch to a sub-flow or open the interactive main menu."""
        parts = args.strip().split(None, 1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "add":
            if rest:
                await self._add_single_provider(rest.lower())
            else:
                await self._flow_add_provider()
        elif sub == "manage":
            if rest:
                await self._provider_detail(rest.lower())
            else:
                await self._flow_manage_providers()
        elif sub in ("discover", "refresh"):
            await self._flow_discover_models()
        elif sub == "test":
            if rest:
                await self._test_provider(rest.lower())
            else:
                await self._flow_test_connection()
        elif sub == "status":
            await self._show_status_all()
        elif sub == "remove":
            if rest:
                await self._remove_provider(rest.lower())
            else:
                self.console.print(
                    f"[{design.WARN}]Usage: /providers remove <provider-id>[/{design.WARN}]"
                )
        else:
            await self._main_menu()

    # ------------------------------------------------------------------
    # Main menu
    # ------------------------------------------------------------------

    async def _main_menu(self) -> None:
        choices = [
            ("add", "Add Provider", "Connect a new cloud AI provider with an API key"),
            ("manage", "Manage Providers", "View, update, or remove configured providers"),
            ("discover", "Discover Models", "Refresh model catalogue from all connected providers"),
            ("test", "Test Connection", "Validate credentials for configured providers"),
            ("refresh", "Refresh Models", "Re-fetch model list from every provider"),
            ("status", "Provider Status", "Quick status table for all providers"),
        ]
        selected = await self._show_menu(
            title="Provider Management",
            subtitle="Manage cloud AI provider connections  (type to filter)",
            choices=choices,
        )
        if selected == "add":
            await self._flow_add_provider()
        elif selected == "manage":
            await self._flow_manage_providers()
        elif selected in ("discover", "refresh"):
            await self._flow_discover_models()
        elif selected == "test":
            await self._flow_test_connection()
        elif selected == "status":
            await self._show_status_all()

    # ------------------------------------------------------------------
    # Generic searchable menu (prompt_toolkit Application)
    # ------------------------------------------------------------------

    async def _show_menu(
        self,
        title: str,
        subtitle: str,
        choices: list[tuple[str, str, str]],
        filterable: bool = True,
    ) -> str | None:
        """Show a fuzzy-searchable interactive menu.

        ``choices`` is a list of ``(key, label, description)`` triples.
        Returns the chosen ``key``, or ``None`` if the user cancelled.
        """
        from prompt_toolkit.application import Application
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl

        from velune.cli.autocomplete import fuzzy_score

        selected_idx: list[int] = [0]
        query: list[str] = [""]
        result: list[str | None] = [None]

        def _visible() -> list[tuple[str, str, str]]:
            if not filterable or not query[0]:
                return choices
            q = query[0].lower()
            scored = [
                (
                    max(fuzzy_score(q, label.lower()), fuzzy_score(q, key.lower())),
                    key,
                    label,
                    desc,
                )
                for key, label, desc in choices
            ]
            return [(k, lbl, d) for s, k, lbl, d in sorted(scored, key=lambda t: -t[0]) if s > 0]

        def _render() -> FormattedText:
            visible = _visible()
            if visible:
                selected_idx[0] = min(selected_idx[0], len(visible) - 1)

            lines: list[tuple[str, str]] = [
                (f"bold fg:{design.ACCENT}", f"\n  {title}\n"),
                (f"fg:{design.MUTED}", f"  {subtitle}\n"),
                (f"fg:{design.FAINT}", "  " + "─" * 44 + "\n"),
            ]

            if filterable:
                if query[0]:
                    lines.append((f"fg:{design.INFO}", f"  filter: {query[0]}  "))
                    lines.append(
                        (f"fg:{design.FAINT}", "[↑↓ navigate · Enter select · Esc cancel]\n\n")
                    )
                else:
                    lines.append(
                        (f"fg:{design.FAINT}", "  ↑↓ navigate · Enter select · Esc cancel\n\n")
                    )

            if not visible:
                lines.append((f"fg:{design.WARN}", "  No matches.\n"))
                return FormattedText(lines)

            for i, (_key, label, desc) in enumerate(visible):
                is_sel = i == selected_idx[0]
                prefix = "❯ " if is_sel else "  "
                label_style = f"bold fg:{design.ACCENT}" if is_sel else f"fg:{design.WHITE}"
                lines.append((label_style, f"  {prefix}{label}\n"))
                lines.append((f"fg:{design.MUTED}", f"       {desc}\n\n"))

            return FormattedText(lines)

        kb = KeyBindings()

        @kb.add("up")
        def _up(event) -> None:
            n = len(_visible())
            if n:
                selected_idx[0] = (selected_idx[0] - 1) % n

        @kb.add("down")
        def _down(event) -> None:
            n = len(_visible())
            if n:
                selected_idx[0] = (selected_idx[0] + 1) % n

        @kb.add("enter")
        def _enter(event) -> None:
            visible = _visible()
            if visible:
                result[0] = visible[selected_idx[0]][0]
            event.app.exit()

        @kb.add("escape", eager=True)
        @kb.add("c-c")
        def _cancel(event) -> None:
            event.app.exit()

        @kb.add("backspace")
        def _bs(event) -> None:
            query[0] = query[0][:-1]
            selected_idx[0] = 0

        @kb.add("<any>")
        def _type(event) -> None:
            ch = event.data
            if filterable and ch and ch.isprintable():
                query[0] += ch
                selected_idx[0] = 0

        app = Application(
            layout=Layout(Window(content=FormattedTextControl(_render, focusable=True))),
            key_bindings=kb,
            full_screen=False,
            mouse_support=False,
        )
        await app.run_async()
        return result[0]

    # ------------------------------------------------------------------
    # Masked API key prompt
    # ------------------------------------------------------------------

    async def _prompt_api_key(self, provider_label: str) -> str | None:
        """Prompt for a masked API key. Returns the key or None if cancelled."""
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import FormattedText

        session: PromptSession = PromptSession(is_password=True)
        prompt_text = FormattedText(
            [
                (f"fg:{design.ACCENT} bold", f"  {provider_label} API key"),
                (f"fg:{design.FAINT}", " (hidden)"),
                ("", ": "),
            ]
        )
        try:
            raw: str = await session.prompt_async(prompt_text)
            return raw.strip() or None
        except (KeyboardInterrupt, EOFError):
            return None

    # ------------------------------------------------------------------
    # Inline confirmation (yes/no)
    # ------------------------------------------------------------------

    async def _confirm(self, message: str) -> bool:
        """Two-option confirmation menu. Returns True for yes."""
        choice = await self._show_menu(
            title=message,
            subtitle="",
            choices=[
                ("no", "No — Cancel", "Keep the current state"),
                ("yes", "Yes — Confirm", "Proceed with this action"),
            ],
            filterable=False,
        )
        return choice == "yes"

    # ------------------------------------------------------------------
    # Add Provider flow
    # ------------------------------------------------------------------

    async def _flow_add_provider(self) -> None:
        """Interactive picker: choose a cloud provider then configure it."""
        choices = [
            (pid, meta["label"], meta["description"]) for pid, meta in CLOUD_PROVIDERS.items()
        ]
        pid = await self._show_menu(
            title="Add Provider",
            subtitle="Choose a cloud AI provider to connect  (type to filter)",
            choices=choices,
        )
        if pid is not None:
            await self._add_single_provider(pid)

    async def _add_single_provider(self, pid: str) -> None:
        """Guided workflow to add or update a single cloud provider."""
        meta = CLOUD_PROVIDERS.get(pid)
        if meta is None:
            self.console.print(f"[{design.WARN}]Unknown provider: {pid}[/{design.WARN}]")
            return

        # Step 1 — show provider context
        self.console.print()
        self.console.print(f"[bold {design.ACCENT}]{meta['label']}[/bold {design.ACCENT}]")
        self.console.print(f"[{design.MUTED}]{meta['description']}[/{design.MUTED}]")
        self.console.print(f"[{design.FAINT}]Keys at: {meta['url']}[/{design.FAINT}]")

        cur_status = provider_status(pid)
        cur_color = status_style(cur_status)
        self.console.print(
            f"[{design.MUTED}]Current status:[/{design.MUTED}] "
            f"[{cur_color}]{cur_status}[/{cur_color}]"
        )
        self.console.print()

        # Step 2 — key entry loop (retry / cancel on failure)
        while True:
            api_key = await self._prompt_api_key(meta["label"])

            if api_key is None:
                self.console.print(f"[{design.MUTED}]Cancelled.[/{design.MUTED}]")
                return

            if not api_key:
                action = await self._show_menu(
                    title="No key entered",
                    subtitle="What would you like to do?",
                    choices=[
                        ("retry", "Retry", "Enter the API key again"),
                        ("cancel", "Cancel", "Return to the previous menu"),
                    ],
                    filterable=False,
                )
                if action == "retry":
                    continue
                return

            # Step 3 — validate
            self.console.print(
                f"[{design.MUTED}]Validating {meta['label']} credentials...[/{design.MUTED}]"
            )
            result = await asyncio.to_thread(validate_provider_sync, pid, api_key)

            if result.ok:
                # Step 4 — persist
                await asyncio.to_thread(save_key, pid, api_key)
                self.console.print(f"[{design.OK}]{result.human_message()}[/{design.OK}]")
                self.console.print(
                    f"[{design.OK}]API key saved securely to OS keychain.[/{design.OK}]"
                )
                if result.models:
                    n = len(result.models)
                    preview = ", ".join(result.models[:6])
                    suffix = f" +{n - 6} more" if n > 6 else ""
                    self.console.print(
                        f"[{design.MUTED}]Models: {preview}{suffix}[/{design.MUTED}]"
                    )
                if result.account_info:
                    self._print_account_info(result.account_info)

                # Step 5 — discover models for this provider
                self.console.print(
                    f"[{design.MUTED}]Discovering {meta['label']} models...[/{design.MUTED}]"
                )
                await self._discover_for_provider(pid)

                self.console.print(
                    f"[bold {design.OK}]{meta['label']} is now connected![/bold {design.OK}]"
                )
                return

            else:
                # Validation failed — human-readable diagnosis
                label = validation_status_label(result.status)
                self.console.print(
                    f"[{design.WARN}]{label}: {result.human_message()}[/{design.WARN}]"
                )
                self._print_failure_hint(result.status)

                action = await self._show_menu(
                    title=f"Connection failed — {label}",
                    subtitle="How would you like to proceed?",
                    choices=[
                        ("retry", "Retry", "Enter a different API key"),
                        (
                            "save_anyway",
                            "Save Anyway",
                            "Store without validation (offline / network issue)",
                        ),
                        ("cancel", "Cancel", "Return without saving"),
                    ],
                    filterable=False,
                )
                if action == "retry":
                    continue
                if action == "save_anyway":
                    await asyncio.to_thread(save_key, pid, api_key)
                    self.console.print(
                        f"[{design.WARN}]Key saved without validation.[/{design.WARN}]"
                    )
                return

    # ------------------------------------------------------------------
    # Manage Providers flow
    # ------------------------------------------------------------------

    async def _flow_manage_providers(self) -> None:
        """List every provider with status → pick one → provider detail."""
        while True:
            choices: list[tuple[str, str, str]] = []
            for pid, meta in CLOUD_PROVIDERS.items():
                st = provider_status(pid)
                color = status_style(st)
                f"[{color}]{st}[/{color}]  ·  {meta['description'][:48]}"
                # Strip Rich tags for the plain-text description shown in the menu
                plain_desc = f"{st}  ·  {meta['description'][:48]}"
                choices.append((pid, meta["label"], plain_desc))
            for pid, meta in LOCAL_PROVIDERS.items():
                st = provider_status(pid)
                choices.append((pid, meta["label"], f"{st}  ·  {meta['description'][:48]}"))

            pid = await self._show_menu(
                title="Manage Providers",
                subtitle="Select a provider to view details or take action  (type to filter)",
                choices=choices,
            )
            if pid is None:
                return

            await self._provider_detail(pid)

    async def _provider_detail(self, pid: str) -> None:
        """Show details and an action menu for a single provider."""
        meta = ALL_PROVIDER_META.get(pid)
        if meta is None:
            self.console.print(f"[{design.WARN}]Unknown provider: {pid}[/{design.WARN}]")
            return

        is_local = meta.get("local", False)
        label = meta["label"]

        # Context print
        self.console.print()
        self.console.print(f"[bold {design.ACCENT}]{label}[/bold {design.ACCENT}]")
        self.console.print(f"[{design.MUTED}]{meta.get('description', '')}[/{design.MUTED}]")

        st = provider_status(pid)
        self.console.print(
            f"[{design.MUTED}]Status:[/{design.MUTED}] [{status_style(st)}]{st}[/{status_style(st)}]"
        )

        # Cached model count
        try:
            mr = self.container.get("runtime.model_registry")
            n_models = len(mr.get_by_provider(pid))
            if n_models:
                self.console.print(f"[{design.MUTED}]Cached models: {n_models}[/{design.MUTED}]")
        except Exception:
            pass
        self.console.print()

        # Build action list based on provider type / current state
        if is_local:
            choices: list[tuple[str, str, str]] = [
                ("test", "Test Connection", "Verify the local server is reachable"),
                ("refresh", "Refresh Models", "Re-fetch the model list from this server"),
                ("back", "← Back", "Return to the provider list"),
            ]
        else:
            choices = []
            if has_key(pid):
                choices += [
                    ("update", "Update API Key", "Replace the stored API key"),
                    ("remove", "Remove API Key", "Delete the key and disconnect this provider"),
                    ("test", "Test Connection", "Validate credentials with a live API call"),
                    ("refresh", "Refresh Models", "Re-fetch the model catalogue"),
                ]
            else:
                choices += [
                    ("add", "Connect Provider", "Add an API key to enable this provider"),
                ]
            choices.append(("back", "← Back", "Return to the provider list"))

        action = await self._show_menu(
            title=f"{label} — Actions",
            subtitle="Choose an action",
            choices=choices,
            filterable=False,
        )

        if action in ("add", "update"):
            await self._add_single_provider(pid)
        elif action == "remove":
            await self._remove_provider(pid)
        elif action == "test":
            await self._test_provider(pid)
        elif action == "refresh":
            await self._discover_for_provider(pid)

    # ------------------------------------------------------------------
    # Remove Provider
    # ------------------------------------------------------------------

    async def _remove_provider(self, pid: str) -> None:
        """Confirm then remove a provider's API key and evict cached models."""
        meta = ALL_PROVIDER_META.get(pid, {"label": pid})
        label = meta["label"]

        self.console.print()
        self.console.print(f"[bold {design.WARN}]Remove {label} API key?[/bold {design.WARN}]")
        self.console.print(
            f"[{design.MUTED}]This will disconnect all {label} models from Velune.[/{design.MUTED}]"
        )
        self.console.print()

        if not await self._confirm(f"Remove {label} key?"):
            self.console.print(f"[{design.MUTED}]Cancelled. Key is still stored.[/{design.MUTED}]")
            return

        await asyncio.to_thread(delete_key, pid)
        self.console.print(
            f"[{design.OK}]API key for {label} removed from OS keychain.[/{design.OK}]"
        )

        # Evict models from the registry
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
                f"[{design.MUTED}]Removed {evicted} cached model(s) for {label}.[/{design.MUTED}]"
            )

        # If no cloud providers remain, surface a helpful notice
        configured_cloud = [p for p in CLOUD_PROVIDERS if has_key(p)]
        if not configured_cloud:
            self.console.print(
                f"[{design.WARN}]No cloud providers are currently configured. "
                f"Use /providers add to connect one.[/{design.WARN}]"
            )

    # ------------------------------------------------------------------
    # Test Connection flow
    # ------------------------------------------------------------------

    async def _flow_test_connection(self) -> None:
        """Pick a provider then test it; or test all configured ones."""
        configured = [
            (pid, CLOUD_PROVIDERS[pid]["label"]) for pid in CLOUD_PROVIDERS if has_key(pid)
        ]
        if not configured:
            self.console.print(
                f"[{design.WARN}]No cloud providers configured. "
                f"Run /providers add to get started.[/{design.WARN}]"
            )
            return

        choices: list[tuple[str, str, str]] = [
            ("__all__", "Test All", "Validate every configured provider in sequence"),
        ]
        choices += [(pid, label, f"Test {label} credentials") for pid, label in configured]

        selected = await self._show_menu(
            title="Test Connection",
            subtitle="Select a provider to test  (type to filter)",
            choices=choices,
        )
        if selected is None:
            return

        if selected == "__all__":
            for pid, _ in configured:
                await self._test_provider(pid)
        else:
            await self._test_provider(selected)

    async def _test_provider(self, pid: str) -> None:
        """Live validation round-trip for a single provider."""
        meta = ALL_PROVIDER_META.get(pid, {"label": pid, "local": False})
        label = meta["label"]
        is_local = meta.get("local", False)

        key = "" if is_local else (get_key(pid) or "")
        if not is_local and not key:
            self.console.print(f"[{design.WARN}]{label} — no API key configured.[/{design.WARN}]")
            return

        self.console.print(f"[{design.MUTED}]Testing {label}...[/{design.MUTED}]")
        result = await asyncio.to_thread(validate_provider_sync, pid, key)

        if result.ok:
            n = len(result.models) if result.models else 0
            self.console.print(f"[{design.OK}]{label} — Connected ({n} model(s))[/{design.OK}]")
        else:
            lbl = validation_status_label(result.status)
            self.console.print(
                f"[{design.WARN}]{label} — {lbl}: {result.human_message()}[/{design.WARN}]"
            )
            self._print_failure_hint(result.status)

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------

    async def _flow_discover_models(self) -> None:
        """Trigger full discovery across all connected providers."""
        self.console.print(
            f"[{design.MUTED}]Scanning all connected providers for models...[/{design.MUTED}]"
        )
        try:
            scanner = ModelDiscoveryScanner()
            models = await scanner.scan_all()
        except Exception as exc:
            self.console.print(f"[{design.WARN}]Discovery failed: {exc}[/{design.WARN}]")
            return

        registered = self._register_models(models)
        self.console.print(
            f"[{design.OK}]Discovered and registered {registered} model(s).[/{design.OK}]"
        )

    async def _discover_for_provider(self, pid: str) -> None:
        """Trigger model discovery for a single provider after key setup."""
        meta = ALL_PROVIDER_META.get(pid, {"label": pid})
        label = meta["label"]
        try:
            scanner = ModelDiscoveryScanner()
            models = await scanner.scan_provider(pid)
        except Exception as exc:
            _log.debug("Discovery error for %s: %s", pid, exc)
            models = []

        if models:
            registered = self._register_models(models)
            self.console.print(
                f"[{design.MUTED}]Registered {registered} {label} model(s).[/{design.MUTED}]"
            )
        else:
            self.console.print(
                f"[{design.MUTED}]No models discovered for {label} (this is normal for some providers).[/{design.MUTED}]"
            )

    def _register_models(self, models) -> int:
        """Push discovered models into the model registry. Returns count registered."""
        try:
            mr = self.container.get("runtime.model_registry")
            for m in models:
                mr.register(m)
            return len(models)
        except Exception:
            return len(models)

    # ------------------------------------------------------------------
    # Status overview
    # ------------------------------------------------------------------

    async def _show_status_all(self) -> None:
        """Print a Rich table showing status for every provider."""
        from rich.table import Table

        table = Table(
            title=f"[bold {design.ACCENT}]Provider Status[/bold {design.ACCENT}]",
            border_style=design.FAINT,
            padding=(0, 1),
        )
        table.add_column("Provider", style=design.INFO, min_width=16)
        table.add_column("Type", style=design.MUTED, width=6)
        table.add_column("Status", min_width=18)
        table.add_column("Env Var", style=design.FAINT)

        for pid, meta in sorted(CLOUD_PROVIDERS.items(), key=lambda kv: kv[1]["label"]):
            st = provider_status(pid)
            color = status_style(st)
            table.add_row(
                meta["label"],
                "cloud",
                f"[{color}]{st}[/{color}]",
                meta.get("env") or "—",
            )
        for pid, meta in sorted(LOCAL_PROVIDERS.items(), key=lambda kv: kv[1]["label"]):
            st = provider_status(pid)
            color = status_style(st)
            table.add_row(meta["label"], "local", f"[{color}]{st}[/{color}]", "—")

        self.console.print(table)
        self.console.print(
            f"\n[{design.MUTED}]Use [bold]/providers add[/bold] to connect a provider, "
            f"[bold]/providers manage[/bold] to update or remove one.[/{design.MUTED}]"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _print_account_info(self, info: dict) -> None:
        parts: list[str] = []
        if "username" in info:
            parts.append(f"user: {info['username']}")
        if "organization" in info:
            parts.append(f"org: {info['organization']}")
        if "model_count" in info:
            parts.append(f"{info['model_count']} models")
        if "total_models" in info:
            parts.append(f"{info['total_models']} total models")
        if parts:
            self.console.print(f"[{design.MUTED}]Account: {', '.join(parts)}[/{design.MUTED}]")

    def _print_failure_hint(self, status: ValidationStatus) -> None:
        hints: dict[ValidationStatus, str] = {
            ValidationStatus.INVALID_KEY: (
                "The key was rejected by the provider. "
                "Double-check it was copied in full with no trailing spaces."
            ),
            ValidationStatus.EXPIRED_KEY: (
                "This key has expired. Generate a new one at the provider's console."
            ),
            ValidationStatus.REVOKED_KEY: (
                "This key has been revoked. You need to create a replacement."
            ),
            ValidationStatus.RATE_LIMITED: (
                "The API is temporarily rate-limited. Wait a moment and retry."
            ),
            ValidationStatus.PERMISSION_DENIED: (
                "Your account or key does not have permission for this operation."
            ),
            ValidationStatus.NETWORK_ERROR: (
                "Could not reach the provider. Check your internet connection."
            ),
            ValidationStatus.MALFORMED_KEY: (
                "The key format looks wrong — check you copied the full key."
            ),
        }
        hint = hints.get(status)
        if hint:
            self.console.print(f"[{design.FAINT}]Hint: {hint}[/{design.FAINT}]")
