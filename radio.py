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


def _band(x: np.ndarray, lo: float, hi: float, sr: int = SR) -> np.ndarray:
    """Полосовой фильтр через БПФ с мягкими краями (без звона)."""
    n = len(x)
    if n == 0:
        return x
    f = np.fft.rfftfreq(n, 1 / sr)
    spec = np.fft.rfft(x)
    gain = np.ones_like(f)
    if lo > 0:
        gain *= 1 / (1 + (lo / np.maximum(f, 1e-3)) ** 4)
    if hi > 0:
        gain *= 1 / (1 + (f / hi) ** 4)
    return np.fft.irfft(spec * gain, n)


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


def broadcast(voice_chunks: list[bytes], rate: int = 24000, rain: bool = False, seed: int = 0) -> np.ndarray:
    """Весь эфир (float32, 48 кГц, моно, пик ≈ -1 дБ)."""
    rng = np.random.default_rng(seed)
    pause = np.zeros(int(0.85 * SR), dtype=np.float32)
    parts = []
    for i, pcm in enumerate(voice_chunks):
        v = _resample(pcm, rate)
        if len(v):
            v = v / (np.abs(v).max() or 1)
            parts += ([pause] if i else []) + [v]
    voice = np.concatenate(parts) if parts else np.zeros(SR, dtype=np.float32)
    # «приёмник»: полоса, насыщение, лёгкое плавание громкости
    voice = _band(voice, 280, 3600)
    voice = np.tanh(voice * 2.2) / np.tanh(2.2)
    t = np.arange(len(voice)) / SR
    voice *= 0.94 + 0.06 * np.sin(2 * np.pi * 0.35 * t + rng.uniform(0, 6))

    intro_tune = _tuning(1.8, rng)
    ident = _ident()
    lead = int(0.4 * SR)                     # тишина эфира между позывным и голосом
    tail = int(1.6 * SR)
    n = len(intro_tune) + len(ident) + lead + len(voice) + tail
    mix = np.zeros(n, dtype=np.float64)
    pos = 0
    mix[pos:pos + len(intro_tune)] += intro_tune
    pos += len(intro_tune) - int(0.5 * SR)   # позывной вступает, пока свист ещё тает
    mix[pos:pos + len(ident)] += _band(ident, 200, 4000) * 0.8
    pos += len(ident) + lead
    vstart = pos
    mix[pos:pos + len(voice)] += voice
    # под голосом — эфир и погода (тоже сквозь приёмник), от позывного до конца
    bed_n = n - (len(intro_tune) - int(0.5 * SR))
    bed = _db(-33) * _static(bed_n, rng)
    bed += _db(-25 if rain else -24) * _band(_rain(bed_n, rng) if rain else _wind(bed_n, rng), 200, 4200)
    bed = _fade(bed, 0.8, 0.3)
    mix[n - bed_n:] += bed
    # конец: всплеск эфира и щелчок выключения
    end = vstart + len(voice) + int(0.9 * SR)
    burst_n = n - end
    if burst_n > 0:
        burst = _band(rng.standard_normal(burst_n), 500, 6500)
        burst = burst / (np.abs(burst).max() or 1) * np.linspace(0.05, 0.45, burst_n)
        mix[end:] += burst
        mix[-200:] = 0
    mix = _band(mix, 120, 7500)
    mix /= np.abs(mix).max() or 1
    return (mix * _db(-1)).astype(np.float32)


def to_ogg(x: np.ndarray, bitrate: int = 48000) -> bytes:
    """float32 48 кГц моно → OGG/Opus (голосовое Telegram)."""
    import av

    buf = io.BytesIO()
    out = av.open(buf, "w", format="ogg")
    st = out.add_stream("libopus", rate=SR)
    st.layout = "mono"
    st.bit_rate = bitrate
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    step = 960 * 50  # кусками по секунде
    pts = 0
    for i in range(0, len(pcm), step):
        chunk = pcm[i:i + step]
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
