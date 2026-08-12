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
    """Raised when a model is not found.

    Maps to a provider's HTTP 404 on a chat/completions (or embeddings) call —
    an unknown, mistyped, or decommissioned model id, or a malformed endpoint
    path. Deliberately NOT a subclass of :class:`InferenceError`: a 404 is a
    deterministic verdict about the request, not a transient failure, so it
    must not be in :data:`velune.providers.retrying.RETRYABLE_EXCEPTIONS` —
    retrying the exact same bad model id three times just delays the same
    inevitable 404, and for a genuinely transient hiccup a real 5xx or
    connection error already retries via ``InferenceError``/
    ``ProviderConnectionError``.
    """

    pass


class InvalidRequestError(ProviderError):
    """Raised when a provider rejects a request as malformed (HTTP 400/422).

    Like :class:`ModelNotFoundError`, this is a verdict about the request
    itself, not the network or the provider's availability — never retryable.
    """

    pass


class InferenceError(ProviderError):
    """Raised when inference fails for a reason that may be transient
    (5xx, network hiccups, and anything not classified more specifically).

    Retried by :class:`velune.providers.retrying.RetryingProvider`. A verdict
    that is actually about the request or the credential must use a more
    specific, non-retryable type instead (:class:`ModelNotFoundError`,
    :class:`InvalidRequestError`, :class:`ProviderAuthenticationError`).
    """

    pass


class ProviderTimeoutError(InferenceError):
    """Raised when a provider does not send a first response chunk within the
    configured time budget — see
    :class:`velune.providers.retrying.RetryingProvider`'s
    ``first_chunk_timeout_s``.

    A subclass of :class:`InferenceError` (not a new top-level bucket) so it
    is retried the same bounded number of times as any other transient
    failure, per the "408/timeout -> retry policy" classification — the REPL
    must reach a terminal state (a clean error) rather than sit at "Cooking…"
    indefinitely if a provider genuinely never responds.
    """

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
