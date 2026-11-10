"""Звук «как по радио» для ночного эфира (handlers/night.py): голос + эфир + позывной → голосовое OGG/Opus.

Голос (PCM 16 бит моно, куски — абзацы) склеивается с паузами, обрезается по полосе AM/FM-приёмника (≈ 280–3600 Гц),
чуть «греется» (мягкое насыщение) и плавает по громкости; под ним — шипение эфира, редкие потрескивания и фон
погоды (ветер или дождь, тоже сквозь «приёмник»). В начале — настройка на волну (шум и свист, уходящий вниз) и
тихий позывной из трёх нот, в конце — хвост эфира и щелчок выключения. Всё на numpy, кодирует PyAV (libopus).
"""
from __future__ import annotations

import io
import logging
import os
from functools import lru_cache

import numpy as np

SR = 48000
log = logging.getLogger(__name__)


def _gain(f: np.ndarray, lo: float, hi: float) -> np.ndarray:
    gain = np.ones_like(f)
    if lo > 0:
        gain *= 1 / (1 + (lo / np.maximum(f, 1e-3)) ** 4)
    if hi > 0:
        gain *= 1 / (1 + (f / hi) ** 4)
    return gain


DIRECT_MAX = 1 << 20   # до ≈ 22 с — фильтр целиком через БПФ (точно и быстро); длиннее — блоками
FIR_TAPS = 8191        # ядро для блочного фильтра (≈ 0.17 с — хватает и для 30 Гц снизу)
OLA_FFT = 1 << 17      # размер БПФ блока


def _band(x: np.ndarray, lo: float, hi: float, sr: int = SR) -> np.ndarray:
    """Полосовой фильтр с мягкими краями (без звона). Длинный сигнал — блочной свёрткой (overlap-add): раньше БПФ
    целиком по 8-минутной страшной истории съедал ~5 ГБ, и сервер убивал бота посреди сборки (07.11)."""
    return _filter(x, lambda f: _gain(f, lo, hi), sr)


def _filter(x: np.ndarray, curve, sr: int = SR) -> np.ndarray:
    """Фильтр с любой АЧХ: curve(частоты) -> усиление. Короткое — БПФ целиком, длинное — блоками (overlap-add)."""
    n = len(x)
    if n == 0:
        return x
    if n <= DIRECT_MAX:
        f = np.fft.rfftfreq(n, 1 / sr)
        return np.fft.irfft(np.fft.rfft(x) * curve(f), n).astype(np.float32)
    k = FIR_TAPS
    half = k // 2
    h = np.fft.irfft(curve(np.fft.rfftfreq(OLA_FFT, 1 / sr)), OLA_FFT)  # нулевая фаза
    h = np.roll(h, half)[:k] * np.hanning(k)
    spec_h = np.fft.rfft(h, OLA_FFT)
    step = OLA_FFT - k + 1
    out = np.zeros(n + k - 1, dtype=np.float32)
    for i in range(0, n, step):
        seg = x[i:i + step]
        y = np.fft.irfft(np.fft.rfft(seg, OLA_FFT) * spec_h, OLA_FFT)[:len(seg) + k - 1]
        out[i:i + len(y)] += y
    return out[half:half + n]


