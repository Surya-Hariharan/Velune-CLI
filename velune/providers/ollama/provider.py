"""Deprecated. Use velune.providers.adapters.ollama."""
import warnings
warnings.warn("Import from velune.providers.adapters.ollama", DeprecationWarning, stacklevel=2)
from velune.providers.adapters.ollama import OllamaProvider
__all__ = ["OllamaProvider"]
