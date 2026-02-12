import asyncio
import os
import time
import uuid
import yaml
from datetime import timezone
from telethon import TelegramClient, events

from transforms import Msg
from rule_engine import apply_transforms
from qq_onebot import OneBotClient, OneBotRequestError
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


def _meta_from_resp(resp: dict | None) -> tuple[int, int]:
    if not isinstance(resp, dict):
        return 1, -1
    meta = resp.get("_meta", {}) if isinstance(resp.get("_meta", {}), dict) else {}
    attempts = int(meta.get("attempts", 1))
    duration_ms = int(meta.get("duration_ms", -1))
    return attempts, duration_ms


def _meta_from_exc(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, OneBotRequestError):
        return max(int(exc.attempts or 1), 1), exc.error_type
    return 1, exc.__class__.__name__


def _log_send_result(
    log: logging.Logger,
    *,
    chat_id: int,
    msg_id: int,
    group_id: int,
    send_mode: str,
    attempt: int,
    result: str,
    error_type: str,
    duration_ms: int = -1,
) -> None:
    log.info(
        "send_result chat_id=%s msg_id=%s group_id=%s send_mode=%s attempt=%s result=%s error_type=%s duration_ms=%s",
        chat_id,
        msg_id,
        group_id,
        send_mode,
        attempt,
        result,
        error_type,
        duration_ms,
    )


def _normalize_sources(raw_sources) -> tuple[list, int]:
    """Normalize telegram.sources and drop invalid entries like None/blank."""
    if not isinstance(raw_sources, list):
        raise ValueError("telegram.sources must be a list")

    normalized = []
    skipped = 0

    for item in raw_sources:
        if item is None:
            skipped += 1
            continue

        # Telethon supports both username/link(string) and numeric peer id.
        if isinstance(item, bool):
            skipped += 1
            continue

        if isinstance(item, int):
            normalized.append(item)
            continue

        if isinstance(item, str):
            s = item.strip()
            if not s or s.startswith("#"):
                skipped += 1
                continue
            normalized.append(s)
            continue

        skipped += 1

    return normalized, skipped


