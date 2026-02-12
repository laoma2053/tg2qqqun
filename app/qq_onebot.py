import asyncio
import logging
import random
import time
from typing import Any

import httpx


class OneBotRequestError(RuntimeError):
    """Structured OneBot request failure."""

    def __init__(
        self,
        message: str,
        *,
        action: str,
        attempts: int,
        error_type: str,
        recoverable: bool,
        status_code: int | None = None,
        detail: str | None = None,
    ):
        super().__init__(message)
        self.action = action
        self.attempts = attempts
        self.error_type = error_type
        self.recoverable = recoverable
        self.status_code = status_code
        self.detail = detail

class OneBotClient:
    """
    NapCat OneBot v11 HTTP 客户端。
    统一用 POST /<action> 的方式调用。
    返回值按 OneBot 常见结构：
      {"status":"ok","retcode":0,"data":...}
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        retry: dict[str, Any] | None = None,
        request_timeout_seconds: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        # NapCat 你已验证：Authorization: Bearer <token> 可用
        self.headers = {"Authorization": f"Bearer {token}"}
        self.log = logging.getLogger("tg2qq.onebot")

        retry = retry or {}
        self.retry_enabled = bool(retry.get("enabled", True))
        self.max_attempts = max(int(retry.get("max_attempts", 3) or 1), 1)
        self.base_delay_ms = max(int(retry.get("base_delay_ms", 500) or 0), 0)
        self.max_delay_ms = max(int(retry.get("max_delay_ms", 5000) or 0), 0)
        self.jitter_ms = max(int(retry.get("jitter_ms", 200) or 0), 0)
        self.request_timeout_seconds = max(float(request_timeout_seconds or 60.0), 1.0)

    async def call(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        调用 OneBot action。
        - action: 例如 "send_group_msg"
        - payload: JSON body
        """
        payload = payload or {}
        total_start = time.monotonic()
        attempts = self.max_attempts if self.retry_enabled else 1
        last_error: OneBotRequestError | None = None

        for attempt in range(1, attempts + 1):
            try:
                data = await self._call_once(action, payload, attempt=attempt)
                if isinstance(data, dict):
                    data.setdefault("_meta", {})
                    data["_meta"]["attempts"] = attempt
                    data["_meta"]["duration_ms"] = int((time.monotonic() - total_start) * 1000)
                return data
            except OneBotRequestError as e:
                e.attempts = attempt
                last_error = e
                should_retry = self.retry_enabled and e.recoverable and attempt < attempts
                if not should_retry:
                    raise

                self.log.warning(
                    "onebot retry: action=%s attempt=%s error_type=%s",
                    action,
                    attempt,
                    e.error_type,
                )
                await self._sleep_before_retry(attempt)

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"onebot call failed unexpectedly: action={action}")

    async def _post_json(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self.request_timeout_seconds) as client:
            return await client.post(url, json=payload, headers=self.headers)

    async def _call_once(self, action: str, payload: dict[str, Any], *, attempt: int) -> dict[str, Any]:
        url = f"{self.base_url}/{action.lstrip('/')}"
        try:
            resp = await self._post_json(url, payload)
        except httpx.TimeoutException as e:
            raise OneBotRequestError(
                "onebot timeout",
                action=action,
                attempts=attempt,
                error_type="timeout",
                recoverable=True,
                detail=str(e),
            ) from e
        except httpx.ConnectError as e:
            raise OneBotRequestError(
                "onebot connect error",
                action=action,
                attempts=attempt,
                error_type="connect_error",
                recoverable=True,
                detail=str(e),
            ) from e
        except httpx.TransportError as e:
            raise OneBotRequestError(
                "onebot transport error",
                action=action,
                attempts=attempt,
                error_type="transport_error",
                recoverable=True,
                detail=str(e),
            ) from e

        status = int(resp.status_code)
        if status == 429 or status >= 500:
            raise OneBotRequestError(
                "onebot upstream http failure",
                action=action,
                attempts=attempt,
                error_type=f"http_{status}",
                recoverable=True,
                status_code=status,
            )

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            status_code = int(e.response.status_code) if e.response is not None else None
            recoverable = bool(status_code == 429 or (status_code is not None and status_code >= 500))
            etype = f"http_{status_code}" if status_code is not None else "http_status_error"
            raise OneBotRequestError(
                "onebot http status error",
                action=action,
                attempts=attempt,
                error_type=etype,
                recoverable=recoverable,
                status_code=status_code,
                detail=str(e),
            ) from e

        try:
            data = resp.json()
        except ValueError as e:
            raise OneBotRequestError(
                "onebot invalid json",
                action=action,
                attempts=attempt,
                error_type="invalid_json",
                recoverable=False,
                status_code=status,
                detail=str(e),
            ) from e

        # OneBot 标准：status != ok 表示调用失败
        if data.get("status") != "ok":
            raise OneBotRequestError(
                "onebot business failure",
                action=action,
                attempts=attempt,
                error_type="onebot_status",
                recoverable=False,
                status_code=status,
                detail=str(data),
            )

        return data

    async def _sleep_before_retry(self, attempt: int) -> None:
        exp_delay_ms = self.base_delay_ms * (2 ** max(attempt - 1, 0))
        base_ms = min(exp_delay_ms, self.max_delay_ms) if self.max_delay_ms > 0 else exp_delay_ms
        jitter = random.randint(0, self.jitter_ms) if self.jitter_ms > 0 else 0
        delay_ms = max(base_ms + jitter, 0)
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    async def get_login_uin(self) -> int:
        """
        获取当前登录 QQ 的 user_id (uin)。
        forward node 里通常要填 uin，否则部分客户端显示异常。
        """
        data = await self.call("get_login_info", {})
        return int(data["data"]["user_id"])

    async def send_group_text(self, group_id: int, text: str) -> dict[str, Any]:
        """
        发送纯文本到群（消息段数组）
        """
        payload = {
            "group_id": group_id,
            "message": [
                {"type": "text", "data": {"text": text}}
            ],
        }
        return await self.call("send_group_msg", payload)

    async def send_group_image_text(self, group_id: int, image_file: str, text: str) -> dict[str, Any]:
        """发送普通图文消息到群（非合并转发）。

        - image_file: NapCat 容器内可访问的 file:// 路径
          例如：file:///AstrBot/data/tg_media/xxxx.jpg
        - OneBot v11：message 为消息段数组，按顺序拼接展示
        """
        payload = {
            "group_id": group_id,
            "message": [
                {"type": "image", "data": {"file": image_file}},
                {"type": "text", "data": {"text": text}},
            ],
        }
        return await self.call("send_group_msg", payload)

    async def send_group_forward(self, group_id: int, uin: int, name: str, image_file: str, text: str) -> dict[str, Any]:
        """
        发送合并转发消息（重点：图片在上，文字在下）

        参数：
        - image_file: 必须是 NapCat 容器内可访问的 file:// 路径
          例如：file:///AstrBot/data/tg_media/xxxx.jpg

        实现要点：
        - messages 是 node 列表
        - node 顺序决定点开后内容顺序：先图片 node，再文字 node
        """
        messages = [
            # Node 1：图片
            {
                "type": "node",
                "data": {
                    "name": name,
                    "uin": str(uin),
                    "content": [
                        {"type": "image", "data": {"file": image_file}}
                    ],
                },
            },
            # Node 2：文字
            {
                "type": "node",
                "data": {
                    "name": name,
                    "uin": str(uin),
                    "content": [
                        {"type": "text", "data": {"text": text}}
                    ],
                },
            },
        ]

        payload = {"group_id": group_id, "messages": messages}
        return await self.call("send_group_forward_msg", payload)
