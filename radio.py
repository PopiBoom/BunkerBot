"""Звук «как по радио» для ночного эфира (handlers/night.py): голос + эфир + позывной → голосовое OGG/Opus.

Голос (PCM 16 бит моно, куски — абзацы) склеивается с паузами, обрезается по полосе AM/FM-приёмника (≈ 280–3600 Гц),
чуть «греется» (мягкое насыщение) и плавает по громкости; под ним — шипение эфира, редкие потрескивания и фон
погоды (ветер или дождь, тоже сквозь «приёмник»). В начале — настройка на волну (шум и свист, уходящий вниз) и
тихий позывной из трёх нот, в конце — хвост эфира и щелчок выключения. Всё на numpy, кодирует PyAV (libopus).
"""
from __future__ import annotations

import io

import numpy as np

SR = 48000


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
    n = len(x)
    if n == 0:
        return x
    if n <= DIRECT_MAX:
        f = np.fft.rfftfreq(n, 1 / sr)
        return np.fft.irfft(np.fft.rfft(x) * _gain(f, lo, hi), n).astype(np.float32)
    k = FIR_TAPS
    half = k // 2
    h = np.fft.irfft(_gain(np.fft.rfftfreq(OLA_FFT, 1 / sr), lo, hi), OLA_FFT)  # нулевая фаза
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
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    if rate == SR or len(x) == 0:
        return x
    t_new = np.arange(int(len(x) * SR / rate)) * rate / SR
    return np.interp(t_new, np.arange(len(x)), x).astype(np.float32)


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