class SendIntervalLimiter:
    """Ensure a minimum interval between message send starts."""

    def __init__(self, min_interval_seconds: float):
        self.min_interval_seconds = max(float(min_interval_seconds or 0), 0.0)
        self._lock = asyncio.Lock()
        self._next_allowed_at = 0.0

    async def wait_for_slot(self) -> float:
        """Wait until next allowed send time and return waited seconds."""
        if self.min_interval_seconds <= 0:
            return 0.0

        async with self._lock:
            now = time.monotonic()
            wait_seconds = max(self._next_allowed_at - now, 0.0)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
                now = time.monotonic()

            self._next_allowed_at = now + self.min_interval_seconds
            return wait_seconds


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
    qq_retry_cfg = qq_cfg.get("retry", {})
    raw_sources = tg_cfg.get("sources", [])
    sources, skipped_sources = _normalize_sources(raw_sources)
    if not sources:
        raise ValueError(
            "telegram.sources has no valid entries (check for blank/null items like '- #...')"
        )

    # 目标群：必须配置 qq.group_ids（非空列表）
    group_ids_raw = qq_cfg.get("group_ids")
    if not isinstance(group_ids_raw, list) or not group_ids_raw:
        raise ValueError("qq.group_ids must be a non-empty list (e.g. [963115963])")
    group_ids = [int(x) for x in group_ids_raw]

    retry_policy = {
        "enabled": bool(qq_retry_cfg.get("enabled", True)),
        "max_attempts": int(qq_retry_cfg.get("max_attempts", 3) or 3),
        "base_delay_ms": int(qq_retry_cfg.get("base_delay_ms", 500) or 500),
        "max_delay_ms": int(qq_retry_cfg.get("max_delay_ms", 5000) or 5000),
        "jitter_ms": int(qq_retry_cfg.get("jitter_ms", 200) or 200),
    }
    request_timeout_seconds = float(qq_cfg.get("request_timeout_seconds", 60) or 60)
    send_interval_seconds = max(float(qq_cfg.get("send_interval_seconds", 10) or 0), 0.0)

    # 去重：sqlite 持久化
    dedup_enabled = bool(dedup_cfg.get("enabled", True))
    dedup_db_path = str(dedup_cfg.get("db_path", "/session/dedup.sqlite3"))
    dedup_ttl_seconds = int(dedup_cfg.get("ttl_seconds", 0) or 0)
    dedup_mark_on = str(dedup_cfg.get("mark_on", "success") or "success").strip().lower()
    if dedup_mark_on not in {"success", "receive"}:
        log.warning("invalid dedup.mark_on=%s, fallback to success", dedup_mark_on)
        dedup_mark_on = "success"

    log.info(
        "starting... onebot_base_url=%s source_count=%s skipped_sources=%s group_count=%s retry_enabled=%s retry_max_attempts=%s request_timeout_seconds=%s send_interval_seconds=%s dedup_enabled=%s dedup_mark_on=%s dedup_ttl_seconds=%s",
        qq_cfg.get("onebot_base_url"),
        len(sources),
        skipped_sources,
        len(group_ids),
        retry_policy["enabled"],
        retry_policy["max_attempts"],
        request_timeout_seconds,
        send_interval_seconds,
        dedup_enabled,
        dedup_mark_on,
        dedup_ttl_seconds,
    )
    if skipped_sources > 0:
        log.warning("ignored invalid telegram.sources entries: %s", skipped_sources)

    # 1) 初始化 OneBot 客户端 & 获取当前登录 QQ 的 uin（forward node 要用）
    ob = OneBotClient(
        qq_cfg["onebot_base_url"],
        qq_cfg["token"],
        retry=retry_policy,
        request_timeout_seconds=request_timeout_seconds,
    )
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
    entities = []
    resolved_sources = []
    resolve_failed = 0
    for src in sources:
        try:
            ent = await client.get_entity(src)
            entities.append(ent)
            resolved_sources.append(str(src))
        except Exception as e:
            resolve_failed += 1
            log.warning(
                "skip unresolved source: source=%r error_type=%s",
                src,
                e.__class__.__name__,
            )

    if resolve_failed > 0:
        log.warning("sources resolved with failures: failed=%s total=%s", resolve_failed, len(sources))
    if not entities:
        raise RuntimeError("no resolvable telegram.sources left after validation")
    log.info("sources resolved:{ %s }", ",".join(resolved_sources))

    send_limiter = SendIntervalLimiter(send_interval_seconds)

    # 4) 存储路径
    host_media_dir = storage["host_media_dir_in_container"]
    os.makedirs(host_media_dir, exist_ok=True)
    napcat_media_dir = storage["napcat_media_dir_in_container"]
    forward_name = "TG转发"

    dedup = DedupStore(dedup_db_path, ttl_seconds=dedup_ttl_seconds) if dedup_enabled else None
    if dedup is not None:
        try:
            dedup.prune()
            log.info(
                "dedup enabled: db=%s ttl_seconds=%s mark_on=%s",
                dedup_db_path,
                dedup_ttl_seconds,
                dedup_mark_on,
            )
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
        dkey = f"tg:{int(ev.chat_id)}:{int(message.id)}"

        # 去重 key（chat_id + msg_id）
        if dedup is not None:
            try:
                if dedup.seen(dkey):
                    log.debug("dedup drop: %s", dkey)
                    return
                if dedup_mark_on == "receive":
                    dedup.mark(dkey)
                    log.debug("dedup marked on receive: %s", dkey)
            except Exception:
                log.exception("dedup pre-check failed (ignored)")

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
        any_send_success = False

        waited_seconds = await send_limiter.wait_for_slot()
        if waited_seconds > 0:
            log.info(
                "send_delay_applied chat_id=%s msg_id=%s waited_seconds=%.2f min_interval_seconds=%.2f",
                ev.chat_id,
                message.id,
                waited_seconds,
                send_interval_seconds,
            )

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
                            resp = await ob.send_group_image_text(
                                group_id=gid,
                                image_file=image_file,
                                text=final_text,
                            )
                            attempts, duration_ms = _meta_from_resp(resp)
                            any_send_success = True
                            _log_send_result(
                                log,
                                chat_id=int(ev.chat_id),
                                msg_id=int(message.id),
                                group_id=gid,
                                send_mode="image_text",
                                attempt=attempts,
                                result="success",
                                error_type="-",
                                duration_ms=duration_ms,
                            )
                            continue
                        except Exception as e:
                            attempts, error_type = _meta_from_exc(e)
                            _log_send_result(
                                log,
                                chat_id=int(ev.chat_id),
                                msg_id=int(message.id),
                                group_id=gid,
                                send_mode="image_text",
                                attempt=attempts,
                                result="failed",
                                error_type=error_type,
                            )
                            try:
                                resp = await ob.send_group_forward(
                                    group_id=gid,
                                    uin=bot_uin,
                                    name=forward_name,
                                    image_file=image_file,
                                    text=final_text,
                                )
                                attempts, duration_ms = _meta_from_resp(resp)
                                any_send_success = True
                                _log_send_result(
                                    log,
                                    chat_id=int(ev.chat_id),
                                    msg_id=int(message.id),
                                    group_id=gid,
                                    send_mode="forward_fallback",
                                    attempt=attempts,
                                    result="success",
                                    error_type="-",
                                    duration_ms=duration_ms,
                                )
                            except Exception as e2:
                                attempts, error_type = _meta_from_exc(e2)
                                _log_send_result(
                                    log,
                                    chat_id=int(ev.chat_id),
                                    msg_id=int(message.id),
                                    group_id=gid,
                                    send_mode="forward_fallback",
                                    attempt=attempts,
                                    result="failed",
                                    error_type=error_type,
                                )
                    if dedup is not None and dedup_mark_on == "success":
                        if any_send_success:
                            try:
                                dedup.mark(dkey)
                                log.debug("dedup marked on success: %s", dkey)
                            except Exception:
                                log.exception("dedup mark-on-success failed (ignored)")
                        else:
                            log.warning("all sends failed, dedup not marked: %s", dkey)
                    return
                except Exception:
                    log.exception("prepare image message failed")
                    return

        # 无图：多群发送，单群失败不影响其它群
        for gid in group_ids:
            try:
                resp = await ob.send_group_text(gid, final_text)
                attempts, duration_ms = _meta_from_resp(resp)
                any_send_success = True
                _log_send_result(
                    log,
                    chat_id=int(ev.chat_id),
                    msg_id=int(message.id),
                    group_id=gid,
                    send_mode="text",
                    attempt=attempts,
                    result="success",
                    error_type="-",
                    duration_ms=duration_ms,
                )
            except Exception as e:
                attempts, error_type = _meta_from_exc(e)
                _log_send_result(
                    log,
                    chat_id=int(ev.chat_id),
                    msg_id=int(message.id),
                    group_id=gid,
                    send_mode="text",
                    attempt=attempts,
                    result="failed",
                    error_type=error_type,
                )

        if dedup is not None and dedup_mark_on == "success":
            if any_send_success:
                try:
                    dedup.mark(dkey)
                    log.debug("dedup marked on success: %s", dkey)
                except Exception:
                    log.exception("dedup mark-on-success failed (ignored)")
            else:
                log.warning("all sends failed, dedup not marked: %s", dkey)

    log.info("tg2qq forwarder running (listening)")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
