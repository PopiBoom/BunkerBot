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

# id: (название, что пишет ИИ — по строке на каждое свободное место, по порядку, места для текста, стиль, готовые
# строки). Места — (x0, y0, x1, y1) или _TB (подпись сверху и снизу). Стиль: outline — белый с обводкой КАПСОМ,
# dark — чёрный на светлом, mock — белый с обводкой без капса. Готовые строки ставит код, не ИИ: {место: текст},
# «=N» — копия места N (Грю на 4-й картинке перечитывает 3-й шаг), «~N» — место N ИзДеВаТеЛьСкИм регистром.
# Шутку шаблона ИИ путал (Кермит без «не моё дело», у Грю на 4-й картинке новый текст) — поэтому так.
_TB = "top_bottom"
TEMPLATES: dict[str, tuple[str, list[str], list | str, str, dict[int, str]]] = {
    "drake": ("Дрейк: отворачивается от одного, доволен другим",
              ["нормальное/правильное, от чего он с отвращением отказывается",
               "позорная или тупая замена, которую он выбирает (его реальный поступок или слова)"],
              [(305, 20, 590, 450), (305, 490, 590, 920)], "outline", {}),
    "pooh": ("Винни-Пух: обычный и во фраке",
             ["простое, как есть", "ТО ЖЕ САМОЕ, но пафосно и по-барски (смысл тот же — меняется только подача)"],
             [(515, 15, 985, 285), (515, 315, 985, 585)], "dark", {}),
    "ds": ("Две кнопки, человек потеет над выбором",
           ["вариант 1", "вариант 2 — противоречит первому (оба — его слова или поступки)", "имя того, кто мучается"],
           [(30, 60, 255, 200), (275, 40, 520, 160), (30, 790, 570, 885)], "outline", {}),
    "gru": ("План Грю: на 4-й картинке он перечитывает 3-й шаг и понимает, что план провальный",
            ["шаг 1 его плана", "шаг 2", "шаг 3 — в нём уже видна дыра (код сам повторит его на 4-й картинке)"],
            [(268, 52, 452, 292), (733, 52, 917, 292), (268, 352, 452, 592), (733, 352, 917, 592)], "dark", {3: "=2"}),
    "db": ("Парень оглядывается на другую девушку при своей",
           ["новое, на что он пялится (девушка в красном)", "он сам — имя (парень)",
            "что у него уже есть и что он бросает ради нового (его девушка)"],
           [(90, 390, 420, 560), (430, 240, 670, 430), (680, 330, 890, 520)], "outline", {}),
    "woman-cat": ("Женщина орёт на кота за столом",
                  ["наезд с претензией: «Имя: что предъявляет»", "невозмутимый ответ кота — реплика того, на кого наехали"],
                  [(12, 8, 460, 128), (472, 8, 920, 128)], "dark", {}),
    "panik-kalm-panik": ("Паника — спокойствие — паника",
                         ["тревожное событие", "мысль, которая успокаивает",
                          "деталь той же истории, от которой хуже, чем в начале"],
                         [(15, 15, 270, 270), (15, 300, 270, 545), (15, 585, 270, 825)], "dark", {}),
    "gb": ("Растущий мозг",
           ["способ сделать что-то", "способ «умнее» (на деле тупее)", "ещё «гениальнее»", "самый идиотский"],
           [(12, 10, 288, 200), (12, 222, 288, 418), (12, 440, 288, 615), (12, 640, 288, 838)], "dark", {}),
    "pigeon": ("Это голубь?",
               ["кто (имя)", "обычная вещь, на которую он смотрит", "его неверный тупой вывод: «Это …?»"],
               [(60, 400, 470, 500), (640, 175, 990, 300), (20, 515, 980, 595)], "outline", {}),
    "fine": ("Собака в горящей комнате: «всё нормально»",
             ["ситуация, где всё горит и разваливается", "его спокойная реплика на это («всё нормально» по-своему)"],
             _TB, "outline", {}),
    "harold": ("Гарольд улыбается сквозь боль",
               ["болезненная для него ситуация", "его бодрая реплика сквозь боль"], _TB, "outline", {}),
    "spongebob": ("Губка Боб передразнивает",
                  ["его фраза почти дословно, в виде «Имя: фраза» (код сам повторит её снизу издевательским регистром)"],
                  _TB, "mock", {1: "~0"}),
    "mordor": ("«Нельзя просто так взять и…» (Боромир)",
               ["продолжение фразы «Нельзя просто так взять и …»: то, что у него никогда не выходит, по фактам"],
               _TB, "outline", {0: "Нельзя просто так взять и"}),
    "rollsafe": ("Умник с пальцем у виска",
                 ["проблема", "его «гениальное» решение: формально решает, по сути тупое"], _TB, "outline", {}),
    "kermit": ("Кермит пьёт чай: «…но это не моё дело»",
               ["колкое наблюдение про одного человека: что он говорит — и что делает на самом деле (лицемерие)"],
               _TB, "outline", {1: "Но это не моё дело"}),
    "success": ("Ребёнок-победитель",
                ["ситуация", "его жалкая «победа» в ней"], _TB, "outline", {}),
    "afraid": ("«Не знаю, что это, и боюсь спросить»",
               ["«Не знаю, что такое / как …» от лица человека — то, что стыдно не знать (по его словам)"],
               _TB, "outline", {1: "И уже боюсь спросить"}),
    "wonka": ("Снисходительный Вонка",
              ["«О, ты …?» — его понт", "«Расскажи ещё, как …» — издевательски, по фактам"], _TB, "outline", {}),
    "disastergirl": ("Девочка улыбается на фоне пожара",
                     ["что сгорело или развалилось из-за него", "его довольная реплика"], _TB, "outline", {}),
}


