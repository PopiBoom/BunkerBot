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


def _tension(ln: int, rng: np.random.Generator) -> np.ndarray:
    """Напряжение под письмом (владелец 08.11: «добавь помехи, напряжение в эфире»): низкий гул, который медленно
    ползёт вверх и нарастает; тонкий писк в ушах к концу; в последней трети — стук сердца, всё чаще (с обертонами
    110-220 Гц — иначе в динамике телефона его не слышно). float32."""
    out = np.zeros(ln, dtype=np.float32)
    span = max(ln / SR, 1.0)
    for p0 in range(0, ln, BED_SEG):  # кусками — без массивов на весь эфир
        n = min(BED_SEG, ln - p0)
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
    for p0 in range(0, ln, BED_SEG):
        n = min(BED_SEG, ln - p0)
        t = (np.arange(n, dtype=np.float64) + p0) / SR
        seg = np.zeros(n)
        for (f, amp), (lf, lph) in zip(voices, lfo, strict=True):
            swell = amp * (0.6 + 0.4 * np.sin(2 * np.pi * lf * t + lph))
            for det in (0.9985, 1.0015):
                for k in range(1, 6):
                    seg += swell * np.sin(2 * np.pi * f * det * k * t + k) / k
        seg *= 0.5 + 0.5 * np.clip(t / span, 0, 1) ** 1.2
        out[p0:p0 + n] = seg.astype(np.float32)
    return out / (float(np.abs(out).max()) or 1)


def _levels(x: np.ndarray, frame: int) -> np.ndarray:
    """Уровень (дБ) по кадрам длиной frame."""
    m = len(x) // frame
    if m == 0:
        return np.zeros(0)
    blk = x[:m * frame].reshape(m, frame)
    return 10 * np.log10(np.mean(np.square(blk, dtype=np.float64), axis=1) + 1e-12)


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


