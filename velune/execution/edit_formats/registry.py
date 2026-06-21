"""Edit format registry — maps model families to preferred output formats.

Strong models (Claude, GPT-4, DeepSeek-R1, Mistral Large) can reliably
follow the precise SEARCH/REPLACE contract.  Smaller / quantised local models
(Llama 3, Qwen, Phi, Gemma) produce far more consistent output when asked to
write the whole file, so they get whole-file as their primary format.

parse_with_fallback() tries each format in the model's preference list and
returns the first one that yields at least one EditBlock.
"""

from __future__ import annotations

import logging
from pathlib import Path

from velune.execution.edit_formats.base import EditBlock, EditFormat, ParseError
from velune.execution.edit_formats.search_replace import SearchReplaceFormat
from velune.execution.edit_formats.udiff import UDiffFormat
from velune.execution.edit_formats.whole_file import WholeFileFormat
from velune.models.family import ModelFamily

logger = logging.getLogger("velune.execution.edit_formats.registry")

# Ordered preference lists per model family.
_FORMAT_PREFERENCES: dict[ModelFamily, list[EditFormat]] = {
    ModelFamily.CLAUDE: [EditFormat.SEARCH_REPLACE, EditFormat.WHOLE_FILE],
    ModelFamily.GPT: [EditFormat.SEARCH_REPLACE, EditFormat.WHOLE_FILE],
    ModelFamily.DEEPSEEK: [EditFormat.SEARCH_REPLACE, EditFormat.WHOLE_FILE],
    ModelFamily.MISTRAL: [EditFormat.SEARCH_REPLACE, EditFormat.WHOLE_FILE],
    ModelFamily.GEMINI: [EditFormat.WHOLE_FILE, EditFormat.SEARCH_REPLACE],
    ModelFamily.LLAMA3: [EditFormat.WHOLE_FILE, EditFormat.SEARCH_REPLACE],
    ModelFamily.QWEN: [EditFormat.WHOLE_FILE, EditFormat.SEARCH_REPLACE],
    ModelFamily.PHI: [EditFormat.WHOLE_FILE],
    ModelFamily.GEMMA: [EditFormat.WHOLE_FILE, EditFormat.SEARCH_REPLACE],
    ModelFamily.UNKNOWN: [EditFormat.SEARCH_REPLACE, EditFormat.WHOLE_FILE, EditFormat.UDIFF],
}

_PARSERS = {
    EditFormat.SEARCH_REPLACE: SearchReplaceFormat(),
    EditFormat.WHOLE_FILE: WholeFileFormat(),
    EditFormat.UDIFF: UDiffFormat(),
}


def preferred_formats(family: ModelFamily) -> list[EditFormat]:
    """Return the ordered format preference list for *family*."""
    return _FORMAT_PREFERENCES.get(family, _FORMAT_PREFERENCES[ModelFamily.UNKNOWN])


def format_instructions_for(family: ModelFamily) -> str:
    """Return the system-prompt fragment for the primary format of *family*."""
    fmts = preferred_formats(family)
    if not fmts:
        return ""
    return _PARSERS[fmts[0]].format_instructions()


def parse_with_fallback(
    response: str,
    family: ModelFamily,
    workspace_path: Path | None = None,
) -> list[EditBlock]:
    """Try each format in preference order; return the first successful parse.

    If every format fails, returns an empty list rather than raising — the
    caller should treat an empty list as "no edits detected" and surface the
    raw response text to the user instead of silently succeeding.
    """
    for fmt in preferred_formats(family):
        parser = _PARSERS[fmt]
        try:
            blocks = parser.parse(response, workspace_path=workspace_path)
            if blocks:
                logger.debug("Parsed %d edit block(s) using format '%s'", len(blocks), fmt)
                return blocks
        except ParseError:
            logger.debug("Format '%s' found no blocks, trying next fallback", fmt)
        except Exception as exc:
            logger.warning("Format '%s' parser raised unexpected error: %s", fmt, exc)

    logger.debug("No edit blocks found in response (tried all formats for %s)", family)
    return []
