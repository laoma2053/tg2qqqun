import asyncio
import os
import uuid
import yaml
from datetime import timezone
from telethon import TelegramClient, events

from transforms import Msg
from rule_engine import apply_transforms
from qq_onebot import OneBotClient
from dedup_store import DedupStore
from media_cleanup import MediaRetentionConfig, cleanup_loop

# 新增：标准日志
import logging
import sys


def load_cfg() -> dict:
    """
    配置文件由 docker-compose 把宿主机 ./config 映射到容器 /config
    """
    with open("/config/config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fmt_date(dt) -> str:
    """
    统一使用 UTC 时间展示（你也可以按需改成 Asia/Shanghai 等）
    """
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def setup_logging(cfg: dict) -> None:
    """初始化 stdout 日志，确保 docker logs 可见。"""
    level_name = str(cfg.get("logging", {}).get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)

    # 降噪：telethon/httpx 在 INFO 会比较多
    logging.getLogger("telethon").setLevel(max(level, logging.WARNING))
    logging.getLogger("httpx").setLevel(max(level, logging.WARNING))


async def main():
    cfg = load_cfg()
    setup_logging(cfg)
    log = logging.getLogger("tg2qq")

    tg_cfg = cfg["telegram"]
    qq_cfg = cfg["qq"]
    storage = cfg["storage"]
    rules = cfg.get("rules", {})
    dedup_cfg = cfg.get("dedup", {})
    retention_cfg = cfg.get("media_retention", {})

    # 目标群：必须配置 qq.group_ids（非空列表）
    group_ids_raw = qq_cfg.get("group_ids")
    if not isinstance(group_ids_raw, list) or not group_ids_raw:
        raise ValueError("qq.group_ids must be a non-empty list (e.g. [963115963])")
    group_ids = [int(x) for x in group_ids_raw]

    log.info(
        "starting... sources=%s groups=%s onebot_base_url=%s",
        tg_cfg.get("sources"),
        group_ids,
        qq_cfg.get("onebot_base_url"),
    )

    # 1) 初始化 OneBot 客户端 & 获取当前登录 QQ 的 uin（forward node 要用）
    ob = OneBotClient(qq_cfg["onebot_base_url"], qq_cfg["token"])
    try:
        bot_uin = await ob.get_login_uin()
        log.info("onebot ok: bot_uin=%s", bot_uin)
    except Exception:
        log.exception("onebot get_login_info failed (check base_url/token and napcat http server)")
        raise

    # 2) 初始化 Telegram 客户端（Telethon）
    client = TelegramClient(tg_cfg["session_path"], tg_cfg["api_id"], tg_cfg["api_hash"])
    try:
        await client.start()
        me = await client.get_me()
        log.info(
            "telegram ok: me_id=%s username=%s phone=%s",
            getattr(me, "id", None),
            getattr(me, "username", None),
            getattr(me, "phone", None),
        )
    except Exception:
        log.exception("telegram start failed (check api_id/api_hash/session and interactive login)")
        raise

    # 3) 解析 sources 为 entity，避免每条消息重复 resolve
    try:
        entities = [await client.get_entity(x) for x in tg_cfg["sources"]]
        log.info("sources resolved: %s", [getattr(e, "id", None) for e in entities])
    except Exception:
        log.exception("failed to resolve sources (check sources value or bot account membership)")
        raise

    # 4) 存储路径
    host_media_dir = storage["host_media_dir_in_container"]
    os.makedirs(host_media_dir, exist_ok=True)
    napcat_media_dir = storage["napcat_media_dir_in_container"]
    forward_name = "TG转发"

    # 去重：sqlite 持久化
    dedup_enabled = bool(dedup_cfg.get("enabled", True))
    dedup_db_path = str(dedup_cfg.get("db_path", "/session/dedup.sqlite3"))
    dedup_ttl_seconds = int(dedup_cfg.get("ttl_seconds", 0) or 0)
    dedup = DedupStore(dedup_db_path, ttl_seconds=dedup_ttl_seconds) if dedup_enabled else None
    if dedup is not None:
        try:
            dedup.prune()
            log.info("dedup enabled: db=%s ttl_seconds=%s", dedup_db_path, dedup_ttl_seconds)
        except Exception:
            log.exception("dedup prune failed")

    # 媒体清理：定期删除过期图片
    try:
        mr = MediaRetentionConfig(
            enabled=bool(retention_cfg.get("enabled", True)),
            dir_in_container=str(
                retention_cfg.get(
                    "dir_in_container",
                    storage.get("host_media_dir_in_container", "/host_tg_media"),
                )
            ),
            keep_days=int(retention_cfg.get("keep_days", 30)),
            interval_hours=int(retention_cfg.get("interval_hours", 24)),
        )
        asyncio.create_task(cleanup_loop(mr))
        log.info(
            "media_retention enabled=%s dir=%s keep_days=%s interval_hours=%s",
            mr.enabled,
            mr.dir_in_container,
            mr.keep_days,
            mr.interval_hours,
        )
    except Exception:
        log.exception("media_retention task start failed (ignored)")

    @client.on(events.NewMessage(chats=entities))
    async def handler(ev: events.NewMessage.Event):
        message = ev.message

        # 去重 key（chat_id + msg_id）
        if dedup is not None:
            try:
                dkey = f"tg:{int(ev.chat_id)}:{int(message.id)}"
                if dedup.seen_or_mark(dkey):
                    log.debug("dedup drop: %s", dkey)
                    return
            except Exception:
                log.exception("dedup seen_or_mark failed (ignored)")

        raw_text = (message.message or "").strip()

        # 频道信息
        try:
            chat = await ev.get_chat()
            chat_title = getattr(chat, "title", None) or getattr(chat, "username", None) or "TG"
        except Exception:
            chat_title = "TG"

        log.info(
            "incoming: chat=%s chat_id=%s msg_id=%s has_photo=%s text_len=%s",
            chat_title,
            ev.chat_id,
            message.id,
            bool(message.photo),
            len(raw_text),
        )

        m = Msg(
            chat=chat_title,
            chat_id=ev.chat_id,
            msg_id=message.id,
            date=fmt_date(message.date),
            text=raw_text,
        )

        try:
            m = apply_transforms(m, rules)
        except Exception:
            log.exception("apply_transforms failed")
            return

        if m is None:
            log.info("dropped by rules: chat_id=%s msg_id=%s", ev.chat_id, message.id)
            return

        # 仅发送清洗后的正文（不加 [TG:xxx] / 时间头）
        final_text = (m.text or "").strip()

        if message.photo:
            try:
                img_bytes = await client.download_media(message.photo, bytes)
            except Exception:
                log.exception("download photo failed")
                img_bytes = None

            if img_bytes:
                try:
                    filename = f"{uuid.uuid4().hex}.jpg"
                    host_path = os.path.join(host_media_dir, filename)
                    with open(host_path, "wb") as f:
                        f.write(img_bytes)

                    image_file = "file://" + os.path.join(napcat_media_dir, filename)

                    # 多群发送：优先普通图文消息；失败则降级为合并转发（更稳）
                    for gid in group_ids:
                        try:
                            await ob.send_group_image_text(
                                group_id=gid,
                                image_file=image_file,
                                text=final_text,
                            )
                            log.info("sent image+text: group_id=%s file=%s", gid, image_file)
                        except Exception:
                            log.exception("send image+text failed (fallback to forward): group_id=%s", gid)
                            try:
                                await ob.send_group_forward(
                                    group_id=gid,
                                    uin=bot_uin,
                                    name=forward_name,
                                    image_file=image_file,
                                    text=final_text,
                                )
                                log.info("sent forward (fallback): group_id=%s file=%s", gid, image_file)
                            except Exception:
                                log.exception("send forward failed: group_id=%s", gid)
                    return
                except Exception:
                    log.exception("prepare image message failed")
                    return

        # 无图：多群发送，单群失败不影响其它群
        for gid in group_ids:
            try:
                await ob.send_group_text(gid, final_text)
                log.info("sent text: group_id=%s", gid)
            except Exception:
                log.exception("send text failed: group_id=%s", gid)

    log.info("tg2qq forwarder running (listening)")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
