import logging
import unittest
from io import StringIO

from stderr_rate_limiter import RateLimitedHandler, TokenBucket


class FakeClock:
    """A controllable clock. Returns exactly the value set by ``tick``."""

    def __init__(self, start=0.0):
        self._t = float(start)

    def __call__(self):
        return self._t

    def tick(self, seconds):
        self._t += float(seconds)
        return self._t

    @property
    def now(self):
        return self._t


class TestTokenBucket(unittest.TestCase):
    def test_initial_capacity_available(self):
        clock = FakeClock()
        b = TokenBucket(rate=1.0, capacity=5.0, clock=clock)
        for _ in range(5):
            self.assertTrue(b.consume())
        self.assertFalse(b.consume())

    def test_refill_proportional_to_elapsed_time(self):
        clock = FakeClock()
        b = TokenBucket(rate=2.0, capacity=2.0, clock=clock)
        self.assertTrue(b.consume(2.0))
        self.assertFalse(b.consume())
        # 2 tokens/sec => 0.5 sec yields one token.
        clock.tick(0.5)
        self.assertTrue(b.consume())
        self.assertFalse(b.consume())

    def test_refill_capped_at_capacity(self):
        clock = FakeClock()
        b = TokenBucket(rate=10.0, capacity=2.0, clock=clock)
        # Drain fully.
        self.assertTrue(b.consume(2.0))
        # Wait long enough that refill would overshoot without the cap.
        clock.tick(100.0)
        self.assertEqual(b.tokens, 2.0)

    def test_consume_larger_than_capacity_rejected(self):
        clock = FakeClock()
        b = TokenBucket(rate=1.0, capacity=3.0, clock=clock)
        self.assertFalse(b.consume(4.0))

    def test_non_positive_rate_rejected(self):
        with self.assertRaises(ValueError):
            TokenBucket(rate=0, capacity=1.0, clock=FakeClock())
        with self.assertRaises(ValueError):
            TokenBucket(rate=-1.0, capacity=1.0, clock=FakeClock())

    def test_non_positive_capacity_rejected(self):
        with self.assertRaises(ValueError):
            TokenBucket(rate=1.0, capacity=0, clock=FakeClock())
        with self.assertRaises(ValueError):
            TokenBucket(rate=1.0, capacity=-1.0, clock=FakeClock())

    def test_non_positive_n_rejected(self):
        b = TokenBucket(rate=1.0, capacity=1.0, clock=FakeClock())
        with self.assertRaises(ValueError):
            b.consume(0)
        with self.assertRaises(ValueError):
            b.consume(-1.0)

    def test_clock_going_backwards_does_not_refill(self):
        clock = FakeClock(start=10.0)
        b = TokenBucket(rate=1.0, capacity=1.0, clock=clock)
        self.assertTrue(b.consume())
        # Simulate a clock jump backwards.
        clock._t = 5.0
        self.assertFalse(b.consume())

    def test_partial_consume_leaves_remainder(self):
        clock = FakeClock()
        b = TokenBucket(rate=1.0, capacity=3.0, clock=clock)
        self.assertTrue(b.consume(2.0))
        self.assertTrue(b.consume())
        self.assertFalse(b.consume())