def _resample(pcm: bytes, rate: int) -> np.ndarray:
    """PCM 16 бит → float32 48 кГц, линейная интерполяция кусками. Раньше np.interp по всему письму держал массивы
    float64 на весь голос: 4 минуты — 0.4 ГБ памяти."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    if rate == SR or len(x) < 2:
        return x
    if SR == 2 * rate:  # 24 → 48 кГц (все наши голоса): между соседними сэмплами — среднее
        out = np.empty(2 * len(x), dtype=np.float32)
        out[0::2] = x
        out[1:-1:2] = (x[:-1] + x[1:]) * np.float32(0.5)
        out[-1] = x[-1]
        return out
    n_out = int(len(x) * SR / rate)
    out = np.empty(n_out, dtype=np.float32)
    for i in range(0, n_out, 1 << 20):
        t = np.arange(i, min(i + (1 << 20), n_out)) * (rate / SR)
        k = np.minimum(t.astype(np.int64), len(x) - 2)
        frac = (t - k).astype(np.float32)
        out[i:i + len(t)] = x[k] * (1 - frac) + x[k + 1] * frac
    return out


def _fade(x: np.ndarray, fin: float, fout: float) -> np.ndarray:
    x = x.copy()
    a, b = int(fin * SR), int(fout * SR)
    if a:
        x[:a] *= np.linspace(0, 1, min(a, len(x)))[: len(x[:a])]
    if b:
        x[-b:] *= np.linspace(1, 0, min(b, len(x)))[-len(x[-b:]):]
    return x


def _db(v: float) -> float:
    return 10 ** (v / 20)


def _wind(n: int, rng: np.random.Generator) -> np.ndarray:
    brown = np.cumsum(rng.standard_normal(n))
    k = 4801  # убрать уход в сторону: вычесть скользящее среднее (через накопленную сумму — быстро)
    c = np.cumsum(np.concatenate(([0.0], brown)))
    half = k // 2
    idx = np.arange(n)
    lo, hi = np.clip(idx - half, 0, n), np.clip(idx + half + 1, 0, n)
    brown -= (c[hi] - c[lo]) / (hi - lo)
    brown = _band(brown, 60, 700)
    t = np.arange(n) / SR
    gusts = 0.55 + 0.45 * np.sin(2 * np.pi * 0.07 * t + 1.3) * np.sin(2 * np.pi * 0.19 * t)
    w = brown * gusts
    return w / (np.abs(w).max() or 1)


def _rain(n: int, rng: np.random.Generator) -> np.ndarray:
    hiss = _band(rng.standard_normal(n), 1500, 7000)
    hiss /= np.abs(hiss).max() or 1
    drops = np.zeros(n)
    for pos in rng.integers(0, n, size=max(1, n // 900)):  # капли — короткие щелчки
        ln = min(240, n - pos)
        drops[pos:pos + ln] += rng.uniform(0.2, 1) * np.exp(-np.arange(ln) / 40) * rng.choice([-1, 1])
    return 0.7 * hiss + 0.5 * drops / (np.abs(drops).max() or 1)


def _static(n: int, rng: np.random.Generator) -> np.ndarray:
    s = _band(rng.standard_normal(n), 900, 6500)
    s /= np.abs(s).max() or 1
    crackle = np.zeros(n)
    for pos in rng.integers(0, n, size=max(1, int(n / SR * 2.5))):  # ≈2-3 потрескивания в секунду
        ln = min(120, n - pos)
        crackle[pos:pos + ln] += rng.uniform(0.3, 1) * np.exp(-np.arange(ln) / 18) * rng.choice([-1, 1])
    return 0.6 * s + 0.8 * crackle


def _tuning(sec: float, rng: np.random.Generator) -> np.ndarray:
    """Настройка на волну: громкий шум эфира и свист, сползающий вниз и пропадающий."""
    n = int(sec * SR)
    t = np.arange(n) / SR
    freq = 2400 * np.exp(-t * 2.2) + 500 + 60 * np.sin(2 * np.pi * 5 * t)
    whistle = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.linspace(0.5, 0, n) ** 1.5
    noise = _band(rng.standard_normal(n), 400, 7000)
    noise /= np.abs(noise).max() or 1
    return 0.55 * noise * np.linspace(1, 0.25, n) + 0.35 * whistle


def _ident() -> np.ndarray:
    """Позывной: три тихие ноты, как у ночной радиостанции."""
    out = []
    for f, ln in ((523.25, 0.32), (659.25, 0.32), (783.99, 0.9)):
        t = np.arange(int(ln * SR)) / SR
        tone = (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(4 * np.pi * f * t)) * np.exp(-t * 3.2)
        out.append(tone)
    return np.concatenate(out) * 0.5


def _drone(n: int, rng: np.random.Generator, t0: float = 0.0, base: float = 50.0, phase: float = 0.0) -> np.ndarray:
    """Для страшной истории: низкий гул (две близкие ноты — биения) и редкие глухие удары где-то далеко.
    t0 — с какой секунды эфира этот кусок: гул считается по абсолютному времени, и куски фона стыкуются без скачков."""
    t = (np.arange(n, dtype=np.float64) / SR + t0)
    hum = np.sin(2 * np.pi * base * t) + 0.7 * np.sin(2 * np.pi * (base * 1.007) * t) + \
        0.25 * np.sin(2 * np.pi * base * 2.01 * t)
    hum *= 0.6 + 0.4 * np.sin(2 * np.pi * 0.05 * t + phase)
    del t
    knocks = np.zeros(n, dtype=np.float32)
    first = max(0, int((4 - t0) * SR))  # первые 4 с эфира — без стука
    if first < n:
        for pos in rng.integers(first, n, size=max(1, int(n / SR / 14))):
            for k in range(rng.integers(1, 4)):  # стук: 1-3 удара
                p = pos + k * int(0.42 * SR)
                ln = min(int(0.25 * SR), n - p)
                if ln > 0:
                    knocks[p:p + ln] += np.sin(2 * np.pi * 70 * np.arange(ln) / SR) * np.exp(-np.arange(ln) / 1800)
    out = (0.6 * hum / 1.95 + 0.9 * knocks) / 1.5   # 1.95 — пик гула, 1.5 — пик гула со стуком
    return out.astype(np.float32)


def _glitch(rng: np.random.Generator) -> np.ndarray:
    """Сбой эфира в жутком месте: голос обрывается, рваный треск и плывущий свист, потом тишина."""
    n = int(1.4 * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    gate = (rng.random(int(n / 600) + 1) > 0.45).repeat(600)[:n]  # заикание: эфир рвётся кусками
    warble = np.sin(2 * np.pi * np.cumsum(1300 + 500 * np.sin(2 * np.pi * 9 * t)) / SR)
    out = (0.7 * noise * gate + 0.35 * warble) * np.linspace(1, 0.2, n)
    out[int(1.0 * SR):] *= 0.05  # провал — почти тишина
    return (out / (np.abs(out).max() or 1) * 0.9).astype(np.float32)


GEN_SEG = 10 * SR   # синтез фона (гул, мрачный аккорд) — кусками по 10 с: временные массивы float64 небольшие
BED_SEG = 30 * SR   # фон эфира собираем кусками по 30 с…
BED_XF = SR // 2    # …с перекрытием 0.5 с (плавные стыки) — память не растёт с длиной эфира


def _bed_into(mix: np.ndarray, start: int, rng: np.random.Generator, rain: bool, horror: bool) -> None:
    """Фон под голосом (эфир, ветер или дождь, для страшной истории — гул и стук) прямо в mix[start:]."""
    total = len(mix) - start
    base, phase = rng.uniform(46, 55), rng.uniform(0, 6)
    fin, fout = int(0.8 * SR), int(0.3 * SR)
    pos = 0
    while pos < total:
        n = min(BED_SEG + BED_XF, total - pos)
        last = pos + n >= total
        seg = _db(-33) * _static(n, rng)
        seg += _db(-25 if rain else -24) * _band(_rain(n, rng) if rain else _wind(n, rng), 200, 4200)
        if horror:  # гул и стук — не сквозь приёмник, «в комнате» у слушателя
            seg += _db(-24) * _band(_drone(n, rng, (start + pos) / SR, base, phase), 30, 900)
        seg = seg.astype(np.float32)
        if pos == 0:
            seg[:min(fin, n)] *= np.linspace(0, 1, min(fin, n), dtype=np.float32)
        else:
            seg[:BED_XF] *= np.linspace(0, 1, BED_XF, dtype=np.float32)
        if last:
            seg[-min(fout, n):] *= np.linspace(1, 0, min(fout, n), dtype=np.float32)
        else:
            seg[-BED_XF:] *= np.linspace(1, 0, BED_XF, dtype=np.float32)
        mix[start + pos:start + pos + n] += seg
        if last:
            break
        pos += n - BED_XF


def broadcast(voice_chunks: list[bytes], rate: int = 24000, rain: bool = False, seed: int = 0,
              horror: bool = False, glitches: set[int] | None = None) -> np.ndarray:
    """Весь эфир (float32, 48 кГц, моно, пик ≈ -1 дБ). horror — гул, далёкий стук и паузы длиннее; glitches —
    после каких кусков голоса вместо паузы сбой эфира. Всё во float32, фон — кусками: 8 минут эфира — сотни МБ
    памяти, а не 5 ГБ (раньше на длинной страшной истории сервер убивал бота)."""
    rng = np.random.default_rng(seed)
    pause = np.zeros(int((1.3 if horror else 0.85) * SR), dtype=np.float32)
    glitches = glitches or set()
    parts = []
    for i, pcm in enumerate(voice_chunks):
        v = _resample(pcm, rate)
        if len(v):
            v = v / (np.abs(v).max() or 1)
            parts += ([_glitch(rng) if i - 1 in glitches else pause] if i else []) + [v]
    voice = np.concatenate(parts).astype(np.float32) if parts else np.zeros(SR, dtype=np.float32)
    del parts
    # «приёмник»: полоса, насыщение, лёгкое плавание громкости
    voice = _band(voice, 280, 3600)
    np.tanh(voice * 2.2, out=voice)
    voice /= np.tanh(2.2)
    ph = rng.uniform(0, 6)
    for i in range(0, len(voice), 1 << 20):  # кусками — без массивов времени на весь эфир
        t = np.arange(i, min(i + (1 << 20), len(voice)), dtype=np.float64) / SR
        voice[i:i + len(t)] *= (0.94 + 0.06 * np.sin(2 * np.pi * 0.35 * t + ph)).astype(np.float32)

    intro_tune = _tuning(1.8, rng).astype(np.float32)
    ident = _ident()
    lead = int(0.4 * SR)                     # тишина эфира между позывным и голосом
    tail = int(1.6 * SR)
    n = len(intro_tune) + len(ident) + lead + len(voice) + tail
    mix = np.zeros(n, dtype=np.float32)
    pos = 0
    mix[pos:pos + len(intro_tune)] += intro_tune
    pos += len(intro_tune) - int(0.5 * SR)   # позывной вступает, пока свист ещё тает
    mix[pos:pos + len(ident)] += _band(ident, 200, 4000) * 0.8
    pos += len(ident) + lead
    vstart = pos
    mix[pos:pos + len(voice)] += voice
    vlen = len(voice)
    del voice
    # под голосом — эфир и погода (тоже сквозь приёмник), от позывного до конца
    _bed_into(mix, len(intro_tune) - int(0.5 * SR), rng, rain, horror)
    # конец: всплеск эфира и щелчок выключения
    end = vstart + vlen + int(0.9 * SR)
    burst_n = n - end
    if burst_n > 0:
        burst = _band(rng.standard_normal(burst_n), 500, 6500)
        burst = burst / (np.abs(burst).max() or 1) * np.linspace(0.05, 0.45, burst_n)
        mix[end:] += burst.astype(np.float32)
        mix[-200:] = 0
    mix = _band(mix, 35 if horror else 120, 7500)
    mix /= max(float(mix.max()), float(-mix.min())) or 1
    mix *= _db(-1)
    return mix


def ogg_secs(data: bytes) -> int:
    """Длительность OGG/Opus в секундах — для send_voice(duration=…): без неё Telegram показывает у голосового
    00:00 (владелец 08.11: «почему в канале у голосового 00:00»), и при пересылке по file_id так и остаётся."""
    try:
        import av

        with av.open(io.BytesIO(data)) as c:
            st = c.streams.audio[0]
            end = 0.0
            for pkt in c.demux(st):
                if pkt.pts is not None and pkt.duration is not None:
                    end = max(end, float((pkt.pts + pkt.duration) * pkt.time_base))
            return max(1, int(round(end)))
    except Exception as e:  # noqa: BLE001
        log.warning("radio: длительность OGG не посчиталась: %s", e)
        return 0


def to_ogg(x: np.ndarray, bitrate: int = 48000) -> bytes:
    """float32 48 кГц моно → OGG/Opus (голосовое Telegram)."""
    import av

    buf = io.BytesIO()
    out = av.open(buf, "w", format="ogg")
    st = out.add_stream("libopus", rate=SR)
    st.layout = "mono"
    st.bit_rate = bitrate
    step = 960 * 50  # кусками по секунде (и в 16 бит — тоже кусками, без копии всего эфира)
    pts = 0
    for i in range(0, len(x), step):
        chunk = (np.clip(x[i:i + step], -1, 1) * 32767).astype("<i2")
        frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SR
        frame.pts = pts
        pts += len(chunk)
        for packet in st.encode(frame):
            out.mux(packet)
    for packet in st.encode(None):
        out.mux(packet)
    out.close()
    return buf.getvalue()


# ---------- «студия»: страшная рубрика «радио Волгограда» звучит как настоящий FM-эфир ----------
# Владелец: «почему голос такой однотонный и скучный? хочу, чтобы как по-настоящему». Прежняя обработка «под
# приёмник» (полоса 280-3600 Гц, насыщение, выравнивание каждого абзаца по пику) стирала интонацию: шёпот становился
# таким же громким, как крик. Здесь — полный голос с мягкой компрессией, джингл станции, гул и стук только под
# письмом, общий уровень на весь голос (тихое остаётся тихим).

def _compress(x: np.ndarray, thresh_db: float = -22.0, ratio: float = 2.5, frame: int = SR // 100) -> np.ndarray:
    """Мягкий компрессор по уровню 10-мс кадров. Считает и применяет кусками — без копий всего голоса."""
    n = len(x)
    if n < frame * 4:
        return x
    m = n // frame
    level = np.empty(m)
    step = 50_000  # кадров за раз (~8 мин звука — 10 проходов)
    for i in range(0, m, step):
        k = min(step, m - i)
        blk = x[i * frame:(i + k) * frame].reshape(k, frame)
        level[i:i + k] = 10 * np.log10(np.mean(np.square(blk, dtype=np.float64), axis=1) + 1e-12)
    gain = np.where(level > thresh_db, (thresh_db + (level - thresh_db) / ratio) - level, 0.0)
    w = 7  # сгладить ~70 мс, чтобы не «качало»
    gain = np.convolve(np.pad(gain, (w // 2, w // 2), mode="edge"), np.ones(w) / w, mode="valid")
    lin = 10 ** (gain / 20)
    centers = np.arange(m) * frame + frame / 2
    out = np.empty_like(x)
    for a in range(0, n, 1 << 20):
        b = min(a + (1 << 20), n)
        out[a:b] = x[a:b] * np.interp(np.arange(a, b), centers, lin).astype(np.float32)
    return out


def _bell(f: float, ln: float, amp: float = 1.0) -> np.ndarray:
    t = np.arange(int(ln * SR)) / SR
    tone = (np.sin(2 * np.pi * f * t) + 0.35 * np.sin(2 * np.pi * f * 2.0 * t) * np.exp(-t * 6)
            + 0.18 * np.sin(2 * np.pi * f * 2.76 * t) * np.exp(-t * 9))
    attack = np.minimum(1, t / 0.004)
    return (tone * attack * np.exp(-t * 2.6) * amp).astype(np.float32)


def _jingle(up: bool = True) -> np.ndarray:
    """Позывной станции: колокольчики по ля-минору (вверх — в начале, вниз — в конце) и мягкий низ."""
    notes = [440.0, 523.25, 659.25, 880.0]
    if not up:
        notes = notes[::-1][:3]
    step = int(0.17 * SR)
    out = np.zeros(step * len(notes) + int(1.6 * SR), dtype=np.float32)
    for i, f in enumerate(notes):
        b = _bell(f, 1.6, 0.9 if i < len(notes) - 1 else 1.1)
        out[i * step:i * step + len(b)] += b
    t = np.arange(int(0.9 * SR)) / SR
    sub = (np.sin(2 * np.pi * 55 * t) * np.exp(-t * 4)).astype(np.float32) * 0.6  # мягкий удар снизу
    out[:len(sub)] += sub
    return out / (np.abs(out).max() or 1)


# Заставка «Писем Волгограда» — звук владельца (08.11: «чтобы вот это игралось в качестве начала»), ≈ 6 с. Нет
# файла или не читается — прежний позывной-колокольчики (_jingle).
INTRO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "radio", "intro.mp3")
INTRO_DB = -3.0       # заставка на 3 дБ тише речи ведущего (в полосе телефона)
INTRO_FADE = 1.0      # у файла нет своего затухания — уводим последнюю секунду
INTRO_OVERLAP = 0.6   # ведущий вступает за столько секунд до конца заставки


@lru_cache(maxsize=1)
def _intro_raw() -> np.ndarray | None:
    """Заставка из файла: моно, 48 кГц, float32 (один раз за запуск)."""
    if not os.path.exists(INTRO_FILE):
        return None
    try:
        import av

        with av.open(INTRO_FILE) as c:
            st = c.streams.audio[0]
            rate = st.codec_context.sample_rate
            chunks = []
            for fr in c.decode(st):
                a = fr.to_ndarray()
                if a.dtype.kind == "i":
                    a = a.astype(np.float32) / 32768
                if fr.format.is_planar:
                    a = a.reshape(len(fr.layout.channels), -1).mean(axis=0)
                else:
                    a = a.reshape(-1, len(fr.layout.channels)).mean(axis=1)
                chunks.append(a.astype(np.float32))
        x = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        x = _resample((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes(), rate)
        if len(x) < SR // 2:
            return None
        x.flags.writeable = False
        return x
    except Exception as e:  # noqa: BLE001 — битый файл не должен ронять эфир
        log.warning("radio: заставка %s не читается: %s", INTRO_FILE, e)
        return None


def _intro(level_db: float) -> np.ndarray | None:
    """Заставка с громкостью level_db (RMS в полосе телефона), мягким входом и затуханием в конце."""
    raw = _intro_raw()
    if raw is None:
        return None
    x = raw.copy()
    r = float(np.sqrt(np.mean(np.square(_band(x, *PHONE_BAND), dtype=np.float64))))
    if r > 0:
        x *= np.float32(_db(level_db) / r)
    fi, fo = int(0.02 * SR), min(int(INTRO_FADE * SR), len(x) // 2)
    x[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)
    x[-fo:] *= np.linspace(1, 0, fo, dtype=np.float32) ** 2
    return x


def _tension(ln: int, rng: np.random.Generator) -> np.ndarray:
    """Напряжение под письмом (владелец 08.11: «добавь помехи, напряжение в эфире»): низкий гул, который медленно
    ползёт вверх и нарастает; тонкий писк в ушах к концу; в последней трети — стук сердца, всё чаще (с обертонами
    110-220 Гц — иначе в динамике телефона его не слышно). float32."""
    out = np.zeros(ln, dtype=np.float32)
    span = max(ln / SR, 1.0)
    for p0 in range(0, ln, GEN_SEG):  # кусками по 10 с — без массивов на весь эфир
        n = min(GEN_SEG, ln - p0)
        t = (np.arange(n, dtype=np.float64) + p0) / SR
        prog = t / span
        ph = 2 * np.pi * (52 * t + 22 * span * prog ** 2.5 / 2.5)            # 52 → 74 Гц, фаза непрерывна
        drone = np.sin(ph) + 0.6 * np.sin(1.012 * ph + 1.3) + 0.3 * np.sin(2.003 * ph)
        drone *= (0.2 + 0.8 * prog ** 1.3) / 1.9                              # нарастает к концу
        phw = 2 * np.pi * (7800 * t + 600 * span * prog ** 2 / 2)             # писк 7.8 → 8.4 кГц
        whine = np.sin(phw) * np.clip((prog - 0.55) / 0.45, 0, 1) * 0.05
        out[p0:p0 + n] = (drone + whine).astype(np.float32)
    t0 = 0.62 * span  # сердце: с последней трети, 64 → 96 ударов в минуту
    beat = np.arange(int(0.16 * SR)) / SR

    def thump(f: float, amp: float) -> np.ndarray:
        return (amp * (np.sin(2 * np.pi * f * beat) * np.exp(-beat * 28)
                       + 0.5 * np.sin(2 * np.pi * 2 * f * beat) * np.exp(-beat * 36)
                       + 0.3 * np.sin(2 * np.pi * 4 * f * beat) * np.exp(-beat * 50))).astype(np.float32)

    lub, dub = thump(55, 1.0), thump(47, 0.7)
    tt = t0
    while tt < span - 0.4:
        prog = (tt - t0) / max(span - t0, 1e-3)
        for at, snd in ((tt, lub), (tt + 0.17, dub)):
            i = int(at * SR)
            k = min(len(snd), ln - i)
            if k > 0:
                out[i:i + k] += snd[:k] * (0.5 + 0.5 * prog)
        tt += 60 / (64 + 32 * prog)
    return out


def _pad(ln: int, rng: np.random.Generator) -> np.ndarray:
    """Мрачный фон под письмом, который слышно и в динамике телефона (гармоники до ≈ 1.2 кГц; гул 50-70 Гц
    телефон не играет): корень, тритон, октава и малая нона над корнем, каждый голос — пара чуть расстроенных тонов
    (медленные биения), волны громкости раз в 6-13 с, к концу вдвое громче. float32, пик ≈ 1."""
    f0 = float(rng.choice([98.0, 103.83, 110.0]))
    voices = [(f0, 1.0), (f0 * 2 ** (6 / 12), 0.7), (f0 * 2, 0.45), (f0 * 2 ** (13 / 12), 0.3)]
    lfo = [(rng.uniform(1 / 13, 1 / 6), rng.uniform(0, 2 * np.pi)) for _ in voices]
    span = max(ln / SR, 1.0)
    out = np.zeros(ln, dtype=np.float32)
    for p0 in range(0, ln, GEN_SEG):
        n = min(GEN_SEG, ln - p0)
        t = (np.arange(n, dtype=np.float64) + p0) / SR
        seg = np.zeros(n)
        for (f, amp), (lf, lph) in zip(voices, lfo, strict=True):
            swell = amp * (0.6 + 0.4 * np.sin(2 * np.pi * lf * t + lph))
            for det in (0.9985, 1.0015):
                for k in range(1, 6):
                    seg += swell * np.sin(2 * np.pi * f * det * k * t + k) / k
        seg *= 0.5 + 0.5 * np.clip(t / span, 0, 1) ** 1.2
        out[p0:p0 + n] = seg.astype(np.float32)
    return _peak1(out)  # на месте — без копий на весь фон


def _levels(x: np.ndarray, frame: int) -> np.ndarray:
    """Уровень (дБ) по кадрам длиной frame. Кусками по ~1 млн сэмплов — без копии всего сигнала во float64."""
    m = len(x) // frame
    out = np.empty(m)
    step = max(1, (1 << 20) // frame)
    for i in range(0, m, step):
        k = min(step, m - i)
        blk = x[i * frame:(i + k) * frame].reshape(k, frame)
        out[i:i + k] = 10 * np.log10(np.mean(np.square(blk, dtype=np.float64), axis=1) + 1e-12)
    return out


def _band_levels(x: np.ndarray, lo: float, hi: float, frame: int) -> np.ndarray:
    """Уровни (дБ) по кадрам в полосе lo-hi — фильтр по кускам 30 с, без копии всего сигнала."""
    seg = BED_SEG // frame * frame
    return np.concatenate([_levels(_band(x[i:i + seg], lo, hi), frame) for i in range(0, len(x), seg)] or
                          [np.zeros(0)])


def _quiet_runs(lv: np.ndarray, below: float, min_frames: int) -> list[tuple[int, int]]:
    """Отрезки кадров тише below длиной от min_frames — паузы в речи."""
    runs, i, m = [], 0, len(lv)
    while i < m:
        if lv[i] < below:
            j = i
            while j < m and lv[j] < below:
                j += 1
            if j - i >= min_frames:
                runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def _duck(bed: np.ndarray, voice: np.ndarray, depth_db: float) -> None:
    """Фон приседает под словами и поднимается в паузах (как под ведущим на радио): слова не тонут в музыке.
    Быстро вниз (~30 мс), медленно вверх (~0.4 с). bed и voice — одной длины, на месте."""
    frame = SR // 100
    lv = _levels(voice, frame)
    if len(lv) < 2:
        return
    act = np.clip((lv + 45) / 15, 0, 1)  # 0 — тишина (тише -45 дБ), 1 — речь (громче -30 дБ)
    att, rel = 1 - np.exp(-1 / 3), 1 - np.exp(-1 / 40)
    g, cur = np.empty(len(act)), 0.0
    for i, a in enumerate(act):
        cur += (a - cur) * (att if a > cur else rel)
        g[i] = cur
    gain = 10 ** (depth_db * g / 20)
    centers = np.arange(len(lv)) * frame + frame / 2
    for a in range(0, len(bed), 1 << 20):
        b = min(a + (1 << 20), len(bed))
        bed[a:b] *= np.interp(np.arange(a, b), centers, gain).astype(np.float32)


def _slice(x: np.ndarray, a: int, b: int) -> np.ndarray:
    """x[a:b] с нулями за краями."""
    out = np.zeros(max(0, b - a), dtype=np.float32)
    lo, hi = max(a, 0), min(b, len(x))
    if hi > lo:
        out[lo - a:hi - a] = x[lo:hi]
    return out


def _peak1(x: np.ndarray) -> np.ndarray:
    x /= max(float(x.max()), float(-x.min())) or 1
    return x


def _rms_to(x: np.ndarray, db: float) -> np.ndarray:
    """Привести x к громкости db (RMS, дБ) — на месте."""
    r = float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if len(x) else 0.0
    if r > 0:
        x *= np.float32(_db(db) / r)
    return x


def _speech_db(v: np.ndarray) -> float:
    """Громкость речи (дБ): средняя по кадрам 20 мс, где говорят (не тише пика на 30 дБ)."""
    lv = _levels(v, SR // 50)
    if not len(lv):
        return -30.0
    act = lv[lv > lv.max() - 30]
    return float(10 * np.log10(np.mean(10 ** (act / 10))))


# ---------- голос ведущего «как на FM» и подложка под ним ----------
# Владелец 08.11: «найди, как разговаривают ведущие». У настоящих FM-ведущих голос обработан: эквалайзер (тепло у
# микрофона, разборчивость 3-5 кГц, «воздух»), плотная компрессия 4:1 и лёгкое насыщение — ровный и «в лицо»; и
# говорят они поверх музыкальной подложки (talk bed), а не в тишине.

def _radio_eq(f: np.ndarray) -> np.ndarray:
    lf = np.log2(np.maximum(f, 1.0))
    db = (3.0 / (1 + np.exp((lf - np.log2(150)) * 5))           # тепло «у самого микрофона»
          - 2.0 * np.exp(-((lf - np.log2(380)) / 0.6) ** 2)      # без «бубнежа»
          + 3.5 * np.exp(-((lf - np.log2(3800)) / 0.75) ** 2)    # разборчивость
          + 2.0 / (1 + np.exp(-(lf - np.log2(9500)) * 5)))       # воздух
    return 10 ** (db / 20) * _gain(f, 60, 14000)


def _on_air(v: np.ndarray) -> np.ndarray:
    """Голос ведущего как у FM-ведущих: эквалайзер, компрессия 4:1, мягкое насыщение."""
    v = _peak1(_filter(v, _radio_eq))
    v = _peak1(_compress(v, -26, 4.0))
    return (np.tanh(v * 1.6) / np.tanh(1.6)).astype(np.float32)


BED_BPM = 84
_CHORDS = [  # ля минор, как позывной: Am7 – Fmaj7 – Cmaj7 – G6, по такту; (бас, ноты аккорда), Гц
    (110.0, (220.0, 261.63, 329.63, 392.0)),
    (87.31, (174.61, 220.0, 261.63, 329.63)),
    (130.81, (261.63, 329.63, 392.0, 493.88)),
    (98.0, (196.0, 246.94, 293.66, 329.63)),
]


def _ep(f: float, ln: float) -> np.ndarray:
    """Нота электропиано: мягкий тон, затухающие обертоны и звонкая атака «язычка»."""
    t = np.arange(int(ln * SR)) / SR
    tone = (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(4 * np.pi * f * t) * np.exp(-t * 3)
            + 0.08 * np.sin(6 * np.pi * f * t) * np.exp(-t * 5)
            + 0.15 * np.sin(2 * np.pi * 7.03 * f * t) * np.exp(-t * 28))
    return (tone * np.minimum(1, t / 0.004) * np.exp(-t * 1.1)).astype(np.float32)


def _talk_bed(n: int, rng: np.random.Generator, chords: list | None = None, bpm: float = BED_BPM,
              drums: bool = True) -> np.ndarray:
    """Подложка под ведущего: спокойный вечерний грув — электропиано по ля-минору, бас, мягкая бочка, щелчок и
    тарелочка с лёгким свингом. float32, пик ≈ 1. chords/bpm — другая гармония и темп (весёлая — у сплетен);
    drums=False — без барабанов (мрачная подложка после письма)."""
    chords = chords or _CHORDS
    beat = 60 / bpm
    bar = 4 * beat
    out = np.zeros(n + int(3 * SR), dtype=np.float32)
    tb = np.arange(int(0.3 * SR)) / SR
    kick = (np.sin(2 * np.pi * np.cumsum(45 + 75 * np.exp(-tb * 30)) / SR) * np.exp(-tb * 14)).astype(np.float32)
    ts = np.arange(int(0.12 * SR)) / SR
    snap = _peak1(_band(rng.standard_normal(len(ts)).astype(np.float32), 1500, 5000))
    snap = (snap * np.exp(-ts * 45) + 0.6 * np.sin(2 * np.pi * 190 * ts) * np.exp(-ts * 35)).astype(np.float32)
    th = np.arange(int(0.05 * SR)) / SR
    hat = (_peak1(_band(rng.standard_normal(len(th)).astype(np.float32), 7000, 14000)) * np.exp(-th * 70))
    hat = hat.astype(np.float32)
    notes_cache: dict[tuple[float, float], np.ndarray] = {}

    def put(snd: np.ndarray, at: float, amp: float) -> None:
        p = int(at * SR)
        if p < len(out):
            k = min(len(snd), len(out) - p)
            out[p:p + k] += snd[:k] * np.float32(amp)

    i = 0
    while i * bar * SR < n:
        t0 = i * bar
        root, notes = chords[i % len(chords)]
        for hit, ln, amp in ((0.0, 2.8, 0.2), (1.5 * beat, 1.8, 0.12), (3.0 * beat, 1.2, 0.08)):
            for j, f in enumerate(notes):
                key = (f, ln)
                if key not in notes_cache:
                    notes_cache[key] = _ep(f, ln)
                put(notes_cache[key], t0 + hit + 0.011 * j, amp * (1 - 0.1 * j))   # аккорд «перебором»
        for hit in (0.0, 2 * beat):
            t = np.arange(int(1.9 * beat * SR)) / SR
            bass = (np.sin(2 * np.pi * root * t) + 0.25 * np.sin(4 * np.pi * root * t)) * np.minimum(1, t / 0.01)
            put((bass * np.exp(-t * 1.4)).astype(np.float32), t0 + hit, 0.3)
            if drums:
                put(kick, t0 + hit, 0.45)
        for hit in (beat, 3 * beat) if drums else ():
            put(snap, t0 + hit, 0.1)
        for k in range(8 if drums else 0):
            put(hat, t0 + k * beat / 2 + (0.012 if k % 2 else 0.0), 0.05 if k % 2 else 0.03)
        i += 1
    return _peak1(_band(out[:n], 35, 9000))


# ---------- эфир: настоящие радиопомехи ----------
# Владелец 08.11: «сделай именно помехи». Как звучит эфир (справочники по радиоприёму и звуковому дизайну): шипение
# слабого сигнала; треск атмосферных разрядов — пачки щелчков; свист соседней станции и «вой» коротких волн, который
# плывёт и не может устояться; замирания — уровень сигнала плавает, при селективном замирании звук «закручивается»,
# при пульсирующем — почти вибрирует; провал — вместо голоса шипение; настройка — свист и обрывки чужих станций.
# Слова при этом не рвём (прошлая жалоба — «прерывисто, неразборчиво»): над речью — только плавные замирания до -7 дБ,
# шипение и негромкий треск; громкое (разряды, провалы, «чужой голос») — в паузах.

def _hiss_curve(f: np.ndarray) -> np.ndarray:
    return _gain(f, 300, 10000) * np.sqrt(np.clip(f, 300, 9000) / 3000)


def _hiss(n: int, rng: np.random.Generator) -> np.ndarray:
    """Шипение эфира (слабый сигнал): яркий шум 0.3-10 кГц, RMS ≈ 1. Для коротких вставок (до ~30 с)."""
    x = _filter(rng.standard_normal(n, dtype=np.float32), _hiss_curve)
    r = float(np.sqrt(np.mean(np.square(x[:SR * 10], dtype=np.float64)))) or 1.0
    return x / np.float32(r)


def _hiss_into(mix: np.ndarray, level_db: np.ndarray, rng: np.random.Generator) -> None:
    """Шипение по всему выпуску прямо в микс: кусками по 30 с с плавными стыками (без массива на весь выпуск —
    5 минут иначе съедали лишние сотни МБ). level_db — громкость по кадрам 10 мс."""
    n = len(mix)
    centers = np.arange(len(level_db)) * FADE_FRAME + FADE_FRAME / 2
    xf = BED_XF
    up = np.sqrt(np.linspace(0, 1, xf, dtype=np.float32))
    down = np.sqrt(np.linspace(1, 0, xf, dtype=np.float32))
    for pos in range(0, n, BED_SEG):
        ln = min(BED_SEG + xf, n - pos)                    # кусок + нахлёст на следующий
        seg = _hiss(ln, rng)
        if pos:
            seg[:xf] *= up[:ln]
        if pos + ln < n:
            seg[-xf:] *= down
        seg *= (10 ** (np.interp(np.arange(pos, pos + ln), centers, level_db) / 20)).astype(np.float32)
        mix[pos:pos + ln] += seg


def _crash(rng: np.random.Generator, sec: float | None = None) -> np.ndarray:
    """Атмосферный разряд: пачка щелчков, затухающая за 0.1-0.4 с. Пик ≈ 1."""
    n = int((sec or rng.uniform(0.1, 0.4)) * SR)
    out = np.zeros(n, dtype=np.float32)
    pos = 0
    while pos < n:
        k = int(rng.integers(20, 200))
        m = min(k, n - pos)
        click = rng.standard_normal(m) * np.exp(-np.arange(m) / (k / 4))
        out[pos:pos + m] += (click * rng.uniform(0.3, 1.0) * np.exp(-pos / (n / 3))).astype(np.float32)
        pos += k + int(rng.exponential(0.012 * SR))
    return _peak1(_band(out, 250, 6000))


def _whistle(sec: float, rng: np.random.Generator, f0: float, glide: float = 0.0) -> np.ndarray:
    """Свист соседней станции / «вой» коротких волн: тон плывёт и дрожит, не может устояться; входит и уходит
    плавно. RMS ≈ 0.6."""
    n = int(sec * SR)
    t = np.arange(n) / SR
    walk = np.cumsum(rng.standard_normal(int(sec * 10) + 2)) * 25          # дрейф, Гц (шаг 0.1 с)
    f = f0 + np.interp(t, np.arange(len(walk)) * 0.1, walk) + glide * t / sec + 4 * np.sin(2 * np.pi * 5.5 * t)
    env = np.clip(np.minimum(t, sec - t) / 1.2, 0, 1) ** 2
    return (np.sin(2 * np.pi * np.cumsum(f) / SR) * env).astype(np.float32)


def _phase(x: np.ndarray, depth: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Селективное замирание: звук «закручивается» (гребенчатый фильтр, задержка 0.3-3 мс плывёт). depth 0..1."""
    n = len(x)
    t = np.arange(n) / SR
    d = (0.3 + 2.7 * (0.5 + 0.5 * np.sin(2 * np.pi * 0.35 * t + rng.uniform(0, 6)))) * 1e-3 * SR
    idx = np.arange(n) - d
    base = np.floor(idx)
    i0 = np.clip(base.astype(np.int64), 0, n - 1)
    frac = (idx - base).astype(np.float32)
    late = x[i0] * (1 - frac) + x[np.minimum(i0 + 1, n - 1)] * frac
    g = (0.8 * depth).astype(np.float32)
    return ((x + g * late) / (1 + 0.4 * g)).astype(np.float32)


