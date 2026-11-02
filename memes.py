"""Мемы: по шаблонам (Drake, «две кнопки», «план Грю»…) и видео-мемы с белой плашкой «когда…».

Шаблоны — пустые картинки в assets/memes (взяты с открытого memegen.link). Текст рисуем САМИ: наружу
уходит только запрос к ИИ, как и для остального стёба (отправлять текст с именами из чата на чужой
сервис мемов — нельзя). Места для текста подобраны под каждую картинку вручную (x0, y0, x1, y1).

Видео-мем: сверху белая плашка с подписью, под ней исходное видео (уменьшенное до 720 по ширине),
звук сохраняется. Кодирует PyAV (ffmpeg внутри пакета), в отдельном потоке — бот не подвисает.
"""
from __future__ import annotations

import io
import re
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw

from games.bunker.render import _font

DIR = Path(__file__).resolve().parent / "assets" / "memes"

# id: (что значит шаблон — для ИИ, места для текста, стиль: outline — белый с обводкой, dark — чёрный на светлом)
_TB = "top_bottom"  # подпись сверху и снизу — места считаем от размера картинки
TEMPLATES: dict[str, tuple[str, list | str, str]] = {
    "drake": ("Drake: 1 — что он с отвращением отвергает, 2 — что с удовольствием выбирает вместо",
              [(305, 20, 590, 450), (305, 490, 590, 920)], "outline"),
    "pooh": ("Винни-Пух: 1 — простое/обычное, 2 — то же самое, но пафосно и «по-барски»",
             [(515, 15, 985, 285), (515, 315, 985, 585)], "dark"),
    "ds": ("Две кнопки: 1 и 2 — два варианта, между которыми он мучительно выбирает, 3 — кто потеет над выбором",
           [(30, 60, 255, 200), (275, 40, 520, 160), (30, 790, 570, 885)], "outline"),
    "gru": ("План Грю: 1, 2, 3 — шаги гениального плана, 4 — то же, что 3, но теперь видно, что план провальный",
            [(268, 52, 452, 292), (733, 52, 917, 292), (268, 352, 452, 592), (733, 352, 917, 592)], "dark"),
    "db": ("Отвлечённый парень: 1 — новое соблазнительное (девушка в красном), 2 — кто отвлёкся (парень), "
           "3 — что у него уже есть (его девушка)", [(90, 390, 420, 560), (430, 240, 670, 430), (680, 330, 890, 520)],
           "outline"),
    "woman-cat": ("Женщина орёт на кота: 1 — кто орёт и что предъявляет, 2 — ответ кота, которому плевать",
                  [(12, 8, 460, 128), (472, 8, 920, 128)], "dark"),
    "panik-kalm-panik": ("Паника-спокойствие-паника: 1 — тревожное событие, 2 — успокаивающая мысль, "
                         "3 — новая деталь, от которой ещё хуже",
                         [(15, 15, 270, 270), (15, 300, 270, 545), (15, 585, 270, 825)], "dark"),
    "gb": ("Растущий мозг: 4 варианта всё «гениальнее», на самом деле — всё тупее, последний — самый идиотский",
           [(12, 10, 288, 200), (12, 222, 288, 418), (12, 440, 288, 615), (12, 640, 288, 838)], "dark"),
    "pigeon": ("Это голубь?: 1 — кто (человек), 2 — на что он смотрит, 3 — его тупой вывод «это …?»",
               [(60, 400, 470, 500), (640, 175, 990, 300), (20, 515, 980, 595)], "outline"),
    "fine": ("Всё нормально (собака в горящей комнате): 1 — что горит, ситуация-пиздец, 2 — «всё нормально» в "
             "его исполнении", _TB, "outline"),
    "harold": ("Гарольд, скрывающий боль: 1 — ситуация, 2 — как он улыбается сквозь боль", _TB, "outline"),
    "spongebob": ("Издевательский Губка Боб: 1 — что человек сказал, 2 — то же самое, но ИзДеВаТеЛьСкИ", _TB,
                  "outline"),
    "mordor": ("Нельзя просто так взять и…: 1 — «нельзя просто так взять и», 2 — что именно", _TB, "outline"),
    "rollsafe": ("Умник с пальцем у виска: 1 — проблема, 2 — «гениальное» тупое решение", _TB, "outline"),
    "kermit": ("Кермит с чаем: 1 — колкое наблюдение про кого-то, 2 — «но это не моё дело»", _TB, "outline"),
    "success": ("Ребёнок-победитель: 1 — ситуация, 2 — его жалкая «победа»", _TB, "outline"),
    "afraid": ("Боюсь спросить: 1 — «я не знаю, что такое …», 2 — «и уже боюсь спросить»", _TB, "outline"),
    "wonka": ("Снисходительный Вонка: 1 — «о, ты …?», 2 — издевательское «расскажи ещё, как …»", _TB, "outline"),
    "disastergirl": ("Девочка на фоне пожара: 1 — что он натворил, 2 — «всё по плану»", _TB, "outline"),
}


