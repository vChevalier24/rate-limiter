# stderr_rate_limiter
"""A logging.Handler wrapper backed by a token-bucket rate limiter.

Only the names re-exported here are part of the public API.
"""

from .core import RateLimitedHandler, TokenBucket

__all__ = ["RateLimitedHandler", "TokenBucket"]
