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


class RateLimitError(InferenceError):
    """Raised when a provider rate-limits a request (HTTP 429).

    ``retry_after`` is the provider's own suggested wait in seconds — parsed
    from a ``Retry-After`` response header by
    :func:`velune.providers.adapters._http_errors.parse_retry_after` — or
    ``None`` when the provider didn't send one, in which case callers (see
    :mod:`velune.providers.retrying`) fall back to standard exponential
    backoff instead.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
