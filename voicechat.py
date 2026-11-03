"""Пиздюк в голосовом чате группы: слушает всех, иногда встревает голосом, отвечает, когда зовут.

Бот Telegram (Bot API) в голосовой чат зайти не может — заходит отдельный ПОЛЬЗОВАТЕЛЬСКИЙ аккаунт («юзербот»)
через Telethon + py-tgcalls (внутри ntgcalls/WebRTC). Настройка в .env:
  VC_API_ID, VC_API_HASH — с my.telegram.org (API development tools) этого аккаунта;
  VC_SESSION — строка входа: на сервере один раз `python tools/voice_login.py` (спросит телефон и код).
Аккаунт должен состоять в группе. Команды: /voice (зайти) и /voice_stop (выйти) — handlers/voice.py.

Слух: py-tgcalls отдаёт звук КАЖДОГО участника отдельно (ssrc → user_id по списку участников звонка). Речь режем
по паузам (громкость кадра 10 мс), кусок → WAV → ai.transcribe (Gemini через OpenRouter) → в память чата
строкой «[войс: …]» от имени говорившего. Отвечает: позвали по имени («Пиздюк, …») — всегда, иначе изредка
(REPLY_CHANCE, не чаще REPLY_GAP). Ответ — Claude (ai.voice_reply, характер Пиздюка), озвучка — ai.speak
(gpt-audio-mini через OpenRouter, PCM 24 кГц) → кадрами по 10 мс в звонок. Озвучить не вышло — текстом в чат.
Звук только в памяти процесса — записи не сохраняются, в базу идёт только текст (как у голосовых).

Тяжёлые библиотеки импортируются лениво: без них (или без настроек) бот работает как раньше, /voice скажет,
чего не хватает.
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import random
import time
import wave
from array import array
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger("voicechat")

RATE = 24000                 # Гц, моно, 16 бит — и слушаем, и говорим (озвучка тоже 24 кГц: без пересчёта)
FRAME = RATE * 2 // 100      # байт в 10 мс
SPEECH_LEVEL = 450           # средняя громкость кадра (0..32767), выше — человек говорит
END_SILENCE = 0.8            # сек тишины — фраза кончилась
MIN_SPEECH = 0.6             # сек речи в куске — короче не слушаем (кашель, «а»)
MAX_UTTER = 20.0             # сек — длинный монолог режем на куски
REPLY_CHANCE = 0.2           # сам встревает: 1 из 5 фраз (если не звали)…
REPLY_GAP = 40.0             # …и не чаще раза в 40 сек
MAX_SESSION = 3 * 3600       # дольше 3 ч в войсе не сидит (и не держит обновления сервера)
ALONE_TIMEOUT = 3 * 60       # остался один — через 3 мин выходит
LEAD = 0.04                  # сек: на сколько озвучка опережает реальное время (запас от рывков)


def configured() -> str:
    """"" — всё есть; иначе — чего не хватает (для ответа на /voice)."""
    missing = [k for k in ("VC_API_ID", "VC_API_HASH", "VC_SESSION") if not os.getenv(k, "").strip()]
    if missing:
        return "в .env нет " + ", ".join(missing) + " (как настроить — в начале voicechat.py)"
    import importlib.util

    if not (importlib.util.find_spec("pytgcalls") and importlib.util.find_spec("telethon")):
        return "не установлены пакеты py-tgcalls и telethon (pip install -r requirements.txt)"
    return ""


@dataclass
class _Utter:
    pcm: bytearray
    started: float
    last_voice: float
    voiced: float = 0.0  # сек с речью


@dataclass
class Session:
    chat_id: int
    bot: object                     # aiogram Bot — писать в чат
    db: object                      # Database
    started: float = field(default_factory=time.time)
    lines: deque = field(default_factory=lambda: deque(maxlen=40))   # [(имя, текст)] — разговор
    buf: dict = field(default_factory=dict)                          # ssrc -> _Utter
    who: dict = field(default_factory=dict)                          # ssrc -> (user_id, имя)
    who_at: float = 0.0
    alone_since: float = 0.0
    last_spoke: float = 0.0
    speak_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    hear_sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(3))
    tasks: list = field(default_factory=list)


_client = None          # Telethon
_calls = None           # PyTgCalls
_me: int | None = None
_names: dict[int, str] = {}
sessions: dict[int, Session] = {}
_start_lock = asyncio.Lock()


def live() -> bool:
    """Сидит ли бот хоть в одном войсе (тогда deploy/update.sh не перезапускает его — см. games.flag_loop)."""
    now = time.time()
    return any(now - s.started < MAX_SESSION for s in sessions.values())


def _level(pcm: bytes) -> float:
    """Средняя громкость кадра (по каждому 4-му отсчёту — хватает и быстро)."""
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if not a:
        return 0.0
    part = a[::4]
    return sum(abs(x) for x in part) / len(part)


def _wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


async def _ensure_client():
    """Поднять юзербота и py-tgcalls один раз на процесс."""
    global _client, _calls, _me
    async with _start_lock:
        if _calls is not None:
            return _calls
        from pytgcalls import PyTgCalls, filters
        from pytgcalls.types import ChatUpdate, Device, Direction
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(os.getenv("VC_SESSION", "").strip()),
                                int(os.getenv("VC_API_ID", "0")), os.getenv("VC_API_HASH", "").strip())
        await client.connect()
        if not await client.is_user_authorized():  # иначе py-tgcalls полез бы спрашивать телефон в консоли
            await client.disconnect()
            raise RuntimeError("строка VC_SESSION не подходит — заново `python tools/voice_login.py`")
        _me = (await client.get_me()).id
        try:
            await client.get_dialogs()  # чтобы Telethon знал группы аккаунта по их id
        except Exception as e:
            logger.info("get_dialogs: %s", e)
        calls = PyTgCalls(client)

        async def on_frames(_c, update) -> None:
            s = sessions.get(update.chat_id)
            if s is not None:
                _feed(s, update.frames)

        async def on_chat(_c, update) -> None:  # выгнали, закрыли войс, вышел сам
            s = sessions.get(update.chat_id)
            if s is not None:
                await _close(s, "🎙 Войс закрыли — я вышел.")

        calls.add_handler(on_frames, filters.stream_frame(Direction.INCOMING, Device.MICROPHONE | Device.SPEAKER))
        calls.add_handler(on_chat, filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        await calls.start()
        _client, _calls = client, calls
        return calls


async def join(bot, db, chat_id: int) -> str:
    """Зайти в войс группы. "" — зашёл; иначе — почему нет (текст для чата)."""
    if chat_id in sessions:
        return "Я уже в войсе."
    why = configured()
    if why:
        return f"Голосовой режим не настроен: {why}."
    try:
        calls = await _ensure_client()
        from pytgcalls.types import AudioQuality, ExternalMedia, GroupCallConfig, MediaStream, RecordStream

        s = Session(chat_id=chat_id, bot=bot, db=db)
        sessions[chat_id] = s
        # Говорим своими кадрами (без ffmpeg), слушаем — кадры каждого участника отдельно
        await calls.play(chat_id, MediaStream(ExternalMedia.AUDIO, AudioQuality.LOW,
                                              video_flags=MediaStream.Flags.IGNORE),
                         config=GroupCallConfig(auto_start=True))
        await calls.record(chat_id, RecordStream(audio=True, audio_parameters=AudioQuality.LOW))
    except Exception as e:
        sessions.pop(chat_id, None)
        logger.warning("voice join failed in %s: %s", chat_id, e)
        return ("Не смог зайти в войс: " + str(e)[:200] + ". Аккаунт бота для войса должен быть в группе, а войс — "
                "запущен (или у аккаунта должно быть право его начать).")
    s.tasks = [asyncio.create_task(_watch(s))]
    logger.info("voice: зашёл в %s", chat_id)
    return ""


async def leave(chat_id: int, note: str = "") -> bool:
    s = sessions.get(chat_id)
    if s is None:
        return False
    await _close(s, note)
    return True


async def _close(s: Session, note: str = "") -> None:
    if sessions.get(s.chat_id) is not s:
        return
    sessions.pop(s.chat_id, None)
    for t in s.tasks:
        t.cancel()
    try:
        if _calls is not None:
            await _calls.leave_call(s.chat_id)
    except Exception as e:
        logger.info("leave_call %s: %s", s.chat_id, e)
    if note:
        try:
            await s.bot.send_message(s.chat_id, note)
        except Exception as e:
            logger.info("voice note failed: %s", e)
    logger.info("voice: вышел из %s", s.chat_id)


async def shutdown() -> None:
    """Бот останавливается — выйти из всех войсов и отключить юзербота."""
    for s in list(sessions.values()):
        await _close(s)
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            pass


# ---------- слух ----------


def _feed(s: Session, frames) -> None:
    """Кадры по 10 мс от каждого участника: копим речь, пока человек говорит."""
    now = time.monotonic()
    for f in frames:
        pcm = f.frame
        if not pcm:
            continue
        ssrc = f.ssrc & 0xFFFFFFFF
        voiced = _level(pcm) >= SPEECH_LEVEL
        u = s.buf.get(ssrc)
        if u is None:
            if not voiced:
                continue
            u = s.buf[ssrc] = _Utter(bytearray(), now, now)
        u.pcm += pcm
        if voiced:
            u.voiced += len(pcm) / (RATE * 2)
            u.last_voice = now
        if len(u.pcm) >= MAX_UTTER * RATE * 2:
            _finish(s, ssrc)


def _finish(s: Session, ssrc: int) -> None:
    u = s.buf.pop(ssrc, None)
    if u is None or u.voiced < MIN_SPEECH:
        return
    s.tasks = [t for t in s.tasks if not t.done()]
    s.tasks.append(asyncio.create_task(_hear(s, ssrc, bytes(u.pcm))))


async def _watch(s: Session) -> None:
    """Раз в 0.25 сек: у кого кончилась фраза. Раз в 30 сек: не остался ли один, не пора ли выходить."""
    tick = 0
    while sessions.get(s.chat_id) is s:
        await asyncio.sleep(0.25)
        now = time.monotonic()
        for ssrc, u in list(s.buf.items()):
            if now - u.last_voice > END_SILENCE:
                _finish(s, ssrc)
        tick += 1
        if tick % 120:
            continue
        if time.time() - s.started > MAX_SESSION:
            await _close(s, "🎙 Три часа в войсе — хватит с вас, я пошёл. Позвать снова — /voice.")
            return
        try:
            people = [p for p in await _calls.get_participants(s.chat_id) if p.user_id != _me]
        except Exception as e:
            logger.info("participants %s: %s", s.chat_id, e)
            continue
        if people:
            s.alone_since = 0.0
        elif not s.alone_since:
            s.alone_since = time.time()
        elif time.time() - s.alone_since > ALONE_TIMEOUT:
            await _close(s, "🎙 В войсе никого — вышел.")
            return


async def _name(user_id: int) -> str:
    if user_id not in _names:
        try:
            u = await _client.get_entity(user_id)
            _names[user_id] = getattr(u, "first_name", None) or getattr(u, "username", None) or "Кто-то"
        except Exception:
            _names[user_id] = "Кто-то"
    return _names[user_id]


async def _speaker(s: Session, ssrc: int) -> tuple[int, str]:
    """Кто говорил: ssrc → участник звонка. Неизвестный ssrc — обновляем список (не чаще раза в 5 сек)."""
    if ssrc not in s.who and time.monotonic() - s.who_at > 5:
        s.who_at = time.monotonic()
        try:
            for p in await _calls.get_participants(s.chat_id):
                if p.user_id != _me:
                    s.who[(p.source or 0) & 0xFFFFFFFF] = (p.user_id, await _name(p.user_id))
        except Exception as e:
            logger.info("participants %s: %s", s.chat_id, e)
    return s.who.get(ssrc, (0, "Кто-то"))


async def _hear(s: Session, ssrc: int, pcm: bytes) -> None:
    import ai
    from games import active
    from handlers import roast as R

    if active(s.chat_id):  # идёт Бункер или Мафия — не слушаем и не встреваем
        return
    async with s.hear_sem:
        text = await ai.transcribe(_wav(pcm))
    if not text or sessions.get(s.chat_id) is not s:
        return
    uid, name = await _speaker(s, ssrc)
    if uid and await s.db.is_optout(s.chat_id, uid):  # /forget_me — не запоминаем и не трогаем
        return
    logger.info("войс %s, %s: %s", s.chat_id, name, text[:120])
    s.lines.append((name, text))
    try:
        await s.db.add_chat_message(s.chat_id, 0, uid, name, None, f"[войс: {text}]"[:1000])
    except Exception as e:
        logger.debug("voice memory failed: %s", e)
    addressed = bool(R._BOT_NAME.search(text))
    if not addressed:
        if (random.random() >= REPLY_CHANCE or time.time() - s.last_spoke < REPLY_GAP
                or len(text.split()) < 4 or s.speak_lock.locked()):
            return
    await _reply(s, uid, name, text, addressed)


# ---------- голос ----------


async def _reply(s: Session, uid: int, name: str, said: str, addressed: bool) -> None:
    import ai
    from handlers import roast as R

    async with s.speak_lock:
        if sessions.get(s.chat_id) is not s:
            return
        is_father = bool(uid) and uid == R._father_id(R._cfg_ref[0] if R._cfg_ref else None)
        if is_father:
            ai.father_rude.set(random.random() < ai.FATHER_RUDE_CHANCE)
        try:
            dossier = await s.db.get_dossier(s.chat_id, uid) if uid else ""
            text = await ai.voice_reply(list(s.lines), name, said, addressed, dossier, is_father)
            text = await ai.polish(text, said, max_words=30, author=ai.FATHER_NAME if is_father else name)
        except Exception as e:
            logger.warning("voice reply failed: %s", e)
            return
        if not text:
            return
        if is_father:
            text = ai.vary_father(text)
        text = ai.scrub(text)
        pcm = await ai.speak(text)
        s.last_spoke = time.time()
        s.lines.append((R.BOT_SELF, text))
        try:
            await s.db.add_chat_message(s.chat_id, 0, 0, R.BOT_SELF, None, f"[войс: {text}]"[:1000])
        except Exception as e:
            logger.debug("voice memory failed: %s", e)
        if pcm:
            await _play(s, pcm)
        else:  # озвучка не вышла — хотя бы текстом, чтобы ответ не пропал
            try:
                await s.bot.send_message(s.chat_id, f"🎙 {html.escape(text)}")
            except Exception as e:
                logger.info("voice text fallback failed: %s", e)


async def _play(s: Session, pcm: bytes) -> None:
    """Озвучку — в звонок кадрами по 10 мс в реальном времени (с небольшим запасом вперёд)."""
    from pytgcalls.types import Device

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    for n, i in enumerate(range(0, len(pcm), FRAME)):
        if sessions.get(s.chat_id) is not s:
            return
        chunk = pcm[i:i + FRAME]
        if len(chunk) < FRAME:
            chunk += b"\0" * (FRAME - len(chunk))
        try:
            await _calls.send_frame(s.chat_id, Device.MICROPHONE, chunk)
        except Exception as e:
            logger.warning("send_frame failed: %s", e)
            return
        delay = t0 + (n + 1) * 0.01 - LEAD - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
