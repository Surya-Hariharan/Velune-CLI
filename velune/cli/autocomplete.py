from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

SLASH_COMMANDS: list[tuple[str, str]] = [
    ("ask",         "Send a prompt to the active model"),
    ("run",         "Execute a task through the council"),
    ("council",     "Force full council on a task"),
    ("model",       "Switch the active model interactively"),
    ("models",      "List all available models"),
    ("mode",        "Show current session mode and settings"),
    ("optimus",     "Switch to speed mode — smallest model, instant tier"),
    ("godly",       "Switch to max power mode — largest model, full council"),
    ("normal",      "Return to balanced normal mode"),
    ("setup",       "Configure API provider keys"),
    ("memory",      "Inspect memory tiers and session stats"),
    ("session",     "Save, list, resume, or export sessions"),
    ("usage",       "Show token usage and cost for this session"),
    ("context",     "Show context window usage"),
    ("diff",        "Show pending file changes as unified diff"),
    ("doctor",      "Run environment health checks"),
    ("help",        "Show all available commands"),
    ("clear",       "Clear screen and conversation context"),
    ("exit",        "Exit the Velune session"),
    ("councilmodel","Assign specific models to council agent roles"),
    ("cm",          "Assign specific models to council agent roles"),
    ("roles",       "Show council role assignments table"),
    ("pull",        "Download an Ollama model interactively"),
    ("download",    "Download an Ollama model interactively"),
    ("get",         "Download an Ollama model interactively"),
    ("delete",      "Delete a locally installed Ollama model"),
    ("remove",      "Delete a locally installed Ollama model"),
    ("rm",          "Delete a locally installed Ollama model"),
]

_MODEL_PREFIX = "/model "


class SlashCompleter(Completer):
    def __init__(
        self,
        extra_commands: list[tuple[str, str]] | None = None,
        model_ids: list[str] | None = None,
    ) -> None:
        self._commands = SLASH_COMMANDS.copy()
        if extra_commands:
            self._commands.extend(extra_commands)
        self._model_ids: list[str] = model_ids or []

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor

        if not text.startswith("/"):
            return

        # "/model <partial>" → complete model IDs
        if text.startswith(_MODEL_PREFIX):
            partial = text[len(_MODEL_PREFIX):]
            for mid in self._model_ids:
                if mid.startswith(partial):
                    yield Completion(
                        text=mid,
                        start_position=-len(partial),
                        display=mid,
                    )
            return

        # "/<partial>" → complete command names
        word = text[1:]
        for cmd_name, description in self._commands:
            if cmd_name.startswith(word.lower()):
                yield Completion(
                    text=cmd_name,
                    start_position=-len(word),
                    display=f"/{cmd_name}",
                    display_meta=description,
                )
