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
    """Кадр на доле frac длины видео. seek прыгает на опорный кадр ДО нужного места (в коротком кружке он часто
    один — самый первый, тёмный), поэтому после seek докручиваем до нужного времени."""
    duration = container.duration  # в микросекундах (AV_TIME_BASE)
    if not duration and stream.duration and stream.time_base:
        duration = int(stream.duration * stream.time_base * 1_000_000)
    target = duration * frac / 1_000_000 if duration else 0.0
    if duration:
        container.seek(int(duration * frac), any_frame=False, backward=True)
    last = None
    for frame in container.decode(stream):
        last = frame
        if frame.time is None or frame.time >= target:
            break
    return last.to_image() if last is not None else None


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


def strip(data: bytes, n: int = 3) -> bytes | None:
    """Несколько кадров (начало, середина, конец) в одну полосу JPEG — ИИ видит, что в видео происходит, а не
    один смазанный кадр (по одному кадру кружка у автоматов ИИ придумал подпись мимо). None — не вышло."""
    try:
        import av
    except ImportError:
        return None
    shots = []
    try:
        for frac in [(i + 1) / (n + 1) for i in range(n)]:
            with av.open(io.BytesIO(data)) as c:
                if not c.streams.video:
                    return None
                img = _grab(c, c.streams.video[0], frac)
            if img is not None:
                img.thumbnail((max(320, 1560 // n), 640))  # вся полоса ≈ 1560 px — больше Claude всё равно ужмёт
                shots.append(img.convert("RGB"))
    except Exception as e:
        logger.debug("strip failed: %s", e)
    if not shots:
        return None
    h = max(i.height for i in shots)
    out = Image.new("RGB", (sum(i.width for i in shots) + 8 * (len(shots) - 1), h), "white")
    x = 0
    for img in shots:
        out.paste(img, (x, (h - img.height) // 2))
        x += img.width + 8
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


AUDIO_RATE = 16000  # речи хватает, файл маленький (минута ≈ 1.9 МБ WAV)


def audio_wav(data: bytes, max_sec: int = 300) -> bytes | None:
    """Звук из голосового (OGG/Opus), кружка или видео — в WAV 16 кГц моно (для расшифровки речи).
    None — звука нет или не вышло."""
    try:
        import av
    except ImportError:
        return None
    import wave

    pcm = bytearray()
    try:
        with av.open(io.BytesIO(data)) as c:
            if not c.streams.audio:
                return None
            res = av.AudioResampler(format="s16", layout="mono", rate=AUDIO_RATE)
            limit = max_sec * AUDIO_RATE * 2
            for frame in c.decode(c.streams.audio[0]):
                frame.pts = None
                for rf in res.resample(frame):
                    pcm += bytes(rf.planes[0])[:rf.samples * 2]
                if len(pcm) >= limit:
                    break
            for rf in res.resample(None):
                pcm += bytes(rf.planes[0])[:rf.samples * 2]
    except Exception as e:
        logger.debug("audio extract failed: %s", e)
        if not pcm:
            return None
    pcm = pcm[:max_sec * AUDIO_RATE * 2]
    if len(pcm) < AUDIO_RATE:  # меньше полсекунды — нечего слушать
        return None
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_RATE)
        w.writeframes(bytes(pcm))
    return buf.getvalue()