def _tune(sec: float, rng: np.random.Generator, voice: np.ndarray) -> np.ndarray:
    """Настройка на волну: шипение, свист проносится сверху вниз, обрывки чужих станций, в конце «поймали» — шум
    уходит. Пик ≈ 1."""
    n = int(sec * SR)
    t = np.arange(n) / SR
    out = _hiss(n, rng) * np.float32(0.25)
    f = 350 + 2600 * (1 - t / sec) ** 1.6 + 300 * np.sin(2 * np.pi * 1.3 * t)
    out += (np.sin(2 * np.pi * np.cumsum(f) / SR) * 0.3).astype(np.float32)
    if len(voice) > 2 * SR:
        for at in rng.uniform(0.1, max(0.2, sec - 0.45), size=3):       # обрывки чужих станций
            k = int(rng.uniform(0.07, 0.15) * SR)
            src = int(rng.integers(0, len(voice) - k))
            snip = _peak1(_band(voice[src:src + k].copy(), 400, 3000)) * np.hanning(k).astype(np.float32)
            p = int(at * SR)
            m = min(k, n - p)
            out[p:p + m] += snip[:m] * np.float32(0.6)
    out *= np.minimum(1, t / 0.15).astype(np.float32)                   # вход
    out *= (np.clip((sec - t) / 0.25, 0, 1) ** 2).astype(np.float32)    # «поймали» — шум уходит
    return _peak1(out)