def _crackle(mix: np.ndarray, at: int, n: int, level: float, rng: np.random.Generator) -> None:
    """Короткий рваный треск эфира с щелчками."""
    n = min(n, len(mix) - at)
    if n < 64 or at < 0:
        return
    burst = _band(rng.standard_normal(n).astype(np.float32), 900, 6000)
    burst /= max(float(np.abs(burst).max()), 1e-6)
    gate = (rng.random(n // 240 + 1) > 0.35).repeat(240)[:n]  # эфир рвётся кусками
    mix[at:at + n] += burst * gate * np.hanning(n).astype(np.float32) * np.float32(level)
    for _ in range(rng.integers(2, 5)):
        c = at + int(rng.integers(0, n))
        k = min(90, len(mix) - c)
        mix[c:c + k] += (np.exp(-np.arange(k) / 14) * rng.choice([-1, 1]) * level).astype(np.float32)


LETTER_IN = 1.3     # пауза перед письмом: фон входит раньше голоса — настроение меняется до первого слова
LETTER_OUT = 1.9    # после письма: фон уже оборвался, тишина — потом ведущий
# Уровни — от громкости речи. Фон меряем в полосе, которую играет динамик телефона (PHONE_BAND): по всей полосе
# он выходил на 30-38 дБ тише голоса — в телефоне его просто не было слышно.
PHONE_BAND = (250, 8000)
PAD_DB = -15.0      # мрачный фон к концу письма, в паузах (под словами ещё на DUCK_DB тише)
RUMBLE_DB = -19.0   # низкий гул и сердце (по всей полосе — их слышно в наушниках)
DUCK_DB = -9.0
CRACKLE_DB = -14.0  # пик треска эфира — только в паузах речи, не чаще раза в CRACKLE_GAP с
CRACKLE_GAP = 8.0


def studio(voice_chunks: list[bytes], styles: list[str], rate: int = 24000, seed: int = 0) -> np.ndarray:
    """Страшная рубрика как настоящий эфир (float32, 48 кГц, пик ≈ -1 дБ). styles — "host" / "letter" для каждого
    куска голоса. Владелец 08.11: «прерывистая, неразборчивая… история прерывается посередине» — помехи глушили сам
    голос каждые 5-9 с, а письмо рвал сбой эфира посередине. Теперь слова не трогаем ничем: под письмом — мрачный
    фон, гул и сердце, которые приседают под словами; треск эфира — только в паузах; перед последней фразой письма
    фон обрывается, и она звучит в тишине; после письма — тишина, потом ведущий."""
    rng = np.random.default_rng(seed)
    pieces, spans, pos = [], [], 0          # spans: (начало, конец, стиль) кусков голоса внутри дорожки голоса
    for i, pcm in enumerate(voice_chunks):
        v = _resample(pcm, rate)
        if not len(v):
            continue
        st = styles[i] if i < len(styles) else "host"
        if pieces:
            prev = spans[-1][2]
            sec = (LETTER_IN if st == "letter" and prev != "letter" else
                   LETTER_OUT if prev == "letter" and st != "letter" else 0.7)
            pieces.append(np.zeros(int(sec * SR), dtype=np.float32))
            pos += len(pieces[-1])
        pieces.append(v)
        spans.append((pos, pos + len(v), st))
        pos += len(v)
    voice = np.concatenate(pieces).astype(np.float32) if pieces else np.zeros(SR, dtype=np.float32)
    del pieces
    voice = _band(voice, 70, 11000)                         # полный голос, без «приёмника»
    voice /= max(float(voice.max()), float(-voice.min())) or 1   # один уровень на весь голос: тихое — тихо
    voice = _compress(voice, -22, 2.5)
    voice /= max(float(voice.max()), float(-voice.min())) or 1

    head, tail = _jingle(True), _jingle(False)
    lead = int(0.25 * SR)
    start = len(head) - int(0.9 * SR) + lead                 # голос вступает, пока джингл ещё звенит
    n = start + len(voice) + int(0.6 * SR) + len(tail)
    mix = np.zeros(n, dtype=np.float32)
    mix[:len(head)] += head * _db(-6)
    mix[start:start + len(voice)] += voice
    end = start + len(voice) + int(0.6 * SR)
    mix[end:end + len(tail)] += tail * _db(-8)
    letters = [(a, b) for a, b, st in spans if st == "letter"]
    if letters:
        la, lb = letters[0][0], letters[-1][1]               # письмо внутри дорожки голоса
        frame = SR // 50
        lv = _levels(voice[la:lb], frame)
        loud = lv > -35
        ref = 10 * np.log10(np.mean(10 ** (lv[loud] / 10))) if loud.any() else -18.0   # громкость речи, дБ
        lvp = _levels(_band(voice[la:lb], *PHONE_BAND), frame)
        ref_phone = 10 * np.log10(np.mean(10 ** (lvp[loud] / 10))) if loud.any() else ref
        # последняя пауза перед финальной фразой (в последних 10 с): с неё фон гаснет, финал — в тишине
        runs = [r for r in _quiet_runs(lv, ref - 30, int(0.3 * SR / frame))
                if r[1] < len(lv) - int(0.8 * SR / frame) and r[0] * frame > (lb - la) - 10 * SR]
        hush = la + (runs[-1][0] * frame if runs else max(0, lb - la - 3 * SR))
        a = max(0, la - int(1.0 * SR))                      # фон входит в паузе перед письмом
        ln = hush + int(0.3 * SR) - a                        # гаснет за 0.3 с — внутри этой паузы
        pad = _pad(ln, rng)
        rumble = _band(_tension(ln, rng), 25, 12000)
        tail_n = max(1, ln // 4)                             # уровень — по самой громкой (последней) четверти
        for x, rel, to, band in ((pad, PAD_DB, ref_phone, PHONE_BAND), (rumble, RUMBLE_DB, ref, None)):
            part = _band(x[-tail_n:], *band) if band else x[-tail_n:]
            r = float(np.sqrt(np.mean(np.square(part, dtype=np.float64)))) or 1.0
            x *= np.float32(_db(to + rel) / r)
        bed = pad
        bed += rumble
        del pad, rumble
        fi, fo = min(int(2.0 * SR), ln // 3), min(int(0.3 * SR), ln // 3)
        bed[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)
        bed[-fo:] *= np.linspace(1, 0, fo, dtype=np.float32)
        _duck(bed, voice[a:a + ln], DUCK_DB)
        mix[start + a:start + a + ln] += bed
        del bed
        # треск эфира — только в паузах речи (слова не трогаем), не в первые 3 с и не после финальной паузы
        last = -1e9
        for i, j in _quiet_runs(lv, ref - 30, int(0.4 * SR / frame)):
            at = i * frame
            if at < 3 * SR or at - last < CRACKLE_GAP * SR or la + at >= hush - int(0.5 * SR):
                continue
            room = (j - i) * frame - int(0.16 * SR)
            k = int(min(rng.uniform(0.12, 0.26) * SR, room))
            if k < int(0.06 * SR):
                continue
            _crackle(mix, start + la + at + int(0.08 * SR) + int(rng.integers(0, room - k + 1)), k,
                     _db(ref + CRACKLE_DB) * rng.uniform(0.7, 1), rng)
            last = at
        _crackle(mix, start + lb + int(0.35 * SR), int(0.18 * SR), _db(ref + CRACKLE_DB - 3), rng)  # конец письма
    for i in range(0, n, BED_SEG):  # еле слышный «воздух» студии — чтобы не было цифровой тишины
        seg = mix[i:i + BED_SEG]
        seg += rng.standard_normal(len(seg), dtype=np.float32) * np.float32(_db(-66))
    mix = _band(mix, 30, 15000)
    mix /= max(float(mix.max()), float(-mix.min())) or 1
    mix *= _db(-1)
    return mix