def lines_count(tid: str) -> int:
    """Сколько всего мест для текста на картинке."""
    boxes = TEMPLATES[tid][2]
    return 2 if boxes == _TB else len(boxes)


def slots(tid: str) -> int:
    """Сколько строк пишет ИИ (или человек) — места без готовых строк."""
    return lines_count(tid) - len(TEMPLATES[tid][4])


def _mock(text: str) -> str:
    """«я с понедельника на диете» → «я С пОнЕдЕлЬнИкА нА дИеТе» (Губка Боб передразнивает)."""
    text = text.split(":", 1)[1] if ":" in text[:25] else text  # «Вася: …» — передразниваем только фразу
    out, up = [], False
    for ch in text.strip():
        if ch.isalpha():
            out.append(ch.upper() if up else ch.lower())
            up = not up
        else:
            out.append(ch)
    return "".join(out)


def fill(tid: str, free: list[str]) -> list[str]:
    """Строки на все места: свои — по порядку, готовые и копии — из шаблона."""
    fixed = TEMPLATES[tid][4]
    it = iter(free)
    lines = [fixed.get(i) if i in fixed else next(it, "") for i in range(lines_count(tid))]
    for i, v in enumerate(lines):
        if v and v[0] in "=~" and v[1:].isdigit():
            src = lines[int(v[1:])] or ""
            lines[i] = src if v[0] == "=" else _mock(src)
    return lines


def menu_for_ai() -> str:
    out = []
    for tid, (name, slot_desc, _b, _s, _f) in TEMPLATES.items():
        out.append(f"{tid} — {name}; строк {len(slot_desc)}: "
                   + "; ".join(f"{i + 1}) {d}" for i, d in enumerate(slot_desc)))
    return "\n".join(out)


def _boxes(tid: str, w: int, h: int) -> list[tuple[int, int, int, int]]:
    boxes = TEMPLATES[tid][2]
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
    outline = style in ("outline", "mock")
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
        if outline:
            d.text(((x0 + x1) // 2, y), line, font=font, fill="white", anchor="ma",
                   stroke_width=max(2, font.size // 11), stroke_fill="black")
        else:
            d.text(((x0 + x1) // 2, y), line, font=font, fill=(18, 18, 22), anchor="ma")
        y += step


def render_meme(tid: str, lines: list[str]) -> bytes:
    """JPEG мема: шаблон tid, по строке текста на каждое место (готовые строки — уже вставлены, см. fill)."""
    img = Image.open(DIR / f"{tid}.jpg").convert("RGB")
    style = TEMPLATES[tid][3]
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
