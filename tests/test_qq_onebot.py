import pathlib
import sys
import unittest

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

from qq_onebot import OneBotClient, OneBotRequestError


def _mk_response(status_code: int, data: dict) -> httpx.Response:
    req = httpx.Request("POST", "http://127.0.0.1/send_group_msg")
    return httpx.Response(status_code=status_code, json=data, request=req)


class StubOneBotClient(OneBotClient):
    def __init__(self, outcomes, **kwargs):
        super().__init__(
            base_url="http://127.0.0.1:3000",
            token="t",
            **kwargs,
        )
        self._outcomes = list(outcomes)
        self.calls = 0

    async def _post_json(self, url: str, payload: dict):  # noqa: ARG002
        if self.calls >= len(self._outcomes):
            raise RuntimeError("no stub outcome left")
        out = self._outcomes[self.calls]
        self.calls += 1
        if isinstance(out, Exception):
            raise out
        return out


class OneBotRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recoverable_error_then_success(self):
        client = StubOneBotClient(
            outcomes=[
                httpx.ReadTimeout("timeout"),
                _mk_response(200, {"status": "ok", "data": {"message_id": 1}}),
            ],
            retry={
                "enabled": True,
                "max_attempts": 3,
                "base_delay_ms": 0,
                "max_delay_ms": 0,
                "jitter_ms": 0,
            },
            request_timeout_seconds=1,
        )

        resp = await client.call("send_group_msg", {"group_id": 1})
        self.assertEqual(client.calls, 2)
        self.assertEqual(resp["_meta"]["attempts"], 2)
        self.assertGreaterEqual(resp["_meta"]["duration_ms"], 0)

    async def test_nonrecoverable_http_4xx_no_retry(self):
        client = StubOneBotClient(
            outcomes=[_mk_response(400, {"status": "failed", "retcode": 100})],
            retry={
                "enabled": True,
                "max_attempts": 3,
                "base_delay_ms": 0,
                "max_delay_ms": 0,
                "jitter_ms": 0,
            },
            request_timeout_seconds=1,
        )

        with self.assertRaises(OneBotRequestError) as ctx:
            await client.call("send_group_msg", {"group_id": 1})
        self.assertEqual(client.calls, 1)
        self.assertEqual(ctx.exception.error_type, "http_400")
        self.assertFalse(ctx.exception.recoverable)
        self.assertEqual(ctx.exception.attempts, 1)

    async def test_retry_exhausted_on_connect_error(self):
        req = httpx.Request("POST", "http://127.0.0.1/send_group_msg")
        client = StubOneBotClient(
            outcomes=[
                httpx.ConnectError("connect failed", request=req),
                httpx.ConnectError("connect failed", request=req),
                httpx.ConnectError("connect failed", request=req),
            ],
            retry={
                "enabled": True,
                "max_attempts": 3,
                "base_delay_ms": 0,
                "max_delay_ms": 0,
                "jitter_ms": 0,
            },
            request_timeout_seconds=1,
        )

        with self.assertRaises(OneBotRequestError) as ctx:
            await client.call("send_group_msg", {"group_id": 1})
        self.assertEqual(client.calls, 3)
        self.assertEqual(ctx.exception.error_type, "connect_error")
        self.assertTrue(ctx.exception.recoverable)
        self.assertEqual(ctx.exception.attempts, 3)

    async def test_onebot_business_failure_no_retry(self):
        client = StubOneBotClient(
            outcomes=[_mk_response(200, {"status": "failed", "retcode": 100, "data": None})],
            retry={
                "enabled": True,
                "max_attempts": 3,
                "base_delay_ms": 0,
                "max_delay_ms": 0,
                "jitter_ms": 0,
            },
            request_timeout_seconds=1,
        )

        with self.assertRaises(OneBotRequestError) as ctx:
            await client.call("send_group_msg", {"group_id": 1})
        self.assertEqual(client.calls, 1)
        self.assertEqual(ctx.exception.error_type, "onebot_status")
        self.assertFalse(ctx.exception.recoverable)
        self.assertEqual(ctx.exception.attempts, 1)


if __name__ == "__main__":
    unittest.main()
