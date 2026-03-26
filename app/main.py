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


def _meta_from_exc(exc: Exception) -> tuple[int, str, str]:
    """Return (attempts, error_type, detail) from an exception."""
    if isinstance(exc, OneBotRequestError):
        detail = (exc.detail or str(exc))[:500]
        return max(int(exc.attempts or 1), 1), exc.error_type, detail
    return 1, exc.__class__.__name__, str(exc)[:500]


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
    error_detail: str = "",
    duration_ms: int = -1,
) -> None:
    emoji = "✅" if result == "success" else "❌"
    log.info(
        "%s 发送结果: chat_id=%s msg_id=%s 群=%s 模式=%s 尝试=%s 结果=%s 错误=%s 耗时=%sms 详情=%s",
        emoji,
        chat_id,
        msg_id,
        group_id,
        send_mode,
        attempt,
        result,
        error_type,
        duration_ms,
        error_detail or "-",
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
        "🚀 启动中... OneBot地址=%s TG来源数=%s 跳过来源=%s 目标群数=%s 重试=%s 最大重试=%s 超时=%ss 发送间隔=%ss 去重=%s 去重时机=%s TTL=%ss",
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
        log.warning("⚠️ 忽略了 %s 个无效的 TG 来源配置", skipped_sources)

    # 1) 初始化 OneBot 客户端 & 获取当前登录 QQ 的 uin（forward node 要用）
    ob = OneBotClient(
        qq_cfg["onebot_base_url"],
        qq_cfg["token"],
        retry=retry_policy,
        request_timeout_seconds=request_timeout_seconds,
    )
    try:
        bot_uin = await ob.get_login_uin()
        log.info("✅ OneBot 连接成功: QQ号=%s", bot_uin)
    except Exception:
        log.exception("❌ OneBot 连接失败 (检查 base_url/token 和 NapCat HTTP 服务)")
        raise

    # 2) 初始化 Telegram 客户端（Telethon）
    client = TelegramClient(tg_cfg["session_path"], tg_cfg["api_id"], tg_cfg["api_hash"])
    try:
        await client.start()
        me = await client.get_me()
        log.info(
            "✅ Telegram 连接成功: ID=%s 用户名=%s 手机=%s",
            getattr(me, "id", None),
            getattr(me, "username", None),
            getattr(me, "phone", None),
        )
    except Exception:
        log.exception("❌ Telegram 启动失败 (检查 api_id/api_hash/session 和交互式登录)")
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
                "⚠️ 跳过无法解析的来源: %r 错误=%s",
                src,
                e.__class__.__name__,
            )

    if resolve_failed > 0:
        log.warning("⚠️ 来源解析完成但有失败: 失败=%s 总数=%s", resolve_failed, len(sources))
    if not entities:
        raise RuntimeError("❌ 没有可用的 TG 来源")
    log.info("✅ TG 来源已解析: %s", ", ".join(resolved_sources))

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
                "✅ 去重已启用: 数据库=%s TTL=%ss 标记时机=%s",
                dedup_db_path,
                dedup_ttl_seconds,
                dedup_mark_on,
            )
        except Exception:
            log.exception("❌ 去重清理失败")

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
            "🗑️ 媒体清理已启用: 目录=%s 保留=%s天 间隔=%s小时",
            mr.dir_in_container,
            mr.keep_days,
            mr.interval_hours,
        )
    except Exception:
        log.exception("⚠️ 媒体清理任务启动失败 (已忽略)")

    @client.on(events.NewMessage(chats=entities))
    async def handler(ev: events.NewMessage.Event):
        message = ev.message
        dkey = f"tg:{int(ev.chat_id)}:{int(message.id)}"

        # 去重 key（chat_id + msg_id）
        if dedup is not None:
            try:
                if dedup.seen(dkey):
                    log.debug("🔁 去重跳过: %s", dkey)
                    return
                if dedup_mark_on == "receive":
                    dedup.mark(dkey)
                    log.debug("✅ 去重已标记(接收时): %s", dkey)
            except Exception:
                log.exception("⚠️ 去重预检查失败 (已忽略)")

        raw_text = (message.message or "").strip()

        # 频道信息
        try:
            chat = await ev.get_chat()
            chat_title = getattr(chat, "title", None) or getattr(chat, "username", None) or "TG"
        except Exception:
            chat_title = "TG"

        log.info(
            "📨 收到消息: 频道=%s chat_id=%s msg_id=%s 有图=%s 文本长度=%s",
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
            log.exception("❌ 文本清洗失败")
            return

        if m is None:
            log.info("🚫 消息被规则过滤: chat_id=%s msg_id=%s", ev.chat_id, message.id)
            return

        # 仅发送清洗后的正文（不加 [TG:xxx] / 时间头）
        final_text = (m.text or "").strip()
        any_send_success = False

        waited_seconds = await send_limiter.wait_for_slot()
        if waited_seconds > 0:
            log.info(
                "⏱️ 发送延迟: chat_id=%s msg_id=%s 等待=%.2fs 最小间隔=%.2fs",
                ev.chat_id,
                message.id,
                waited_seconds,
                send_interval_seconds,
            )

        if message.photo:
            try:
                img_bytes = await client.download_media(message.photo, bytes)
            except Exception:
                log.exception("❌ 图片下载失败")
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
                            attempts, error_type, error_detail = _meta_from_exc(e)
                            _log_send_result(
                                log,
                                chat_id=int(ev.chat_id),
                                msg_id=int(message.id),
                                group_id=gid,
                                send_mode="image_text",
                                attempt=attempts,
                                result="failed",
                                error_type=error_type,
                                error_detail=error_detail,
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
                                attempts, error_type, error_detail = _meta_from_exc(e2)
                                _log_send_result(
                                    log,
                                    chat_id=int(ev.chat_id),
                                    msg_id=int(message.id),
                                    group_id=gid,
                                    send_mode="forward_fallback",
                                    attempt=attempts,
                                    result="failed",
                                    error_type=error_type,
                                    error_detail=error_detail,
                                )
                    if dedup is not None and dedup_mark_on == "success":
                        if any_send_success:
                            try:
                                dedup.mark(dkey)
                                log.debug("✅ 去重已标记: %s", dkey)
                            except Exception:
                                log.exception("⚠️ 去重标记失败 (已忽略)")
                        else:
                            log.warning("⚠️ 所有群发送失败，未标记去重: %s", dkey)
                    return
                except Exception:
                    log.exception("❌ 图片消息准备失败")
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
                attempts, error_type, error_detail = _meta_from_exc(e)
                _log_send_result(
                    log,
                    chat_id=int(ev.chat_id),
                    msg_id=int(message.id),
                    group_id=gid,
                    send_mode="text",
                    attempt=attempts,
                    result="failed",
                    error_type=error_type,
                    error_detail=error_detail,
                )

        if dedup is not None and dedup_mark_on == "success":
            if any_send_success:
                try:
                    dedup.mark(dkey)
                    log.debug("✅ 去重已标记: %s", dkey)
                except Exception:
                    log.exception("⚠️ 去重标记失败 (已忽略)")
            else:
                log.warning("⚠️ 所有群发送失败，未标记去重: %s", dkey)

    log.info("🎧 TG→QQ 转发器运行中 (监听消息)")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