def _ghost(src: np.ndarray, k: int) -> np.ndarray:
    """«Чужой голос» в помехах: обрывок речи задом наперёд, медленнее и ниже, глухо — как из соседнего эфира."""
    m = max(2, int(k * 0.85))
    seg = src[:m][::-1]
    slow = np.interp(np.arange(k) * (m / k), np.arange(len(seg)), seg).astype(np.float32)   # 0.85× — ниже
    return _peak1(_band(slow, 400, 2800)) * np.hanning(k).astype(np.float32)


FADE_FRAME = SR // 100  # замирания считаем по кадрам 10 мс
GHOST_ROOM = 0.7        # на сколько секунд удлиняем одну паузу письма — там в эфире прорезается «чужой голос»


def _make_room(v: np.ndarray, extra: float = GHOST_ROOM, avoid: tuple[int, int] | None = None) -> tuple[np.ndarray, int]:
    """Одну паузу между фразами (55-80 % письма, не в последних 12 с) удлиняем на extra с: паузы у голоса короткие
    (0.3-0.8 с), а сбою эфира с «чужим голосом» нужна секунда. Возвращает (голос, где начинается вставка или -1)."""
    frame = SR // 50
    lv = _levels(v, frame)
    loud = lv > -35
    if not loud.any():
        return v, -1
    ref = 10 * np.log10(np.mean(10 ** (lv[loud] / 10)))
    last = len(lv) - int(12 * SR / frame)
    cand = [r for r in _quiet_runs(lv, ref - 30, int(0.35 * SR / frame))
            if 0.55 * len(lv) < r[0] < min(0.8 * len(lv), last)
            and not (avoid and r[0] * frame < avoid[1] + SR and r[1] * frame > avoid[0] - SR)]
    if not cand:
        return v, -1
    a, b = max(cand, key=lambda r: r[1] - r[0])
    mid = (a + b) // 2 * frame
    return np.concatenate([v[:mid], np.zeros(int(extra * SR), dtype=np.float32), v[mid:]]), mid


