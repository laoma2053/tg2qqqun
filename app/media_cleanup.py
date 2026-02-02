import os
import time
from dataclasses import dataclass


@dataclass
class MediaRetentionConfig:
    enabled: bool = True
    dir_in_container: str = "/host_tg_media"
    keep_days: int = 30
    interval_hours: int = 24


def _iter_files(root: str):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def cleanup_once(cfg: MediaRetentionConfig) -> dict:
    """Delete files older than keep_days under dir_in_container."""
    if not cfg.enabled:
        return {"enabled": False, "deleted": 0, "checked": 0}

    root = cfg.dir_in_container
    os.makedirs(root, exist_ok=True)

    now = time.time()
    cutoff = now - (max(int(cfg.keep_days), 0) * 86400)

    deleted = 0
    checked = 0

    allowed_ext = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    for path in _iter_files(root):
        checked += 1
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue

        _, ext = os.path.splitext(path)
        if ext.lower() not in allowed_ext:
            continue

        # use mtime
        if st.st_mtime < cutoff:
            try:
                os.remove(path)
                deleted += 1
            except OSError:
                # ignore undeletable files
                pass

    return {"enabled": True, "deleted": deleted, "checked": checked}


async def cleanup_loop(cfg: MediaRetentionConfig):
    """Run cleanup periodically."""
    # small startup delay to avoid racing with mounts on boot
    await _sleep_seconds(5)

    while True:
        cleanup_once(cfg)
        hours = max(int(cfg.interval_hours), 1)
        await _sleep_seconds(hours * 3600)


async def _sleep_seconds(seconds: int):
    import asyncio

    await asyncio.sleep(max(int(seconds), 1))
