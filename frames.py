"""Кадр из видео для ИИ: у кружков первый кадр обычно тёмный (камера ещё не проснулась),
поэтому берём кадр из середины, а если и он тёмный — самый светлый из нескольких.

PyAV (pip-пакет `av`) несёт ffmpeg внутри себя — ставить ничего в систему не нужно.
"""
from __future__ import annotations

import io
import logging

from PIL import Image, ImageStat

logger = logging.getLogger("frames")

DARK = 35            # средняя яркость (0-255), ниже которой кадр считаем «темнотой»
POINTS = (0.5, 0.7, 0.3, 0.85)  # где пробуем кадры: сначала середина
MAX_SIDE = 768


def _brightness(img: Image.Image) -> float:
    return ImageStat.Stat(img.convert("L")).mean[0]


def _grab(container, stream, frac: float) -> Image.Image | None:
    duration = container.duration  # в микросекундах (AV_TIME_BASE)
    if not duration and stream.duration and stream.time_base:
        duration = int(stream.duration * stream.time_base * 1_000_000)
    if duration:
        container.seek(int(duration * frac), any_frame=False, backward=True)
    for frame in container.decode(stream):
        return frame.to_image()
    return None


def middle_frame(data: bytes) -> bytes | None:
    """JPEG кадра из середины видео (или самого светлого из нескольких). None — не вышло."""
    try:
        import av
    except ImportError:
        logger.warning("PyAV не установлен — беру превью Telegram (первый кадр)")
        return None
    best, best_light = None, -1.0
    try:
        for frac in POINTS:
            with av.open(io.BytesIO(data)) as c:
                if not c.streams.video:
                    return None
                img = _grab(c, c.streams.video[0], frac)
            if img is None:
                continue
            light = _brightness(img)
            if light > best_light:
                best, best_light = img, light
            if light >= DARK:  # кадр нормальный — дальше не ищем
                break
    except Exception as e:
        logger.debug("frame grab failed: %s", e)
        if best is None:
            return None
    if best is None:
        return None
    best.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    best.convert("RGB").save(buf, format="JPEG", quality=88)
    return buf.getvalue()