def lines_count(tid: str) -> int:
    boxes = TEMPLATES[tid][1]
    return 2 if boxes == _TB else len(boxes)


def menu_for_ai() -> str:
    return "\n".join(f"{tid} ({lines_count(tid)} строк): {desc}" for tid, (desc, _b, _s) in TEMPLATES.items())


def _boxes(tid: str, w: int, h: int) -> list[tuple[int, int, int, int]]:
    boxes = TEMPLATES[tid][1]
    if boxes == _TB:
        return [(int(w * .03), int(h * .02), int(w * .97), int(h * .27)),
                (int(w * .03), int(h * .73), int(w * .97), int(h * .98))]
    return list(boxes)


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u20E3]")


def _clean(text: str) -> str:
    """Эмодзи в шрифте нет (вышли бы квадратики), длинное тире владелец не любит."""
    return " ".join(_EMOJI.sub("", text or "").replace("—", "-").replace("–", "-").split())


def _wrap_words(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str] | None:
    """Перенос по словам; слово не влезает целиком — None (берём шрифт мельче, слова не рвём)."""
    lines, cur = [], ""
    for w in text.split():
        if draw.textlength(w, font=font) > width:
            return None
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font) <= width:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _draw_text(img: Image.Image, box: tuple[int, int, int, int], text: str, style: str) -> None:
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    text = _clean(text)
    if style == "outline":
        text = text.upper()
    pad = 6
    width, height = x1 - x0 - 2 * pad, y1 - y0 - 2 * pad
    for size in range(min(64, max(16, height // 2)), 13, -2):
        font = _font("DejaVuSans-Bold.ttf", size)
        lines = _wrap_words(d, text, font, width)
        step = int(size * 1.18)
        if lines and len(lines) * step <= height:
            break
    else:  # совсем длинно — самым мелким, как есть
        font, step = _font("DejaVuSans-Bold.ttf", 14), 17
        lines = _wrap_words(d, text, font, width) or [text]
    y = y0 + pad + (height - len(lines) * step) // 2
    for line in lines:
        if style == "outline":
            d.text(((x0 + x1) // 2, y), line, font=font, fill="white", anchor="ma",
                   stroke_width=max(2, font.size // 11), stroke_fill="black")
        else:
            d.text(((x0 + x1) // 2, y), line, font=font, fill=(18, 18, 22), anchor="ma")
        y += step


def render_meme(tid: str, lines: list[str]) -> bytes:
    """JPEG мема: шаблон tid, по строке текста на каждое место."""
    img = Image.open(DIR / f"{tid}.jpg").convert("RGB")
    style = TEMPLATES[tid][2]
    for box, text in zip(_boxes(tid, *img.size), lines, strict=False):
        if _clean(text):
            _draw_text(img, box, text, style)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# ---------- видео-мем ----------

VIDEO_MAX_SEC = 60          # длиннее — отказываем (перекодирование грузит процессор сервера)
VIDEO_WIDTH = 720
VIDEO_FPS = 30


def caption_bar(text: str, width: int) -> Image.Image:
    """Белая плашка с чёрной подписью, как у тикток-мемов «когда…»."""
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    text = _clean(text)
    base = max(18, width // 16)  # шрифт от ширины: у кружка (384) мельче, чем у видео 720
    for size in range(base, int(base * .6) - 1, -2):
        font = _font("DejaVuSans-Bold.ttf", size)
        lines = _wrap_words(probe, text, font, width - width // 12)
        if lines and len(lines) <= 4:
            break
    lines = lines or [text]
    step = int(font.size * 1.22)
    pad = max(12, font.size // 2)
    h = (2 * pad + len(lines) * step + 1) // 2 * 2  # чётная — для yuv420p
    img = Image.new("RGB", (width, h), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((width // 2, pad + i * step), line, font=font, fill="black", anchor="ma")
    return img


def video_duration(data: bytes) -> float:
    import av

    with av.open(io.BytesIO(data)) as c:
        if c.duration:
            return c.duration / 1_000_000
        vs = c.streams.video[0]
        return float(vs.duration * vs.time_base) if vs.duration and vs.time_base else 0.0


def _rows(plane, width: int, line: int) -> bytes:
    """Плоскость кадра построчно с нужным шагом строки (у декодера строки бывают с выравниванием до 64)."""
    mv = memoryview(plane)
    if plane.line_size == line:
        return bytes(mv[:line * plane.height])
    pad = b"\0" * (line - width)
    ls = plane.line_size
    return b"".join(bytes(mv[i * ls:i * ls + width]) + pad for i in range(plane.height))


def _stack(bar_yuv: list[bytes], frame, w: int, h: int):
    """Кадр «плашка сверху + видео снизу» в yuv420p: склейка плоскостей, без PIL на каждый кадр."""
    import av

    out = av.VideoFrame(w, h, "yuv420p")
    for pl, top, src in zip(out.planes, bar_yuv, frame.planes, strict=True):
        pl.update(top + _rows(src, pl.width, pl.line_size))
    return out


def make_video_meme(data: bytes, caption: str) -> tuple[bytes, int, int, bool]:
    """(mp4, ширина, высота, есть ли звук). Сверху плашка с подписью, ниже видео (до VIDEO_WIDTH по ширине, до VIDEO_FPS
    кадров в секунду); звук перекодируется в AAC. Время кадров берём из исходника — звук не уезжает."""
    import av

    inp = av.open(io.BytesIO(data))
    try:
        vs = inp.streams.video[0]
        a_in = inp.streams.audio[0] if inp.streams.audio else None
        w0, h0 = vs.codec_context.width, vs.codec_context.height
        fw = min(VIDEO_WIDTH, w0) // 2 * 2
        fh = max(2, int(h0 * fw / w0) // 2 * 2)
        bar = caption_bar(caption, fw)
        H = bar.height + fh  # обе чётные — для yuv420p
        fps = min(Fraction(VIDEO_FPS), vs.average_rate or vs.guessed_rate or Fraction(25))
        # Плашка переводится в YUV один раз; каждый кадр — склейка её плоскостей с плоскостями кадра
        # (без PIL на каждый кадр: так втрое быстрее, а процессор на сервере слабый)
        bar_frame = av.VideoFrame.from_image(bar).reformat(format="yuv420p")
        layout = av.VideoFrame(fw, H, "yuv420p").planes  # какой шаг строк у кадров на выходе
        bar_yuv = [_rows(src, dst.width, dst.line_size) for src, dst in zip(bar_frame.planes, layout, strict=True)]
        buf = io.BytesIO()
        out = av.open(buf, "w", format="mp4")
        ov = out.add_stream("libx264", rate=fps)
        ov.width, ov.height, ov.pix_fmt = fw, H, "yuv420p"
        ov.options = {"preset": "veryfast", "crf": "26"}
        oa = resampler = None
        if a_in is not None:
            rate = a_in.codec_context.sample_rate or 44100
            oa = out.add_stream("aac", rate=rate)
            oa.layout = "stereo"
            resampler = av.AudioResampler(format="fltp", layout="stereo", rate=rate)
        last = -1
        for packet in inp.demux(*([vs] + ([a_in] if a_in is not None else []))):
            for frame in packet.decode():
                if packet.stream.type == "video":
                    t = frame.time if frame.time is not None else (last + 1) / fps
                    pts = max(0, round(t * fps))
                    if pts <= last:  # 60 кадров/с → 30: лишние кадры пропускаем
                        continue
                    last = pts
                    vf = _stack(bar_yuv, frame.reformat(width=fw, height=fh, format="yuv420p"), fw, H)
                    vf.pts, vf.time_base = pts, 1 / fps
                    for p in ov.encode(vf):
                        out.mux(p)
                elif oa is not None:
                    frame.pts = None
                    for rf in resampler.resample(frame):
                        for p in oa.encode(rf):
                            out.mux(p)
        for p in ov.encode():
            out.mux(p)
        if oa is not None:
            for rf in resampler.resample(None):
                for p in oa.encode(rf):
                    out.mux(p)
            for p in oa.encode():
                out.mux(p)
        out.close()
    finally:
        inp.close()
    return buf.getvalue(), fw, H, oa is not None
