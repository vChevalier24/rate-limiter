"""Token-bucket rate limiting for logging handlers.

The token bucket is a well-known algorithm: a bucket holds at most *capacity*
tokens, refilled at *rate* tokens per second. Each consumed token represents one
log record that was allowed through. When the bucket is empty, records are
dropped (or summarized) until tokens become available again.

All time is obtained through a caller-supplied ``clock`` callable returning
seconds as a float. This is not test plumbing that leaked into the API; it is
the only way to make rate-limit behaviour deterministically testable, and it is
documented as a constructor argument. A test that asserted on wall-clock time
would be a broken test, so we refuse to depend on wall-clock time internally.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Callable, List, Optional


class TokenBucket:
    """A thread-safe token bucket.

    ``capacity`` tokens can accumulate; the bucket refills at ``rate`` tokens
    per second up to that ceiling. ``consume`` removes one token if available
    and returns True, otherwise returns False without blocking.

    The bucket is lazy: it does not run a background refill thread. Refill is
    computed on each call from the elapsed time reported by ``clock``. This
    keeps the object cheap to hold when idle and makes behaviour a pure
    function of the clock values the caller supplies.
    """

    def __init__(
        self,
        rate: float,
        capacity: float,
        clock: Callable[[], float] = lambda: 0.0,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._rate = float(rate)
        self._capacity = float(capacity)
        self._clock = clock
        self._tokens = float(capacity)
        # Last clock reading we refilled against. Using the caller's clock
        # here means the first consume() sees zero elapsed time, which is the
        # correct initial condition.
        self._last = float(clock())
        self._lock = threading.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def capacity(self) -> float:
        return self._capacity

    def _refill_locked(self) -> None:
        now = float(self._clock())
        elapsed = now - self._last
        if elapsed <= 0:
            # Clock did not advance (or moved backwards). We do not refill on
            # negative elapsed time: a clock that jumps backwards should not
            # manufacture budget. We also do not raise: monotonicity is the
            # caller's responsibility, and degrading gracefully is better than
            # crashing a logging handler.
            self._last = now
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last = now

    def consume(self, n: float = 1.0) -> bool:
        """Consume ``n`` tokens; return True if granted, False if not.

        A request larger than ``capacity`` is rejected outright: the bucket can
        never satisfy it, so blocking would be the only alternative, and a
        logging handler must never block the caller.
        """
        if n <= 0:
            raise ValueError("n must be positive")
        if n > self._capacity:
            return False
        with self._lock:
            self._refill_locked()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    @property
    def tokens(self) -> float:
        """Current token count, after a lazy refill."""
        with self._lock:
            self._refill_locked()
            return self._tokens


class RateLimitedHandler(logging.Handler):
    """A ``logging.Handler`` that rate-limits emission via a token bucket.

    Each record costs one token. When the bucket is empty the record is not
    emitted; instead, a single summary line is emitted once the bucket recovers
    enough to let a record through again, stating how many records were
    suppressed. This is the trade-off: during a flood you lose individual
    messages, but you keep stderr readable and you keep the process's logging
    path cheap.

    The handler wraps another handler (``target``) rather than subclassing a
    specific one, so it works equally well with ``StreamHandler``,
    ``FileHandler``, or a custom handler. If ``target`` is omitted, a
    ``StreamHandler`` writing to ``stderr`` is used — this matches the
    library's stated purpose.

    ``clock`` is forwarded to the bucket and is the single source of time for
    rate-limit decisions.
    """

    def __init__(
        self,
        rate: float,
        capacity: float,
        target: Optional[logging.Handler] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        super().__init__()
        if clock is None:
            # time.monotonic is the right default: it is immune to system
            # clock adjustments, which is exactly what a rate limiter needs.
            # We import lazily so the module import is side-effect free for
            # environments that patch time.
            import time

            clock = time.monotonic
        self._bucket = TokenBucket(rate, capacity, clock=clock)
        self._target: logging.Handler = target or logging.StreamHandler(sys.stderr)
        self._dropped = 0
        self._lock = threading.Lock()

    @property
    def bucket(self) -> TokenBucket:
        """The underlying bucket. Exposed for inspection and tests."""
        return self._bucket

    @property
    def target(self) -> logging.Handler:
        return self._target

    def emit(self, record: logging.LogRecord) -> None:
        """Emit ``record`` if a token is available, otherwise count it dropped.

        We do not acquire ``self.lock`` (the Handler-level lock) around the
        bucket call because the bucket has its own lock; acquiring both would
        be redundant and would order locks inconsistently with ``close``.
        """
        if self._bucket.consume(1.0):
            suppressed = self._take_suppressed()
            if suppressed > 0:
                self._emit_summary(suppressed)
            self._target.emit(record)
        else:
            with self._lock:
                self._dropped += 1

    def _take_suppressed(self) -> int:
        with self._lock:
            dropped = self._dropped
            self._dropped = 0
            return dropped

    def _emit_summary(self, suppressed: int) -> None:
        """Emit one line describing how many records were suppressed.

        We synthesize a LogRecord so the summary passes through the target
        handler's normal formatting and level filtering. The level is WARNING
        so it is visible even if the flood was at INFO or below.
        """
        msg = "stderr_rate_limiter: %d record(s) suppressed by rate limit"
        summary = logging.LogRecord(
            name="stderr_rate_limiter",
            level=logging.WARNING,
            pathname=__file__,
            lineno=0,
            msg=msg,
            args=(suppressed,),
            exc_info=None,
        )
        self._target.emit(summary)

    def flush(self) -> None:
        self._target.flush()

    def close(self) -> None:
        # Order: flush pending output, then hand close down to the target, then
        # mark ourselves closed via the base class. We do not emit a final
        # summary on close: close() may be called during interpreter shutdown
        # when the target handler's streams are already torn down.
        try:
            self._target.flush()
        finally:
            self._target.close()
            super().close()
