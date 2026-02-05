import httpx
from typing import Any

class OneBotClient:
    """
    NapCat OneBot v11 HTTP 客户端。
    统一用 POST /<action> 的方式调用。
    返回值按 OneBot 常见结构：
      {"status":"ok","retcode":0,"data":...}
    """

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        # NapCat 你已验证：Authorization: Bearer <token> 可用
        self.headers = {"Authorization": f"Bearer {token}"}

    async def call(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        调用 OneBot action。
        - action: 例如 "send_group_msg"
        - payload: JSON body
        """
        url = f"{self.base_url}/{action.lstrip('/')}"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, json=payload or {}, headers=self.headers)
            r.raise_for_status()
            data = r.json()

        # OneBot 标准：status != ok 表示调用失败
        if data.get("status") != "ok":
            raise RuntimeError(data)

        return data

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
