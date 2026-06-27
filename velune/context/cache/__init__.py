"""Context caching package — provider-agnostic prompt cache management.

Public API:
    ContextCacheManager  — facade used by all call sites
    make_cache_manager   — factory: returns the right manager for a given provider_id
    CacheMetrics         — structured hit/miss/token/cost statistics
    CacheState           — fingerprint registry + invalidation tracking
    ContextFingerprinter — deterministic SHA-256 hashing per segment
"""

from velune.context.cache.fingerprint import ContextFingerprinter
from velune.context.cache.manager import ContextCacheManager, make_cache_manager
from velune.context.cache.metrics import CacheMetrics
from velune.context.cache.state import CacheState

__all__ = [
    "ContextCacheManager",
    "make_cache_manager",
    "CacheMetrics",
    "CacheState",
    "ContextFingerprinter",
]
