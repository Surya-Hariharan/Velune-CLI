"""Deprecated. Use velune.providers.adapters.anthropic."""
import warnings

warnings.warn("Import from velune.providers.adapters.anthropic", DeprecationWarning, stacklevel=2)
from velune.providers.adapters.anthropic import AnthropicProvider

__all__ = ["AnthropicProvider"]
