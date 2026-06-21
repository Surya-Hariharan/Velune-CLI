"""Provider-related errors."""


class ProviderError(Exception):
    """Base exception for provider errors."""

    pass


class ProviderNotFoundError(ProviderError):
    """Raised when a provider is not found."""

    pass


class ProviderConnectionError(ProviderError):
    """Raised when connection to provider fails."""

    pass


class ProviderAuthenticationError(ProviderError):
    """Raised when provider authentication fails."""

    pass


class ModelNotFoundError(ProviderError):
    """Raised when a model is not found."""

    pass


class InferenceError(ProviderError):
    """Raised when inference fails."""

    pass
