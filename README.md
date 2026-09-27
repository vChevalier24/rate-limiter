# stderr_rate_limiter

A `logging.Handler` wrapper that enforces a token-bucket rate limit so noisy
error streams do not flood stderr during incidents.

```python
import logging
from stderr_rate_limiter import RateLimitedHandler

handler = RateLimitedHandler(rate=5.0, capacity=20.0)
logger = logging.getLogger("app")
logger.addHandler(handler)
logger.setLevel(logging.INFO)

for _ in range(1000):
    logger.error("something is wrong")  # only ~20 pass immediately
```

`RateLimitedHandler(rate, capacity, target=None, clock=None)` wraps another
handler (default: a `StreamHandler` on `stderr`). Each emitted record costs one
token; the bucket refills at `rate` tokens per second up to `capacity`. When the
bucket is empty, records are dropped and a single summary line is emitted once
the bucket recovers, stating how many records were suppressed.

`TokenBucket(rate, capacity, clock=...)` is also exported for direct use. Its
`consume(n=1.0)` method returns `True` if `n` tokens were granted and `False`
otherwise (including when `n` exceeds `capacity`).

## Why this exists

During an incident a tight error loop can write thousands of identical lines to
stderr per second, drowning out everything else and burning I/O. This library
caps the emission rate and collapses the overflow into a periodic count. The
trade-off is deliberate: you lose individual messages during a flood. If you
need every record, do not use this handler — write to a file with rotation
instead.

## The awkward edge

The handler summarizes suppressed records as a `WARNING`-level synthetic
`LogRecord` emitted through the wrapped target. If the target handler has a
level filter above `WARNING`, the summary will not appear, and a flood will
look like silence. Set the target's level to `WARNING` or below.

Time is taken from a caller-supplied `clock` callable (default
`time.monotonic`). A clock that moves backwards will not manufacture budget —
the bucket simply stops refilling until the clock exceeds its previous maximum.