def _fading(m: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Замирания сигнала над письмом по кадрам 10 мс (m кадров): (дБ ≤ 0, глубина «закручивания» 0..1). Уровень
    плавает ±1.2 дБ; раз в 8-15 с (к концу чаще) — замирание на 4-7 дБ: вниз 0.5 с, держится 0.3-1 с, вверх 0.8 с;
    одно пульсирующее — сигнал «вибрирует» 1.2 с. Первые 4 с — чисто."""
    t = np.arange(m) / 100
    db = np.zeros(m)
    for _ in range(3):
        db += rng.uniform(0.3, 0.6) * np.sin(2 * np.pi * rng.uniform(0.07, 0.3) * t + rng.uniform(0, 6))
    depth = np.zeros(m)
    pos, deep = int(rng.uniform(4, 8) * 100), []
    while pos < m - 250:
        shape = np.concatenate([np.linspace(0, 1, 50) ** 2, np.ones(int(rng.uniform(30, 100))),
                                np.linspace(1, 0, 80) ** 2])
        k = min(len(shape), m - pos)
        db[pos:pos + k] -= rng.uniform(4, 7) * shape[:k]
        depth[pos:pos + k] = np.maximum(depth[pos:pos + k], shape[:k])
        deep.append(pos)
        pos += len(shape) + int(rng.uniform(8, 15) * (1 - 0.35 * pos / m) * 100)
    if m > 1500:  # пульсирующее замирание — в последней трети
        p = int(rng.uniform(0.66, 0.9) * m) - 120
        tt = np.arange(120) / 100
        db[p:p + 120] -= 2.0 * (0.5 + 0.5 * np.sin(2 * np.pi * 7 * tt)) * np.sin(np.pi * tt / 1.2)
    db[:300] = np.minimum(db[:300], 0) * np.linspace(0, 1, min(300, m))[:len(db[:300])]
    return np.minimum(db, 0.0), depth


LETTER_IN = 1.7     # пауза перед письмом: настройка на волну (свист, обрывки чужих станций), фон входит
LETTER_OUT = 1.9    # после письма: обрыв сигнала, тишина — потом ведущий
# Уровни — от громкости речи. Фон меряем в полосе, которую играет динамик телефона (PHONE_BAND): по всей полосе
# он выходил на 30-38 дБ тише голоса — в телефоне его просто не было слышно.
PHONE_BAND = (250, 8000)
PAD_DB = -15.0      # мрачный фон к концу письма, в паузах (под словами ещё на DUCK_DB тише)
RUMBLE_DB = -19.0   # низкий гул и сердце (по всей полосе — их слышно в наушниках)
DUCK_DB = -9.0
TALK_BED_DB = -19.0     # подложка под ведущим (полоса телефона, в паузах; под словами ещё на 7 дБ тише)
HISS_HOST_DB = -34.0    # шипение эфира под ведущим — еле-еле, просто «радио»
HISS_DB = -26.0         # шипение над письмом — слышно всё время; в замираниях растёт (HISS_RISE дБ на дБ)
HISS_RISE = 1.3
CRASH_PAUSE_DB = -8.0   # пик разряда в паузе (от громкости речи)
CRASH_SPEECH_DB = -20.0  # поверх слов — тихо
CLICK_DB = -30.0        # мелкий треск эфира, ≈ 2.5 щелчка в секунду
WHISTLE_DB = -26.0      # свист соседней станции (полоса телефона)
GHOST_DB = -20.0        # «чужой голос» в паузах
TUNE_DB = -8.0          # настройка на волну перед письмом


# ---------- звуки по сюжету, звонок в студию, музыка под письмом (владелец 08.11: «1, 2, 5») ----------
SFX_DB = -4.0        # звук по сюжету — на 4 дБ тише речи письма (полоса телефона), в своей паузе
MUSIC_DB = -16.0     # музыка ElevenLabs под письмом — в паузах на 16 дБ тише речи (под словами ещё на DUCK_DB)
RING_DB = -5.0       # звонок студийного телефона — от громкости ведущего
BUSY_DB = -9.0       # короткие гудки после обрыва связи — от громкости письма (тише звонка: это фон, а не событие)
CALL_DB = 0.5        # звонящий — чуть громче письма, но тише ведущего


def decode(data: bytes) -> np.ndarray | None:
    """MP3/OGG/WAV → float32 моно 48 кГц (звуки и музыка ElevenLabs). None — не декодировалось."""
    try:
        import av

        with av.open(io.BytesIO(data)) as c:
            st = c.streams.audio[0]
            rate = st.codec_context.sample_rate
            chunks = []
            for fr in c.decode(st):
                a = fr.to_ndarray()
                if a.dtype.kind == "i":
                    a = a.astype(np.float32) / 32768
                n_ch = len(fr.layout.channels)
                a = a.reshape(n_ch, -1).mean(axis=0) if fr.format.is_planar else a.reshape(-1, n_ch).mean(axis=1)
                chunks.append(a.astype(np.float32))
        x = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        x = _resample((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes(), rate)
        return x if len(x) > SR // 10 else None
    except Exception as e:  # noqa: BLE001
        log.warning("radio: звук не декодировался: %s", e)
        return None


def _insert_sfx(v: np.ndarray, events: list[tuple[float, np.ndarray]]) -> tuple[np.ndarray, list[tuple[int, np.ndarray]]]:
    """Звуки по сюжету: для каждого (время в письме, звук) — ближайшая пауза после этого места; в неё вставляем
    тишину длиной со звук (слова не перекрываем). Возвращает (голос, [(где звук в новом голосе, звук)])."""
    frame = SR // 50
    lv = _levels(v, frame)
    loud = lv > -35
    if not events or not loud.any():
        return v, []
    ref = 10 * np.log10(np.mean(10 ** (lv[loud] / 10)))
    pauses = _quiet_runs(lv, ref - 30, int(0.2 * SR / frame))
    out, placed, last, length = [], [], 0, 0
    for t, snd in sorted(events, key=lambda e: e[0]):
        target = int(t * SR / frame)
        cand = [r for r in pauses if target - int(0.8 * SR / frame) <= r[0] <= target + int(3.0 * SR / frame)
                and r[0] * frame > last]
        if not cand:
            continue
        a, b = min(cand, key=lambda r: abs(r[0] - target))
        cut = (a + min(b - a, int(0.15 * SR / frame))) * frame
        out.append(v[last:cut])
        length += cut - last
        gap = len(snd) + int(0.45 * SR)
        out.append(np.zeros(gap, dtype=np.float32))
        placed.append((length + int(0.15 * SR), snd))
        length += gap
        last = cut
    out.append(v[last:])
    return np.concatenate(out).astype(np.float32), placed


def _phone(v: np.ndarray) -> np.ndarray:
    """Звонящий в студию: телефонная полоса 320-3300 Гц, плотная компрессия, лёгкий перегруз линии."""
    v = _band(v, 320, 3300)
    v = _peak1(_compress(_peak1(v), -24, 3.0))
    return (np.tanh(v * 2.2) / np.tanh(2.2)).astype(np.float32)


def _ring() -> np.ndarray:
    """Звонок студийного телефона: две трели (440+480 Гц с дрожью 20 Гц) и щелчок снятой трубки."""
    t = np.arange(int(0.9 * SR)) / SR
    trill = (np.sin(2 * np.pi * 440 * t) + np.sin(2 * np.pi * 480 * t)) * (0.55 + 0.45 * np.sign(np.sin(2 * np.pi * 20 * t)))
    env = np.minimum(1, t / 0.015) * np.minimum(1, (t[-1] - t) / 0.04)
    burst = (_band(trill * env, 300, 4000) * 0.5).astype(np.float32)
    click = np.zeros(int(0.03 * SR), dtype=np.float32)
    click[:240] = np.random.default_rng(3).standard_normal(240).astype(np.float32) * np.exp(-np.arange(240) / 40)
    gap = np.zeros(int(0.5 * SR), dtype=np.float32)
    out = np.concatenate([burst, gap, burst * np.float32(0.95), np.zeros(int(0.45 * SR), dtype=np.float32), click])
    return _peak1(out)


def _busy(beeps: int = 4) -> np.ndarray:
    """Короткие гудки, как после оборванного звонка: 425 Гц (тон российской телефонной сети), 0.35 с гудок /
    0.35 с тишина, в телефонной полосе и чуть с перегрузом линии."""
    on, off = int(0.35 * SR), int(0.35 * SR)
    t = np.arange(on) / SR
    env = np.minimum(1, t / 0.008) * np.minimum(1, (t[-1] - t) / 0.008)
    tone = np.tanh(1.6 * np.sin(2 * np.pi * 425 * t)) * env
    one = np.concatenate([tone, np.zeros(off)]).astype(np.float32)
    return _peak1(_band(np.tile(one, beeps)[:-off // 2], 300, 3400))


def _line_drop(seed: int = 0) -> np.ndarray:
    """Обрыв связи: голос режется, короткий цифровой хрип линии (рваный шум с щелчками, 0.12 с) и щелчок."""
    rng = np.random.default_rng(seed + 11)
    n = int(0.12 * SR)
    burst = rng.standard_normal(n).astype(np.float32)
    burst *= (rng.random(n // 240 + 1) > 0.35).repeat(240)[:n]          # рваный: куски шума с дырами
    burst *= np.exp(-np.arange(n) / (0.06 * SR)).astype(np.float32)
    burst = _band(burst, 400, 4000)
    click = np.zeros(int(0.02 * SR), dtype=np.float32)
    click[:180] = rng.standard_normal(180).astype(np.float32) * np.exp(-np.arange(180) / 30)
    return _peak1(np.concatenate([_peak1(burst) * np.float32(0.8), click]))


def _cut_tail(v: np.ndarray) -> np.ndarray:
    """Голос без тишины в конце — чтобы обрыв звучал обрывом, а не паузой после фразы."""
    frame = SR // 100
    lv = _levels(v, frame)
    loud = np.nonzero(lv > (lv.max() - 35 if len(lv) else 0))[0]
    if not len(loud):
        return v
    end = min(len(v), (loud[-1] + 1) * frame + int(0.02 * SR))
    out = v[:end].copy()
    k = min(len(out), int(0.006 * SR))
    if k:
        out[-k:] *= np.linspace(1, 0, k, dtype=np.float32)  # 6 мс — без щелчка цифрового среза, но резко
    return out


def _tape(v: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """«Расшифровка диктофона»: голос как с карманного диктофона — полоса 180-6500 Гц, лёгкое плавание скорости
    (дрожь ленты), шипение плёнки, мягкий перегруз."""
    v = _band(v, 180, 6500)
    n = len(v)
    t = np.arange(n) / SR
    wob = 1 + 0.0025 * np.sin(2 * np.pi * 0.6 * t) + 0.001 * np.sin(2 * np.pi * 5.5 * t)
    idx = np.clip(np.cumsum(wob) - wob[0], 0, n - 1)
    v = np.interp(idx, np.arange(n), v).astype(np.float32)
    v = _peak1(v)
    hiss = _band(rng.standard_normal(n).astype(np.float32), 2000, 8000)
    v = v + hiss / (np.abs(hiss).max() or 1) * np.float32(_db(-38))
    return (np.tanh(v * 1.5) / np.tanh(1.5)).astype(np.float32)


def _cut_at(v: np.ndarray, t: float) -> int:
    """Где разрезать голос около секунды t: ближайшая пауза после (или чуть до) t — чтобы не по слову."""
    frame = SR // 50
    lv = _levels(v, frame)
    loud = lv > -35
    if not loud.any():
        return min(len(v), int(t * SR))
    ref = 10 * np.log10(np.mean(10 ** (lv[loud] / 10)))
    target = int(t * SR / frame)
    cand = [r for r in _quiet_runs(lv, ref - 30, int(0.25 * SR / frame))
            if target - int(1.0 * SR / frame) <= r[0] <= target + int(5.0 * SR / frame)]
    if not cand:
        return min(len(v), int(t * SR))
    a, b = min(cand, key=lambda r: abs(r[0] - target))
    return (a + min(b - a, int(0.2 * SR / frame))) * frame


def _interrupt(chunks: list[np.ndarray], letter_db: float, drop: bool = False) -> np.ndarray:
    """Звонок посреди письма (владелец 08.11): тишина, звонок телефона, ведущий прерывается, звонящий (телефон),
    ведущий возвращается к чтению. chunks — [ведущий, звонящий, ведущий] (48 кГц).
    drop — связь обрывается (владелец 09.11: «не всегда обрывалась; оборвалась — звук гудка»): голос звонящего
    режется, хрип линии и короткие гудки, потом ведущий. Без обрыва — звонящий договорил, щелчок положенной трубки."""
    host_in, caller, host_back = (list(chunks) + [np.zeros(0, dtype=np.float32)] * 3)[:3]
    ring = _ring() * np.float32(_db(letter_db + RING_DB) / 0.35)
    out = [np.zeros(int(0.5 * SR), dtype=np.float32), ring, np.zeros(int(0.3 * SR), dtype=np.float32)]
    for v, kind, after in ((host_in, "host", 0.35), (caller, "call", 0.5), (host_back, "host", 0.8)):
        if not len(v):
            continue
        v = _on_air(_band(v, 70, 11000)) if kind == "host" else _phone(_band(v, 70, 11000))
        v *= np.float32(_db(letter_db + (1.5 if kind == "host" else CALL_DB) - _speech_db(v)))
        if kind == "call":
            level = _db(letter_db + CALL_DB) / 0.35
            if drop:   # оборвалось: на полуслове — хрип линии — короткие гудки — ведущий
                out += [_cut_tail(v), _line_drop() * np.float32(level),
                        np.zeros(int(0.22 * SR), dtype=np.float32),
                        _busy() * np.float32(_db(letter_db + BUSY_DB) / 0.35),
                        np.zeros(int(0.45 * SR), dtype=np.float32)]
                continue
            click = _line_drop()[-int(0.02 * SR):] * np.float32(level * 0.5)  # трубку положили: только щелчок
            out += [v, np.zeros(int(0.3 * SR), dtype=np.float32), click]
        else:
            out.append(v)
        out.append(np.zeros(int(after * SR), dtype=np.float32))
    return np.concatenate(out).astype(np.float32)


# Ведущий после письма (владелец 10.11, «7» из идей): раньше сразу возвращалась та же бодрая подложка, что в начале.
# Теперь первая фраза (роль host_soft — тише и медленнее) — в тишине, потом вступает мрачная подложка: без барабанов,
# медленнее, ля минор с доминантой ми мажор, тише обычной.
_DARK_CHORDS = [
    (55.0, (220.0, 261.63, 329.63)),      # Am
    (43.65, (174.61, 220.0, 261.63)),     # F
    (36.71, (146.83, 174.61, 220.0)),     # Dm
    (41.2, (164.81, 207.65, 246.94)),     # E
]
DARK_BPM = 62
DARK_BED_DB = -22.0     # мрачная подложка после письма — тише обычной (TALK_BED_DB)
DARK_BED_IN = 3.0       # вступает плавно, за столько секунд
SOFT_DB = -1.5          # первая фраза после письма — на 3 дБ тише ведущего (ведущий — +1.5 к письму)
ROOM_DB = -24.0         # фон места под письмом — от речи в полосе телефона (в паузах)
ROOM_DUCK_DB = -4.0     # под словами фон места почти не приседает — это «воздух» комнаты, а не музыка
THROUGH_DB = {"intercom": -1.0, "door": -6.0, "phone": -1.5, "speaker": -2.0}  # реплика «через предмет» к своей громкости


def _through_one(seg: np.ndarray, via: str, rng: np.random.Generator) -> np.ndarray:
    """Реплика «через предмет»: домофон — дешёвый динамик с хрипом и фоном линии; за дверью — глухо, только низ и
    короткий отзвук коробки; телефон — трубка; динамик — объявление в транспорте/на станции, с отзвуком зала."""
    if via == "intercom":
        x = _peak1(_band(seg, 480, 3000))
        x = np.tanh(x * 3.2) / np.tanh(3.2)
        t = np.arange(len(x)) / SR
        x = x + (0.02 * np.sin(2 * np.pi * 100 * t) + 0.012 * _band(rng.standard_normal(len(x)).astype(np.float32),
                                                                       1000, 4000)).astype(np.float32)
        return _band(x.astype(np.float32), 350, 3600)
    if via == "door":
        x = _band(seg, 60, 600)
        out = x.copy()
        for ms, g in ((9, 0.35), (17, 0.22), (29, 0.12)):  # короткий отзвук: голос «в коробке» за дверью
            k = int(ms * SR / 1000)
            out[k:] += x[:-k] * np.float32(g)
        return out
    if via == "phone":
        return _phone(seg)
    x = _peak1(_band(seg, 330, 4500))  # speaker
    x = (np.tanh(x * 2.0) / np.tanh(2.0)).astype(np.float32)
    out = x.copy()
    for ms, g in ((110, 0.3), (230, 0.16), (370, 0.08)):  # отзвук зала
        k = int(ms * SR / 1000)
        if k < len(x):
            out[k:] += x[:-k] * np.float32(g)
    return out


def _through(v: np.ndarray, spans: list[tuple[float, float, str]] | None, rng: np.random.Generator) -> np.ndarray:
    """Голоса «через предмет»: те секунды письма (по меткам ElevenLabs) — обработкой нужного предмета, громкость
    как у исходной речи (за дверью — тише), края — плавно за 25 мс."""
    if not spans:
        return v
    v = v.copy()
    xf = int(0.025 * SR)
    for t0, t1, via in spans:
        a, b = max(0, int(t0 * SR)), min(len(v), int(t1 * SR))
        if b - a < 4 * xf:
            continue
        seg = v[a:b].copy()
        x = _through_one(seg, via, rng)
        r0 = float(np.sqrt(np.mean(np.square(seg, dtype=np.float64)))) or 1.0
        r1 = float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) or 1.0
        x = (x * np.float32(r0 / r1 * _db(THROUGH_DB.get(via, 0.0)))).astype(np.float32)
        w = np.ones(b - a, dtype=np.float32)
        w[:xf], w[-xf:] = np.linspace(0, 1, xf), np.linspace(1, 0, xf)
        v[a:b] = seg * (1 - w) + x * w
    return v


def _loop(x: np.ndarray, n: int, xf: float = 3.0) -> np.ndarray:
    """Музыка нужной длины: короче — повторяем с плавным переходом xf с."""
    k = int(xf * SR)
    if len(x) >= n or len(x) <= 2 * k:
        return np.resize(x, n).astype(np.float32) if len(x) < n else x[:n].astype(np.float32)
    out = x.astype(np.float32).copy()
    ramp = np.linspace(0, 1, k, dtype=np.float32)
    while len(out) < n:
        head = out[:-k]
        tail = out[-k:] * (1 - ramp) + x[:k] * ramp
        out = np.concatenate([head, tail, x[k:]])
    return out[:n]


def studio(voice_chunks: list[bytes], styles: list[str], rate: int = 24000, seed: int = 0,
           sfx: list[tuple[float, np.ndarray]] | None = None, music: np.ndarray | None = None,
           interrupt: tuple[float, list[bytes]] | None = None, tape: bool = False,
           call_drop: bool = False, through: list[tuple[float, float, str]] | None = None,
           place: np.ndarray | None = None) -> np.ndarray:
    """«Письма Волгограда» как настоящий эфир (float32, 48 кГц, пик ≈ -1 дБ). styles — "host" / "letter" для каждого
    куска голоса ("host_call" — ведущий после звонка телефона, "call" — звонящий в студию, телефонная полоса); sfx —
    звуки по сюжету [(секунда в голосе письма, звук 48 кГц)] — каждый в свою паузу; music — музыка под письмом
    (ElevenLabs; нет — синтезированный фон _pad); interrupt — звонок посреди письма (секунда в голосе письма,
    [ведущий, звонящий, ведущий]); call_drop — связь со звонящим обрывается (хрип и короткие гудки); tape — письмо
    как с диктофона («расшифровка диктофона»); through — реплики «через предмет» [(с, по, intercom/door/phone/
    speaker)] в секундах голоса письма; place — фон места под письмом (ElevenLabs, петля); "host_soft" — первая
    фраза ведущего после письма: тише, в тишине, после неё — мрачная подложка. Ведущий — «эфирный» голос поверх музыкальной подложки; перед письмом — настройка на волну; над
    письмом — мрачный фон, напряжение и помехи эфира (замирания, шипение, треск, свист, провалы и «чужой голос» в
    паузах), слова не рвутся; перед последней фразой всё стихает — финал в тишине; потом обрыв сигнала и ведущий."""
    rng = np.random.default_rng(seed)
    # 1) голос: ведущий — обработка «как на FM», письмо — мягкая (тихое остаётся тихим); ведущий на 1.5 дБ громче
    parts, ghost_at, sfx_at, call_span = [], -1, [], None
    for i, pcm in enumerate(voice_chunks):
        v = _resample(pcm, rate)
        if not len(v):
            continue
        st = styles[i] if i < len(styles) else "host"
        v = _band(v, 70, 11000)
        if st in ("host", "host_call", "host_soft"):
            parts.append((_on_air(v), st))
        elif st == "call":
            parts.append((_phone(v), st))
        else:
            v = _peak1(_compress(_peak1(v), -22, 2.5))
            v = _through(v, through, rng)  # до вставок звуков и звонка: секунды — по исходному голосу письма
            if tape:
                v = _tape(v, rng)
            cut_t = interrupt[0] if interrupt else None
            if cut_t is not None:  # место звонка — до вставки звуков (время — по голосу письма)
                cut = _cut_at(v, cut_t)
            v, placed = _insert_sfx(v, sfx or [])
            if cut_t is not None:
                cut += sum(len(snd) + int(0.45 * SR) for p, snd in placed if p < cut)  # звуки до звонка сдвинули его
                ins = _interrupt([_resample(c, rate) for c in interrupt[1]], _speech_db(v), call_drop)
                v = np.concatenate([v[:cut], ins, v[cut:]]).astype(np.float32)
                placed = [(p + (len(ins) if p >= cut else 0), snd) for p, snd in placed]
                call_span = (cut, cut + len(ins))
            v, room = _make_room(v, avoid=call_span)
            if room >= 0:  # вставка «чужого голоса» сдвигает звуки и звонок после неё
                placed = [(p + (int(GHOST_ROOM * SR) if p >= room else 0), snd) for p, snd in placed]
                if call_span and call_span[0] >= room:
                    call_span = (call_span[0] + int(GHOST_ROOM * SR), call_span[1] + int(GHOST_ROOM * SR))
            parts.append((v, st))
            sfx_at = placed
            ghost_at = room  # где в письме вставлена пауза под «чужой голос» (-1 — не нашлось)
    letter_db = [_speech_db(v) for v, st in parts if st == "letter"]
    for v, st in parts:
        if st != "letter" and letter_db:
            v *= np.float32(_db(letter_db[0] + (CALL_DB if st == "call" else SOFT_DB if st == "host_soft" else 1.5)
                                - _speech_db(v)))
    pieces, spans, pos, ring_at = [], [], 0, []   # spans: (начало, конец, стиль) кусков внутри дорожки голоса
    ring = _ring()
    for v, st in parts:
        if pieces:
            prev = spans[-1][2]
            sec = (LETTER_IN if st == "letter" and prev != "letter" else
                   LETTER_OUT if prev == "letter" and st != "letter" else 0.45 if st == "call" else
                   0.55 if prev == "host_soft" else 0.7)
            gap = int(sec * SR)
            if st == "host_call":  # звонок в студию: телефон звонит в тишине, потом ведущий снимает трубку
                ring_at.append(pos + gap)
                gap += len(ring) + int(0.3 * SR)
            pieces.append(np.zeros(gap, dtype=np.float32))
            pos += gap
        pieces.append(v)
        spans.append((pos, pos + len(v), st))
        pos += len(v)
    voice = _peak1(np.concatenate(pieces).astype(np.float32)) if pieces else np.zeros(SR, dtype=np.float32)
    del pieces, parts

    # 2) микс: заставка владельца (или позывной), голос, позывной в конце
    host = [(a0, b0) for a0, b0, st in spans if st in ("host", "host_call")]
    ref_hp = (_speech_db(_band(np.concatenate([voice[a0:b0] for a0, b0 in host]), *PHONE_BAND))
              if host else -20.0)                            # громкость ведущего в полосе телефона
    head, tail = _intro(ref_hp + INTRO_DB), _jingle(False)
    if head is not None:
        start = len(head) - int(INTRO_OVERLAP * SR)          # ведущий вступает, пока заставка уходит
    else:
        head = _jingle(True) * np.float32(_db(-6))
        start = len(head) - int(0.9 * SR) + int(0.25 * SR)   # голос вступает, пока позывной ещё звенит
    n = start + len(voice) + int(0.6 * SR) + len(tail)
    mix = np.zeros(n, dtype=np.float32)
    mix[:len(head)] += head
    mix[start:start + len(voice)] += voice
    end = start + len(voice) + int(0.6 * SR)
    mix[end:end + len(tail)] += tail * _db(-8)
    hiss_db = np.full(n // FADE_FRAME + 2, np.nan)          # уровень шипения эфира по кадрам 10 мс (дБ)

    # 3) подложка под ведущим
    if host:
        for j, (a0, b0, st) in enumerate(spans):
            if st not in ("host", "host_call"):
                continue
            dark = j > 0 and spans[j - 1][2] == "host_soft"  # после письма и тихой фразы — мрачная, без барабанов
            a, b = max(0, start + a0 - (0 if dark else int(0.5 * SR))), min(n, start + b0 + int(1.0 * SR))
            bed = _talk_bed(b - a, rng, _DARK_CHORDS, DARK_BPM, drums=False) if dark else _talk_bed(b - a, rng)
            r = float(np.sqrt(np.mean(np.square(_band(bed, *PHONE_BAND), dtype=np.float64))))  # слышно в телефоне
            bed *= np.float32(_db(ref_hp + (DARK_BED_DB if dark else TALK_BED_DB)) / r) if r > 0 else np.float32(0)
            fi = min(int((DARK_BED_IN if dark else 0.4) * SR), (b - a) // 3)
            fo = min(int(1.2 * SR), (b - a) // 3)
            bed[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)
            bed[-fo:] *= np.linspace(1, 0, fo, dtype=np.float32)
            _duck(bed, _slice(voice, a - start, b - start), -7.0)
            mix[a:b] += bed
            hiss_db[a // FADE_FRAME:b // FADE_FRAME + 1] = ref_hp + HISS_HOST_DB
    for a0, b0, st in spans:  # звонящий и тихая фраза после письма — без подложки, шипение как под ведущим
        if st in ("call", "host_soft"):
            hiss_db[(start + a0) // FADE_FRAME:(start + b0) // FADE_FRAME + 1] = ref_hp + HISS_HOST_DB
    for at in ring_at:  # звонок студийного телефона — в тишине перед «Алло, вы в эфире»
        mix[start + at:start + at + len(ring)] += ring * np.float32(_db(ref_hp + RING_DB) / 0.35)

    letters = [(a0, b0) for a0, b0, st in spans if st == "letter"]
    if letters:
        la, lb = letters[0][0], letters[-1][1]               # письмо внутри дорожки голоса
        frame = SR // 50
        lv = _levels(voice[la:lb], frame)
        loud = lv > -35
        ref = 10 * np.log10(np.mean(10 ** (lv[loud] / 10))) if loud.any() else -18.0   # громкость речи, дБ
        lvp = _band_levels(voice[la:lb], *PHONE_BAND, frame)
        ref_phone = 10 * np.log10(np.mean(10 ** (lvp[loud] / 10))) if loud.any() else ref
        pauses = _quiet_runs(lv, ref - 30, int(0.3 * SR / frame))
        # последняя пауза перед финальной фразой (в последних 10 с): с неё всё стихает, финал — в тишине
        last = [r for r in pauses if r[1] < len(lv) - int(0.8 * SR / frame) and r[0] * frame > (lb - la) - 10 * SR]
        hush = la + (last[-1][0] * frame if last else max(0, lb - la - 3 * SR))

        # 4) мрачный фон, гул и сердце под письмом (приседают под словами)
        a = max(0, la - int(1.0 * SR))
        ln = hush + int(0.3 * SR) - a                        # гаснет за 0.3 с — внутри этой паузы
        tail_n = max(1, ln // 4)                             # уровень — по самой громкой (последней) четверти

        def level(x: np.ndarray, rel: float, to: float, band: tuple | None) -> np.ndarray:
            part = _band(x[-tail_n:], *band) if band else x[-tail_n:]
            r = float(np.sqrt(np.mean(np.square(part, dtype=np.float64)))) or 1.0
            x *= np.float32(_db(to + rel) / r)
            return x

        if music is not None and len(music) > 5 * SR:  # настоящая музыка (ElevenLabs) вместо синтезатора
            bed = level(_band(_loop(music, ln), 40, 12000), MUSIC_DB, ref_phone, PHONE_BAND)
        else:
            bed = level(_pad(ln, rng), PAD_DB, ref_phone, PHONE_BAND)
        bed += level(_tension(ln, rng), RUMBLE_DB, ref, None)   # гул и сердце — 47 Гц-8.4 кГц, фильтр не нужен
        fi, fo = min(int(2.0 * SR), ln // 3), min(int(0.3 * SR), ln // 3)
        bed[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)
        bed[-fo:] *= np.linspace(1, 0, fo, dtype=np.float32)
        _duck(bed, voice[a:a + ln], DUCK_DB)
        mix[start + a:start + a + ln] += bed
        del bed
        if place is not None and len(place) > 2 * SR:  # фон места (подъезд, гараж, двор…) — петля ElevenLabs
            amb = level(_band(np.resize(place, ln).astype(np.float32), 50, 12000), ROOM_DB, ref_phone, PHONE_BAND)
            amb[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)
            amb[-fo:] *= np.linspace(1, 0, fo, dtype=np.float32)
            _duck(amb, voice[a:a + ln], ROOM_DUCK_DB)
            mix[start + a:start + a + ln] += amb
            del amb

        # звуки по сюжету — каждый в своей паузе (вставлена под него), громкость — от речи в полосе телефона
        for p, snd in sfx_at:
            x = snd.astype(np.float32).copy()
            r = float(np.sqrt(np.mean(np.square(_band(x, *PHONE_BAND), dtype=np.float64)))) or 1.0
            x *= np.float32(_db(ref_phone + SFX_DB) / r)
            pk = float(np.abs(x).max()) or 1.0
            if pk > 0.9:
                x *= np.float32(0.9 / pk)
            k = min(len(x), int(0.03 * SR))
            if k:
                x[:k] *= np.linspace(0, 1, k, dtype=np.float32)
                x[-k:] *= np.linspace(1, 0, k, dtype=np.float32)
            c = start + la + p
            mix[c:c + len(x)] += x[:max(0, len(mix) - c)]

        # 5) эфир над письмом: замирания (голос и фон плавно уходят, шипение растёт, звук «закручивается»)
        s0, s1 = start + la, start + hush
        m = (s1 - s0) // FADE_FRAME
        fade_db, depth = _fading(m, rng)
        if call_span:  # звонок посреди письма — сигнал ровный (замирания плавно гаснут и возвращаются)
            mask = np.ones(m)
            mask[max(0, call_span[0] // FADE_FRAME):max(0, call_span[1] // FADE_FRAME)] = 0
            mask = np.convolve(np.pad(mask, (40, 40), mode="edge"), np.ones(81) / 81, mode="valid")[:m]
            fade_db, depth = fade_db * mask, depth * mask
        centers = np.arange(m) * FADE_FRAME + FADE_FRAME / 2
        for c0 in range(0, s1 - s0, 1 << 20):
            c1 = min(c0 + (1 << 20), s1 - s0)
            g = 10 ** (np.interp(np.arange(c0, c1), centers, fade_db) / 20)
            mix[s0 + c0:s0 + c1] *= g.astype(np.float32)
        on = np.flatnonzero(np.diff(np.concatenate([[0], (depth > 0.02).astype(int), [0]])))
        for w0, w1 in zip(on[::2], on[1::2], strict=True):  # «закручивание» — только там, где замирает
            p0, p1 = s0 + w0 * FADE_FRAME, min(s1, s0 + w1 * FADE_FRAME)
            dd = np.interp(np.arange(p1 - p0), np.arange(w1 - w0) * FADE_FRAME, depth[w0:w1]).astype(np.float32)
            mix[p0:p1] = _phase(mix[p0:p1].copy(), dd, rng)
        f0 = s0 // FADE_FRAME
        hiss_db[f0:f0 + m] = ref_phone + HISS_DB - HISS_RISE * fade_db
        if call_span:  # под звонком — шипение как под ведущим
            hiss_db[f0 + call_span[0] // FADE_FRAME:f0 + call_span[1] // FADE_FRAME] = ref_hp + HISS_HOST_DB
        hiss_db[s1 // FADE_FRAME:(start + lb) // FADE_FRAME + 30] = -90.0   # перед финальной фразой эфир стихает

        # настройка на волну — в паузе перед письмом
        tune = _tune(LETTER_IN - 0.15, rng, voice[:max(1, la - int(LETTER_IN * SR))])
        t0 = s0 - int(LETTER_IN * SR) + int(0.1 * SR)
        mix[t0:t0 + len(tune)] += tune * np.float32(_db(ref + TUNE_DB))
        hiss_db[t0 // FADE_FRAME:f0] = np.nan                # там свой шум

        sfx_spans = [(p - int(0.2 * SR), p + len(snd) + int(0.4 * SR)) for p, snd in sfx_at]
        if call_span:  # звонок посреди письма — без треска, провалов и свиста
            sfx_spans.append(call_span)

        def near_sfx(at: int, k: int) -> bool:  # треск и провалы не лезут на звуки по сюжету
            return any(at < b and at + k > a for a, b in sfx_spans)

        def in_pause(at: int, k: int) -> bool:  # at, k — в сэмплах внутри письма
            i0, i1 = at // frame, (at + k) // frame + 1
            return i1 <= len(lv) and bool(np.all(lv[i0:i1] < ref - 30)) and not near_sfx(at, k)

        # треск: мелкие щелчки всё время и разряды — громко в паузах, тихо поверх слов
        span_l = hush - la
        for at in np.sort(rng.uniform(0, span_l, size=int(span_l / SR * 2.5))):
            c = s0 + int(at)
            k = min(int(rng.integers(30, 150)), s1 - c)
            if k > 0:
                mix[c:c + k] += (rng.standard_normal(k) * np.exp(-np.arange(k) / (k / 5))
                                 * _db(ref + CLICK_DB) * rng.uniform(0.4, 1)).astype(np.float32)
        at = int(rng.uniform(1.5, 3.5) * SR)
        while at < span_l - SR:
            crash = _crash(rng)
            loud_at = in_pause(at, len(crash))
            c = s0 + at
            if not near_sfx(at, len(crash)):
                mix[c:c + len(crash)] += crash * np.float32(_db(ref + (CRASH_PAUSE_DB if loud_at else CRASH_SPEECH_DB))
                                                            * rng.uniform(0.6, 1))
            at += int(rng.exponential(4.5 * (1 - 0.3 * at / span_l)) * SR) + len(crash) + int(0.8 * SR)
        for p0, p1 in pauses:  # и почти в каждой длинной паузе — разряд
            if (p1 - p0 >= int(0.45 * SR / frame) and p0 * frame < span_l - SR and rng.random() < 0.6
                    and not near_sfx(p0 * frame, (p1 - p0) * frame)):
                crash = _crash(rng, min(0.35, (p1 - p0) * frame / SR - 0.12))
                c = s0 + p0 * frame + int(0.06 * SR)
                mix[c:c + len(crash)] += crash * np.float32(_db(ref + CRASH_PAUSE_DB) * rng.uniform(0.5, 1))
        # свист соседней станции — примерно раз в 38 с письма (не меньше двух), плывёт
        k_wh = max(2, int(round(span_l / SR / 38)))
        for lo, hi in ((0.08 + 0.84 * j / k_wh, 0.08 + 0.84 * (j + 0.8) / k_wh) for j in range(k_wh)):
            sec = float(rng.uniform(4, 7))
            c = s0 + int(rng.uniform(lo, hi) * span_l)
            if c + int(sec * SR) < s1 and not near_sfx(c - s0, int(sec * SR)):
                w = _whistle(sec, rng, float(rng.uniform(1100, 2600)), float(rng.uniform(-300, 300)))
                mix[c:c + len(w)] += w * np.float32(_db(ref_phone + WHISTLE_DB) / 0.6)
        # провал сигнала: шипение рвётся «забором» (как FM в движущейся машине)
        def drop(at: int, k: int) -> None:
            tt = np.arange(k) / SR
            chop = (0.55 + 0.45 * np.sign(np.sin(2 * np.pi * 14 * tt))) * np.hanning(k)
            mix[s0 + at:s0 + at + k] += _hiss(k, rng) * chop.astype(np.float32) * np.float32(_db(ref_phone - 12))

        # в удлинённой паузе — сбой: провал, и сквозь шипение «чужой голос» (речь задом наперёд, ниже и глуше)
        if 0 <= ghost_at < span_l - 3 * SR:
            k0 = int(0.22 * SR)
            drop(ghost_at + int(0.05 * SR), k0)
            k = int((GHOST_ROOM + 0.1) * SR)
            src = int(rng.integers(0, max(1, ghost_at - k)))
            g = _ghost(voice[la + src:la + src + k], k)
            mix[s0 + ghost_at + k0:s0 + ghost_at + k0 + k] += _rms_to(g, ref + GHOST_DB)
            gf = (s0 + ghost_at) // FADE_FRAME
            hiss_db[gf:gf + int((GHOST_ROOM + 0.4) * 100)] += 8.0      # сигнал слабеет — шипение громче
        # ещё 1-2 провала — в паузах подлиннее, не чаще раза в 15 с
        drops, last_drop = 0, ghost_at if ghost_at >= 0 else -10 ** 9
        for p0, p1 in pauses:
            room = (p1 - p0) * frame - int(0.2 * SR)
            at = p0 * frame + int(0.1 * SR)
            if (drops >= 2 or at < 0.25 * span_l or at + room > span_l - 2 * SR or room < int(0.3 * SR)
                    or abs(at - last_drop) < 15 * SR or near_sfx(at, room)):
                continue
            drop(at, min(int(0.45 * SR), room))
            drops, last_drop = drops + 1, at
        # обрыв сигнала после письма — потом тишина и ведущий
        c = start + lb + int(0.35 * SR)
        crash = _crash(rng, 0.3)
        mix[c:c + len(crash)] += crash * np.float32(_db(ref - 10))
        hiss_db[(start + lb) // FADE_FRAME + 30:(start + lb) // FADE_FRAME + 130] = -90.0

    # 6) шипение эфира по всему выпуску: еле-еле под ведущим, слышно под письмом, растёт в замираниях
    idx = np.arange(len(hiss_db))
    ok = ~np.isnan(hiss_db)
    if ok.any():
        hiss_db = np.interp(idx, idx[ok], hiss_db[ok])
        hiss_db = np.convolve(np.pad(hiss_db, (15, 15), mode="edge"), np.ones(31) / 31, mode="valid")  # без ступенек
        _hiss_into(mix, hiss_db, rng)
    del voice
    for i in range(0, n, BED_SEG):  # еле слышный «воздух» студии — чтобы не было цифровой тишины
        seg = mix[i:i + BED_SEG]
        seg += rng.standard_normal(len(seg), dtype=np.float32) * np.float32(_db(-66))
    mix = _band(mix, 30, 15000)
    mix = _peak1(mix)
    mix *= _db(-1)
    return mix


# ---------- «Сплетни Волгограда»: весёлый эфир (09.11) ----------
# Тот же ведущий «как на FM», но без помех и мрака: бодрый позывной в мажоре, весёлая подложка (до мажор, 104 удара в
# минуту), между частями выпуска (тема → версия 1 → 2 → 3 → ставка) — короткий «дзынь», как перебивка в шоу.
GOSSIP_BPM = 104
_GOSSIP_CHORDS = [  # до мажор: Cmaj7 – Am7 – Fmaj7 – G6
    (130.81, (261.63, 329.63, 392.0, 493.88)),
    (110.0, (220.0, 261.63, 329.63, 392.0)),
    (87.31, (174.61, 220.0, 261.63, 329.63)),
    (98.0, (196.0, 246.94, 293.66, 329.63)),
]
GOSSIP_BED_DB = -17.0   # подложка под ведущим (полоса телефона, в паузах; под словами ещё на 7 дБ тише)
STING_DB = -7.0         # «дзынь» между частями — от громкости ведущего (полоса телефона)


def _gossip_jingle(up: bool = True) -> np.ndarray:
    """Позывной сплетен: быстрое арпеджио колокольчиков в до мажоре (вверх — в начале, вниз — в конце)."""
    notes = [523.25, 659.25, 783.99, 1046.5]
    if not up:
        notes = notes[::-1]
    step = int(0.1 * SR)
    out = np.zeros(step * len(notes) + int(1.3 * SR), dtype=np.float32)
    for i, f in enumerate(notes):
        b = _bell(f, 1.3, 0.8 if i < len(notes) - 1 else 1.1)
        out[i * step:i * step + len(b)] += b
        if i == len(notes) - 1:  # последняя — с терцией сверху, «та-дам»
            out[i * step:i * step + len(b)] += _bell(f * 1.26, 1.3, 0.5)
    return out / (np.abs(out).max() or 1)


def _sting() -> np.ndarray:
    """Короткая перебивка между частями: два звонких «дзынь» вверх."""
    step = int(0.09 * SR)
    out = np.zeros(step + int(0.8 * SR), dtype=np.float32)
    out[:int(0.8 * SR)] += _bell(783.99, 0.8, 0.8)
    out[step:step + int(0.8 * SR)] += _bell(1046.5, 0.8, 1.0)
    return out / (np.abs(out).max() or 1)


GOSSIP_MUSIC_DB = -19.0  # музыка ElevenLabs под ведущими — она плотнее синтезатора, чуть тише (полоса телефона)
CALLER_DB = -1.0        # звонящий в студию — чуть тише ведущих (телефон и так режет полосу)
TURN_GAP = 0.28         # пауза между репликами ведущих — живой разговор, без «радио-тишины»


def gossip(voice_chunks: list[bytes], rate: int = 24000, seed: int = 0, roles: list[str] | None = None,
           parts: list[str] | None = None, music: np.ndarray | None = None) -> np.ndarray:
    """«Сплетни Волгограда» (float32, 48 кГц, пик ≈ -1 дБ): позывной, ведущий и соведущая «как на FM» поверх весёлой
    подложки (music — ElevenLabs, нет — синтезатор), звонящий в студию — телефоном (перед звонком звенит телефон,
    после — щелчок положенной трубки), между частями выпуска (parts) — «дзынь», в конце позывной вниз. Без помех.
    roles — host / cohost / call_f|call_m для каждого куска; parts — к какой части он относится."""
    rng = np.random.default_rng(seed)
    roles = list(roles or ["host"] * len(voice_chunks))
    parts = list(parts or [str(i) for i in range(len(voice_chunks))])
    items = []
    for pcm, role, part in zip(voice_chunks, roles, parts, strict=False):
        v = _resample(pcm, rate)
        if len(v):
            v = _band(v, 70, 11000)
            items.append(((_phone(v) if role.startswith("call") else _on_air(v)), role, part))
    if not items:
        return np.zeros(SR, dtype=np.float32)
    host_lv = [_speech_db(v) for v, r, _ in items if not r.startswith("call")]
    ref = host_lv[0] if host_lv else _speech_db(items[0][0])
    for v, r, _ in items:  # ведущие — одной громкости, звонящий чуть тише
        v *= np.float32(_db(ref + (CALLER_DB if r.startswith("call") else 0.0) - _speech_db(v)))
    sting, ring = _sting(), _ring()
    click = _line_drop()[-int(0.02 * SR):]
    layout, events, pos = [], [], 0   # events: (позиция в дорожке голоса, что, уровень)
    for i, (v, role, part) in enumerate(items):
        if i:
            prev_role, prev_part = items[i - 1][1], items[i - 1][2]
            if prev_role.startswith("call") and not role.startswith("call"):   # звонящий положил трубку
                events.append((pos + int(0.15 * SR), "click"))
                pos += int(0.35 * SR)
            if part != prev_part:
                if part == "call":                                               # звонок: телефон звенит в тишине
                    pos += int(0.4 * SR)
                    events.append((pos, "ring"))
                    pos += len(ring) + int(0.3 * SR)
                else:
                    pos += int(0.35 * SR)
                    events.append((pos, "sting"))
                    pos += int(0.55 * SR)
            else:
                pos += int((0.45 if role.startswith("call") or prev_role.startswith("call") else TURN_GAP) * SR)
        layout.append((pos, v))
        pos += len(v)
    if items[-1][1].startswith("call"):
        events.append((pos + int(0.15 * SR), "click"))
        pos += int(0.35 * SR)
    voice = np.zeros(pos, dtype=np.float32)
    for p0, v in layout:
        voice[p0:p0 + len(v)] += v
    del layout, items
    voice = _peak1(voice)
    ref_hp = _speech_db(_band(voice, *PHONE_BAND))      # громкость ведущих в полосе телефона

    def to_level(x: np.ndarray, rel: float) -> np.ndarray:
        r = float(np.sqrt(np.mean(np.square(_band(x, *PHONE_BAND), dtype=np.float64)))) or 1.0
        return x * np.float32(_db(ref_hp + rel) / r)

    head = to_level(_gossip_jingle(True), -3.0)
    tail = to_level(_gossip_jingle(False), -6.0)
    start = len(head) - int(0.5 * SR)                    # ведущий вступает, пока позывной ещё звенит
    end = start + len(voice) + int(0.5 * SR)
    n = end + len(tail) + int(0.3 * SR)
    mix = np.zeros(n, dtype=np.float32)
    mix[:len(head)] += head
    mix[start:start + len(voice)] += voice
    mix[end:end + len(tail)] += tail
    sounds = {"sting": to_level(sting, STING_DB), "ring": ring * np.float32(_db(ref_hp + RING_DB) / 0.35),
              "click": click * np.float32(_db(ref_hp + CALLER_DB) / 0.35 * 0.5)}
    for at, kind in events:
        snd, p = sounds[kind], start + at
        k = max(0, min(len(snd), n - p))
        mix[p:p + k] += snd[:k]
    a, b = max(0, start - int(0.6 * SR)), min(n, end + int(0.8 * SR))   # подложка — под всем голосом
    if music is not None and len(music) > 5 * SR:  # настоящая музыка (ElevenLabs) вместо синтезатора
        bed = to_level(_band(_loop(music, b - a), 40, 12000), GOSSIP_MUSIC_DB)
    else:
        bed = to_level(_talk_bed(b - a, rng, _GOSSIP_CHORDS, GOSSIP_BPM), GOSSIP_BED_DB)
    fi, fo = min(int(0.5 * SR), (b - a) // 3), min(int(1.5 * SR), (b - a) // 3)
    bed[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)
    bed[-fo:] *= np.linspace(1, 0, fo, dtype=np.float32)
    _duck(bed, _slice(voice, a - start, b - start), -7.0)
    mix[a:b] += bed
    del bed, voice
    mix = _band(mix, 30, 15000)
    mix = _peak1(mix)
    mix *= _db(-1)
    return mix