class TestRateLimitedHandler(unittest.TestCase):
    def _make_handler(self, rate, capacity, clock, target=None):
        if target is None:
            target = logging.StreamHandler(StringIO())
        h = RateLimitedHandler(
            rate=rate, capacity=capacity, target=target, clock=clock
        )
        return h, target

    def test_records_emitted_up_to_capacity(self):
        clock = FakeClock()
        buf = StringIO()
        target = logging.StreamHandler(buf)
        target.setFormatter(logging.Formatter("%(message)s"))
        h, _ = self._make_handler(1.0, 3.0, clock, target)

        logger = logging.getLogger("test.emit_to_capacity")
        logger.addHandler(h)
        logger.setLevel(logging.DEBUG)
        for i in range(3):
            logger.info("msg %d", i)
        lines = [l for l in buf.getvalue().splitlines() if l]
        self.assertEqual(lines, ["msg 0", "msg 1", "msg 2"])

    def test_records_beyond_capacity_are_dropped(self):
        clock = FakeClock()
        buf = StringIO()
        target = logging.StreamHandler(buf)
        target.setFormatter(logging.Formatter("%(message)s"))
        h, _ = self._make_handler(1.0, 2.0, clock, target)

        logger = logging.getLogger("test.drop_beyond_capacity")
        logger.addHandler(h)
        logger.setLevel(logging.DEBUG)
        for i in range(5):
            logger.info("msg %d", i)
        lines = [l for l in buf.getvalue().splitlines() if l]
        self.assertEqual(lines, ["msg 0", "msg 1"])

    def test_summary_emitted_after_recovery(self):
        clock = FakeClock()
        buf = StringIO()
        target = logging.StreamHandler(buf)
        target.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))
        h, _ = self._make_handler(1.0, 1.0, clock, target)

        logger = logging.getLogger("test.summary_after_recovery")
        logger.addHandler(h)
        logger.setLevel(logging.DEBUG)
        logger.info("first")  # consumes the token
        logger.info("dropped1")  # suppressed
        logger.info("dropped2")  # suppressed
        # Advance time enough for one token.
        clock.tick(1.0)
        logger.info("after")  # token available; summary emitted first
        lines = [l for l in buf.getvalue().splitlines() if l]
        self.assertEqual(
            lines,
            [
                "INFO:first",
                "WARNING:stderr_rate_limiter: 2 record(s) suppressed by rate limit",
                "INFO:after",
            ],
        )

    def test_no_summary_when_nothing_suppressed(self):
        clock = FakeClock()
        buf = StringIO()
        target = logging.StreamHandler(buf)
        target.setFormatter(logging.Formatter("%(message)s"))
        h, _ = self._make_handler(1.0, 2.0, clock, target)

        logger = logging.getLogger("test.no_summary")
        logger.addHandler(h)
        logger.setLevel(logging.DEBUG)
        logger.info("a")
        logger.info("b")
        lines = [l for l in buf.getvalue().splitlines() if l]
        self.assertEqual(lines, ["a", "b"])

    def test_default_target_is_stderr_stream(self):
        # We cannot capture sys.stderr reliably across handlers, so we just
        # assert the default target is a StreamHandler whose stream is sys.stderr
        # at construction time.
        import sys

        h = RateLimitedHandler(rate=1.0, capacity=1.0, clock=FakeClock())
        self.assertIsInstance(h.target, logging.StreamHandler)
        self.assertIs(h.target.stream, sys.stderr)

    def test_target_is_used_for_emission(self):
        clock = FakeClock()
        emitted = []

        class RecordingHandler(logging.Handler):
            def emit(self, record):
                emitted.append(record.getMessage())

        h = RateLimitedHandler(
            rate=1.0, capacity=10.0, target=RecordingHandler(), clock=clock
        )
        logger = logging.getLogger("test.custom_target")
        logger.addHandler(h)
        logger.setLevel(logging.DEBUG)
        logger.warning("hello")
        self.assertEqual(emitted, ["hello"])

    def test_flush_and_close_delegate_to_target(self):
        clock = FakeClock()
        buf = StringIO()
        target = logging.StreamHandler(buf)
        h, _ = self._make_handler(1.0, 1.0, clock, target)

        logger = logging.getLogger("test.flush_close")
        logger.addHandler(h)
        logger.setLevel(logging.DEBUG)
        logger.info("x")
        h.flush()
        self.assertIn("x", buf.getvalue())
        h.close()
        # After close, emitting through the target should not raise.
        target.emit(logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__,
            lineno=0, msg="after", args=None, exc_info=None,
        ))

    def test_bucket_exposed(self):
        h = RateLimitedHandler(rate=2.0, capacity=5.0, clock=FakeClock())
        self.assertIsInstance(h.bucket, TokenBucket)
        self.assertEqual(h.bucket.rate, 2.0)
        self.assertEqual(h.bucket.capacity, 5.0)

    def test_recovery_then_flood_then_recovery(self):
        clock = FakeClock()
        buf = StringIO()
        target = logging.StreamHandler(buf)
        target.setFormatter(logging.Formatter("%(message)s"))
        h, _ = self._make_handler(2.0, 2.0, clock, target)

        logger = logging.getLogger("test.flood_cycle")
        logger.addHandler(h)
        logger.setLevel(logging.DEBUG)
        logger.info("a")  # token 1
        logger.info("b")  # token 2
        logger.info("c")  # dropped
        logger.info("d")  # dropped
        logger.info("e")  # dropped
        # 2 tokens/sec => 1 sec yields 2 tokens.
        clock.tick(1.0)
        logger.info("f")  # token; summary of 3 emitted first
        logger.info("g")  # token
        logger.info("h")  # dropped
        lines = [l for l in buf.getvalue().splitlines() if l]
        self.assertEqual(
            lines,
            [
                "a",
                "b",
                "stderr_rate_limiter: 3 record(s) suppressed by rate limit",
                "f",
                "g",
            ],
        )


if __name__ == "__main__":
    unittest.main()
