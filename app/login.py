import asyncio
import yaml
from telethon import TelegramClient


def load_cfg() -> dict:
    with open("/config/config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def main():
    cfg = load_cfg()
    tg_cfg = cfg["telegram"]

    session_path = tg_cfg["session_path"]
    api_id = tg_cfg["api_id"]
    api_hash = tg_cfg["api_hash"]

    client = TelegramClient(session_path, api_id, api_hash)

    # start() will prompt for phone/code/password on first run
    await client.start()

    me = await client.get_me()
    who = getattr(me, "username", None) or getattr(me, "first_name", None) or str(getattr(me, "id", ""))
    print(f"Telegram login success: {who}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
