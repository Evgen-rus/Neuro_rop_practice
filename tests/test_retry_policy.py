from __future__ import annotations

import asyncio
import unittest

from reliability.retry import RetryPolicy, is_transient_error, run_with_retry, run_with_retry_async


class FakeHttpError(RuntimeError):
    def __init__(self, status_code: int, message: str = "failure") -> None:
        super().__init__(message)
        self.status_code = status_code


class RetryPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = RetryPolicy(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0, jitter_ratio=0)

    def test_transient_failure_retries_then_succeeds(self) -> None:
        calls = 0
        events: list[dict] = []

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise FakeHttpError(503)
            return "ok"

        result = run_with_retry(
            operation,
            operation_name="test",
            policy=self.policy,
            on_event=events.append,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(calls, 3)
        self.assertEqual([event["status"] for event in events], ["attempt", "retry_wait", "attempt", "retry_wait", "attempt", "success"])

    def test_permanent_client_error_is_not_retried(self) -> None:
        calls = 0

        def operation() -> None:
            nonlocal calls
            calls += 1
            raise FakeHttpError(400)

        with self.assertRaises(FakeHttpError):
            run_with_retry(operation, operation_name="test", policy=self.policy)
        self.assertEqual(calls, 1)

    def test_rate_limit_is_transient(self) -> None:
        self.assertTrue(is_transient_error(FakeHttpError(429)))
        self.assertFalse(is_transient_error(FakeHttpError(401)))

    def test_async_retry_uses_same_policy(self) -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("temporary")
            return "ok"

        async def no_sleep(_delay: float) -> None:
            return None

        result = asyncio.run(
            run_with_retry_async(
                operation,
                operation_name="async-test",
                policy=self.policy,
                sleep=no_sleep,
            )
        )
        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
