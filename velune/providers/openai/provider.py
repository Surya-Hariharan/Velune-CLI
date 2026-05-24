"""Deprecated. Use velune.providers.adapters.openai."""
import warnings
warnings.warn("Import from velune.providers.adapters.openai", DeprecationWarning, stacklevel=2)
from velune.providers.adapters.openai import OpenAIProvider
__all__ = ["OpenAIProvider"]
