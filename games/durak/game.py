"""Дурак (подкидной) в личке у бота — HTML-виджет, как шахматы (09.11, владелец: «а дурака возможно будет создать?»
→ подкидной, 2-6 игроков, без игры против Пиздюка).

/durak (админ; личка или группа): вызвавшему в личку — стол-виджет (`widget.html`), в канал бати — картинка со
ссылкой `t.me/<бот>?start=dur_<id>` («Сесть за стол»; в личку не шлём — просьба владельца, как у шахмат). Кто открыл ссылку — за
столом (до 6), ему в личку свой стол. Хозяин стола жмёт «▶️ Начать» (от 2 игроков; сел шестой — начинается само).

Правила (классика): колода 36, по 6 карт, козырь — нижняя карта колоды (её видно под колодой). Первым заходит тот,
у кого младший козырь. Подкидывать может любой, кроме отбивающегося, — карты того же достоинства, что на столе; за
отбой не больше 6 карт и не больше, чем было у отбивающегося в начале. Отбился — «Бито», и он заходит следующим;
не смог — «Беру» (ему ещё могут подкинуть вдогонку), заходит следующий за ним. Добор до 6: сначала заходивший,
потом остальные по кругу, отбивавшийся последним. Колода кончилась, карты кончились — вышел (не дурак). Последний с
картами — дурак; вышли последние двое разом — ничья.
«Бито»/«Пас» не ждём от тех, кому нечего подкинуть, — отбой закрывается сам. Зависшие: заходящий молчит `TURN` —
ходит младшей картой сам; отбивающийся молчит — бьёт младшими, не может — «Беру» (только «Беру» — брошенная
партия ходила бы по кругу без конца); подкидывающие молчат (`TURN` у заходившего, `GRACE` у
остальных после его «Бито» или после «Беру») — пас. Таймеры — `ticker` (из bot.py) по партиям из `durak_live`.

Состояние стола каждого — `chat_data` его сообщения (`_view`: рука, стол, козырь, колода, соперники, роль, статус);
бот правит столы всех после каждого действия (`EditMessageWidgetData`, молча) и пишет с уведомлением тому, чей ход
(«🃏 Твой ход - заходи», «🛡 Вася заходит 7♥ - отбивайся»; прошлое такое сообщение удаляется). Запросы виджета:
`ua:<id>:<карта>` — зайти/подкинуть, `ud:<id>:<карта><пара>` — покрыть, `ub` — бито/пас, `ut` — беру, `us` —
начать, `un` — ещё раз (хозяин, те же игроки, те же сообщения). Карта — 2 знака: достоинство `6789TJQKA` + масть
`shdc`. Партия — настройка `durak:<id>`. Без ИИ, бесплатно.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import secrets
import time
from pathlib import Path

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import tgbeta
from config import Config
from database import Database
from games.chess.widget import _name as player_name
from games.chess.widget import _note, _send_card, _unnote
from tgbeta import AnswerWidgetQuery, EditMessageWidgetData, SendWidget

logger = logging.getLogger("durak")
router = Router(name="durak")

KEY = "durak:"          # настройка: партия
OPEN = "durak_open"     # стол, который собирает игроков
LIVE = "durak_live"     # столы, за которыми следит ticker
START = "dur_"          # /start dur_<id>
HTML = Path(__file__).with_name("widget.html").read_text("utf-8")
CARD = Path(__file__).resolve().parents[2] / "assets" / "lobby" / "durak.jpg"
CARD_KEY = "durak_card"
CAPTION = "🃏 Дурак у Пиздюка · не видно стол - обнови Telegram"
RANKS, SUITS = "6789TJQKA", "shdc"
SIGN = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}
MAX_PLAYERS, HAND = 6, 6
TURN = 90               # сек на ход (зайти, отбиться, решить «бито»)
GRACE = 15              # сек остальным подкинуть после «Бито» заходившего или после «Беру»
LOBBY_TTL = 3 * 3600    # стол не начался — закрывается
GAME_TTL = 4 * 3600     # партия дольше — закрывается без дурака
TICK = 2
NAME_LEN = 14
MAX_DATA = 1024

_locks: dict[str, asyncio.Lock] = {}
_bg: set = set()
_live: set[str] = set()


def _lock(gid: str) -> asyncio.Lock:
    if len(_locks) > 300:
        _locks.clear()
    return _locks.setdefault(gid, asyncio.Lock())


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg.add(task)
    task.add_done_callback(_bg.discard)


async def _load(db: Database, gid: str) -> dict | None:
    raw = await db.get_setting(KEY + gid, "")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


async def _save(db: Database, gid: str, st: dict) -> None:
    st["ts"] = time.time()
    await db.set_setting(KEY + gid, json.dumps(st, ensure_ascii=False))


async def _set_live(db: Database, gid: str, on: bool) -> None:
    (_live.add if on else _live.discard)(gid)
    await db.set_setting(LIVE, json.dumps(sorted(_live)))


# ---------- карты и правила (без Telegram) ----------


def _rank(c: str) -> int:
    return RANKS.index(c[0])


def beats(c: str, a: str, trump: str) -> bool:
    """Бьёт ли карта c карту a при козырной масти trump."""
    return (c[1] == a[1] and _rank(c) > _rank(a)) or (c[1] == trump and a[1] != trump)


def show(c: str) -> str:
    return ("10" if c[0] == "T" else c[0]) + SIGN[c[1]] + "︎"


def _ids(st: dict) -> list[int]:
    return [p[0] for p in st["players"]]


def _nm(st: dict, uid: int) -> str:
    return next((p[1] for p in st["players"] if p[0] == uid), "?")


def _hand(st: dict, uid: int) -> list[str]:
    return st["hands"].setdefault(str(uid), [])


def _active(st: dict) -> list[int]:
    return [u for u in _ids(st) if u not in st["out"]]


def _next(st: dict, uid: int) -> int:
    """Следующий по кругу, кто ещё в игре."""
    ids = _ids(st)
    i = ids.index(uid)
    for k in range(1, len(ids)):
        u = ids[(i + k) % len(ids)]
        if u not in st["out"]:
            return u
    return uid


def _unbeaten(st: dict) -> int:
    return sum(1 for _a, d in st["table"] if d is None)


def _ranks(st: dict) -> set[str]:
    return {c[0] for pair in st["table"] for c in pair if c}


def _room(st: dict) -> int:
    """Сколько карт ещё можно подкинуть в этот отбой."""
    return max(0, st["limit"] - len(st["table"])) if st["table"] else 0


def _throwers(st: dict) -> list[int]:
    return [u for u in _active(st) if u != st["dfd"] and _hand(st, u)]


def _can_throw(st: dict, uid: int) -> bool:
    ranks = _ranks(st)
    return _room(st) > 0 and any(c[0] in ranks for c in _hand(st, uid))


def _sorted(hand: list[str], trump: str) -> list[str]:
    return sorted(hand, key=lambda c: (c[1] == trump, SUITS.index(c[1]), _rank(c)))


def deal(st: dict, rng: random.Random) -> None:
    """Раздать: по 6, козырь — нижняя карта колоды, первым заходит младший козырь."""
    deck = [r + s for s in SUITS for r in RANKS]
    rng.shuffle(deck)
    ids = _ids(st)
    st["trump"] = deck[0]  # нижняя карта; вшестером колода раздаётся вся — козырь уходит последнему на руки
    st["hands"] = {str(u): [deck.pop() for _ in range(HAND)] for u in ids}
    st["deck"] = deck
    trump = st["trump"][1]
    lows = [(_rank(c), u) for u in ids for c in st["hands"][str(u)] if c[1] == trump]
    st.update(phase="play", out=[], table=[], passed=[], taking=False, limit=0, discard=0, durak=None,
              start=time.time(), bout=0)
    st["att"] = min(lows)[1] if lows else rng.choice(ids)
    st["dfd"] = _next(st, st["att"])


def attack(st: dict, uid: int, card: str, notes: dict) -> str | None:
    if uid not in _active(st):
        return "Ты уже вышел из игры"
    if uid == st["dfd"]:
        return "Ты отбиваешься"
    hand = _hand(st, uid)
    if card not in hand:
        return "Нет такой карты"
    first = not st["table"]
    if first:
        if uid != st["att"]:
            return f"Заходит {_nm(st, st['att'])}"
        st["limit"] = min(HAND, len(_hand(st, st["dfd"])))
    else:
        if card[0] not in _ranks(st):
            return "Подкинуть можно только того же достоинства, что на столе"
        if _room(st) <= 0:
            return "Больше подкидывать нельзя"
    was_beaten = not _unbeaten(st)
    hand.remove(card)
    st["table"].append([card, None])
    if uid in st["passed"]:
        st["passed"].remove(uid)
    if not st["taking"] and (first or was_beaten):
        verb = "заходит" if first else "подкидывает"
        notes[st["dfd"]] = f"🛡 {html.escape(_nm(st, uid))} {verb} {show(card)} - отбивайся"
    return None


def defend(st: dict, uid: int, card: str, i: int, notes: dict) -> str | None:
    if uid != st["dfd"]:
        return f"Отбивается {_nm(st, st['dfd'])}"
    if st["taking"]:
        return "Ты уже берёшь"
    if not 0 <= i < len(st["table"]) or st["table"][i][1] is not None:
        return "Эта карта уже покрыта"
    hand = _hand(st, uid)
    if card not in hand:
        return "Нет такой карты"
    if not beats(card, st["table"][i][0], st["trump"][1]):
        return "Этой картой не побить"
    hand.remove(card)
    st["table"][i][1] = card
    st["passed"] = []  # новые достоинства на столе — подкидывать снова можно всем
    return None


def take(st: dict, uid: int) -> str | None:
    if uid != st["dfd"]:
        return "Берёт только отбивающийся"
    if st["taking"] or not _unbeaten(st):
        return "Нечего брать"
    st["taking"] = True
    st["passed"] = []
    return None


def pass_(st: dict, uid: int) -> str | None:
    if uid == st["dfd"] or uid not in _active(st):
        return "Сейчас не тебе"
    if not st["table"]:
        return "Стол пустой"
    if _unbeaten(st) and not st["taking"]:
        return f"Ждём, пока {_nm(st, st['dfd'])} отобьётся"
    if uid not in st["passed"]:
        st["passed"].append(uid)
    return None


def _end_bout(st: dict, notes: dict) -> None:
    """Отбой кончился: бито или взял, добор, кто вышел, кто дурак, кто заходит дальше."""
    att, dfd, took = st["att"], st["dfd"], st["taking"]
    cards = [c for pair in st["table"] for c in pair if c]
    if took:
        _hand(st, dfd).extend(cards)
    else:
        st["discard"] += len(cards)
    st.update(table=[], passed=[], taking=False, limit=0)
    ids = _ids(st)
    i = ids.index(att)
    others = [ids[(i + k) % len(ids)] for k in range(1, len(ids))]
    for u in [att] + [u for u in others if u not in (dfd,) and u not in st["out"]] + [dfd]:
        hand = _hand(st, u)
        while len(hand) < HAND and st["deck"]:
            hand.append(st["deck"].pop())
    if not st["deck"]:
        for u in _active(st):
            if not _hand(st, u):
                st["out"].append(u)
    active = _active(st)
    if len(active) <= 1:
        st["phase"] = "end"
        st["durak"] = active[0] if active else None
        st.pop("due", None)
        return
    nxt = _next(st, dfd) if took or dfd in st["out"] else dfd
    st["att"], st["dfd"] = nxt, _next(st, nxt)
    st["bout"] += 1
    notes[nxt] = "🃏 Твой ход - заходи"


def settle(st: dict, now: float, notes: dict) -> None:
    """После любого действия: закрыть отбой, если ждать некого, и поставить таймер на того, кого ждём."""
    while st.get("phase") == "play":
        if not st["table"] or (_unbeaten(st) and not st["taking"]):
            st["due"] = now + TURN
            return
        waiting = [u for u in _throwers(st) if u not in st["passed"] and _can_throw(st, u)]
        if waiting:
            st["due"] = now + (TURN if st["att"] in waiting and not st["taking"] else GRACE)
            return
        _end_bout(st, notes)


def _auto_defend(st: dict) -> None:
    """Отбивающийся завис: побить всё младшими картами (некозырными, если можно), не выходит — «Беру».
    Если всегда только брать, брошенная партия ходит по кругу и не кончается никогда."""
    dfd, trump = st["dfd"], st["trump"][1]
    hand = sorted(_hand(st, dfd), key=lambda c: (c[1] == trump, _rank(c)))
    plan, used = [], set()
    for i, (a, d) in enumerate(st["table"]):
        if d is None:
            c = next((c for c in hand if c not in used and beats(c, a, trump)), None)
            if c is None:
                take(st, dfd)
                return
            plan.append((c, i))
            used.add(c)
    for c, i in plan:
        defend(st, dfd, c, i, {})


def timeout(st: dict, now: float, notes: dict) -> None:
    """Кто-то завис: заходящий — младшей картой, отбивающийся — «Беру», подкидывающие — пас."""
    if not st["table"]:
        hand = sorted(_hand(st, st["att"]), key=lambda c: (c[1] == st["trump"][1], _rank(c)))
        if hand:
            attack(st, st["att"], hand[0], notes)
    elif _unbeaten(st) and not st["taking"]:
        _auto_defend(st)
    else:
        for u in _throwers(st):
            if u not in st["passed"]:
                st["passed"].append(u)
    settle(st, now, notes)


# ---------- вид стола игрока ----------


def _status(st: dict, uid: int) -> str:
    ph = st["phase"]
    nm = lambda u: _nm(st, u)[:NAME_LEN]  # noqa: E731
    if ph == "lobby":
        if uid == st["host"]:
            return "Жми «Начать», когда все сядут" if len(st["players"]) >= 2 else "Ждём игроков - ссылка в канале"
        return f"Ждём, когда {nm(st['host'])} начнёт"
    if ph == "end":
        if st.get("cancel"):
            return st["cancel"]
        if st.get("durak") is None:
            return "Ничья - дурака нет"
        return "Ты дурак" if st["durak"] == uid else f"Дурак - {nm(st['durak'])}"
    if uid in st["out"]:
        return "Ты вышел - не дурак! Досматривай"
    att, dfd = st["att"], st["dfd"]
    if not st["table"]:
        if uid == att:
            return "Твой ход - заходи"
        return f"{nm(att)} заходит под тебя" if uid == dfd else f"{nm(att)} заходит под {nm(dfd)}"
    unb = _unbeaten(st)
    if uid == dfd:
        if st["taking"]:
            return "Берёшь. Сейчас подкинут ещё"
        return "Отбивайся или «Беру»" if unb else "Отбился! Ждём «Бито»"
    can = _can_throw(st, uid) and uid not in st["passed"]
    if st["taking"]:
        return f"{nm(dfd)} берёт" + (" - подкинь вдогонку или «Пас»" if can else "")
    if unb:
        return f"{nm(dfd)} отбивается" + (" - можно подкинуть" if _can_throw(st, uid) else "")
    return "Отбился. Подкинь или «Бито»" if can else "Ждём остальных"


def _flags(st: dict, u: int) -> str:
    if st["phase"] == "lobby":
        return "h" if u == st["host"] else ""
    f = "x" if u in st["out"] else ""
    if st["phase"] == "play":
        f += "a" if u == st["att"] else "d" if u == st["dfd"] else ""
        f += "p" if u in st["passed"] else ""
    return f


def view(st: dict, uid: int) -> str:
    """chat_data стола игрока uid — JSON, как его читает widget.html."""
    ph = st["phase"]
    v: dict = {"g": st["id"], "p": {"lobby": "l", "play": "g"}.get(ph, "e")}
    if uid == st["host"]:
        v["H"] = 1
    ids = _ids(st)
    if ph == "lobby":
        v["o"] = [[n[:NAME_LEN], 0, _flags(st, u)] for u, n in st["players"]]
    else:
        i = ids.index(uid) if uid in ids else -1
        others = [ids[(i + k) % len(ids)] for k in range(1, len(ids))] if i >= 0 else ids
        v["o"] = [[_nm(st, u)[:NAME_LEN], len(_hand(st, u)), _flags(st, u)] for u in others]
        v["h"] = "".join(_sorted(_hand(st, uid), st["trump"][1])) if st.get("hands") else ""
        v["t"] = "".join(a + (d or "--") for a, d in st.get("table", []))
        if st.get("trump"):
            v["k"] = st["trump"]
        v["d"] = len(st.get("deck", []))
    if ph == "play":
        v["r"] = ("x" if uid in st["out"] else "d" if uid == st["dfd"] else "a" if uid == st["att"] else "w")
        if v["r"] in "aw":
            v["m"] = _room(st)
            if uid in st["passed"]:
                v["ps"] = 1
        if st["taking"]:
            v["tk"] = 1
    if ph == "end":
        v["x"] = "d" if st.get("durak") is None else "l" if st["durak"] == uid else "w"
        if st.get("next") or st.get("cancel"):
            v.pop("H", None)  # «Ещё раз» уже нажали / стол закрыт
    v["s"] = _status(st, uid)
    data = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    if len(data.encode()) > MAX_DATA:  # почти не бывает: 6 длинных имён и 30 карт на руке
        for o in v.get("o", []):
            o[0] = o[0][:5]
        data = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return data


# ---------- Telegram ----------


async def _send_table(bot: Bot, st: dict, uid: int) -> None:
    sent = await bot(SendWidget(chat_id=uid, html=HTML, allow_queries=True, chat_data=view(st, uid), caption=CAPTION))
    st.setdefault("msgs", {})[str(uid)] = int(sent["message_id"])


async def _push_one(bot: Bot, st: dict, uid: int) -> None:
    mid = (st.get("msgs") or {}).get(str(uid))
    if not mid:
        return
    try:
        await bot(EditMessageWidgetData(chat_id=uid, message_id=mid, chat_data=view(st, uid)))
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            logger.info("дурак %s: стол %s не обновил: %s", st.get("id"), uid, e)
    except Exception as e:  # noqa: BLE001 — сеть: следующее действие поправит
        logger.info("дурак %s: стол %s не обновил: %s", st.get("id"), uid, e)


async def _push(bot: Bot, st: dict, skip: int = 0) -> None:
    await asyncio.gather(*(_push_one(bot, st, u) for u in _ids(st) if u != skip))


async def _send_notes(bot: Bot, st: dict, notes: dict, unnote: list[int] = ()) -> None:
    for u in unnote:
        if u not in notes:
            await _unnote(bot, st, u)
    for u, text in notes.items():
        await _note(bot, st, u, text)


def _deal_notes(st: dict) -> dict:
    att = st["att"]
    return {u: f"🃏 Раздал! Козырь - {show(st['trump'])}. "
               + ("Ты заходишь." if u == att else f"Заходит {html.escape(_nm(st, att))}.") for u in _ids(st)}


async def create(bot: Bot, db: Database, uid: int, name: str = "") -> None:
    """Новый стол: стол-виджет хозяину, картинка со ссылкой — сразу в канал."""
    from handlers.channel_posts import channel_id, mark_own

    old = await db.get_setting(OPEN, "")
    if old:  # прошлый стол так и не начался — закрыть
        async with _lock(old):
            st = await _load(db, old)
            if st and st["phase"] == "lobby":
                st.update(phase="end", cancel="Стол закрыт - собрали новый")
                await _push(bot, st)
                await _save(db, old, st)
                await _set_live(db, old, False)
    gid = secrets.token_urlsafe(6)
    name = name or "Игрок"
    st = {"id": gid, "host": uid, "players": [[uid, name]], "phase": "lobby", "created": time.time(),
          "out": [], "hands": {}, "table": [], "passed": [], "msgs": {}, "notes": {}}
    try:
        await _send_table(bot, st, uid)
    except TelegramBadRequest as e:
        await bot.send_message(uid, f"❌ Telegram не принял стол-виджет: <code>{html.escape(str(e)[:200])}</code>")
        return
    await _save(db, gid, st)
    await db.set_setting(OPEN, gid)
    await _set_live(db, gid, True)
    me = await bot.me()
    link = f"https://t.me/{me.username}?start={START}{gid}"
    text = (f"🃏 <b>{html.escape(name)} собирает на дурака</b>\n\n"
            "За столом до 6. Жми «Сесть за стол» - карты придут в личку, играем у Пиздюка (нужен свежий Telegram). "
            "Последний с картами - дурак.\n\n"
            f'👉 <a href="{link}">Сесть за стол</a>')
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🃏 Сесть за стол", url=link)]])
    # сразу в канал (09.11 владелец: «не надо мне это в бот кидать» — раньше шло в личку и пересылалось оттуда)
    channel, post, problem = await channel_id(bot, db), None, ""
    if channel:
        try:
            post = await _send_card(bot, db, channel, text, kb, CARD, CARD_KEY)
            await mark_own(db, post.message_id)  # стёб-бот не обсуждает свой же пост
        except Exception as e:  # noqa: BLE001
            logger.warning("дурак: в канал не ушло: %s", e)
            problem = f"В канал не ушло ({html.escape(str(e)[:150])})"
    else:
        problem = "Канал не нашёл (бот должен быть админом канала)"
    async with _lock(gid):
        st = await _load(db, gid) or st
        if post:
            st["post"] = [channel, post.message_id]
        if problem and st["phase"] == "lobby" and len(st["players"]) < 2:  # в личку — только если не вышло
            await _note(bot, st, uid, f"⚠️ {problem} - перешли ссылку сам: {link}")
        await _save(db, gid, st)
    logger.info("дурак %s: стол от %s, пост %s", gid, name, st.get("post"))


async def join(bot: Bot, db: Database, user, gid: str) -> None:
    """/start dur_<id>: сесть за стол."""
    uid = user.id
    started = False
    async with _lock(gid):
        st = await _load(db, gid)
        if not st:
            await bot.send_message(uid, "🃏 Такого стола нет - жди новый в канале.")
            return
        if uid in _ids(st):  # уже за столом — прислать стол заново (потерял)
            await _send_table(bot, st, uid)
            await _save(db, gid, st)
            return
        if st["phase"] != "lobby":
            await bot.send_message(uid, "🃏 За этим столом уже играют - жди следующий в канале.")
            return
        if len(st["players"]) >= MAX_PLAYERS:
            await bot.send_message(uid, "🃏 Стол полный - жди следующий в канале.")
            return
        name = player_name(user)
        st["players"].append([uid, name])
        host = _nm(st, st["host"])
        await bot.send_message(uid, f"🃏 Ты за столом у {html.escape(host)}! Карты придут сюда, как только начнём. "
                                    "Не видно стол - обнови Telegram.")
        try:
            await _send_table(bot, st, uid)
        except TelegramBadRequest as e:
            st["players"].pop()
            await bot.send_message(uid, f"❌ Telegram не принял стол: <code>{html.escape(str(e)[:200])}</code>")
            await _save(db, gid, st)
            return
        n = len(st["players"])
        notes: dict = {}
        if n >= MAX_PLAYERS:  # сел шестой — раздаём сами
            deal(st, random.Random())
            settle(st, time.time(), notes)
            notes.update(_deal_notes(st))
            started = True
        else:
            notes[st["host"]] = f"🃏 {html.escape(name)} сел за стол ({n} из {MAX_PLAYERS})."
        await _push(bot, st, skip=uid)
        await _send_notes(bot, st, notes)
        await _save(db, gid, st)
    if started and await db.get_setting(OPEN, "") == gid:
        await db.set_setting(OPEN, "")
    logger.info("дурак %s: сел %s (%d)", gid, user.first_name, len(st["players"]))


def _apply(st: dict | None, uid: int, kind: str, arg: str, notes: dict) -> tuple[dict, dict]:
    """Запрос стола → (ответ виджету, что сделать после). Меняет st."""
    ev = {"changed": False, "done": False, "rematch": False, "started": False}
    if not st:
        return {"err": "Стол не найден"}, ev
    if uid not in _ids(st):
        return {"err": "Ты не за этим столом"}, ev
    ph = st["phase"]
    err = None
    if kind == "ug":
        return {"s": json.loads(view(st, uid))}, ev
    if kind == "us":
        if ph != "lobby":
            return {"err": "Игра уже идёт"}, ev
        if uid != st["host"]:
            return {"err": "Начинает хозяин стола"}, ev
        if len(st["players"]) < 2:
            return {"err": "Нужно хотя бы двое"}, ev
        deal(st, random.Random())
        notes.update(_deal_notes(st))
        ev["started"] = True
    elif kind == "un":
        if ph != "end" or st.get("cancel"):
            return {"err": "Игра ещё идёт"}, ev
        if uid != st["host"]:
            return {"err": "Ещё раз - решает хозяин стола"}, ev
        if st.get("next"):
            return {"err": "Уже раздаю"}, ev
        ev["rematch"] = True
        return {"ok": 1}, ev
    elif ph != "play":
        return {"err": "Игра ещё не началась" if ph == "lobby" else "Игра окончена"}, ev
    elif kind == "ua":
        err = attack(st, uid, arg[:2], notes)
    elif kind == "ud":
        err = defend(st, uid, arg[:2], int(arg[2:] or -1) if arg[2:].isdigit() else -1, notes)
    elif kind == "ub":
        err = pass_(st, uid)
    elif kind == "ut":
        err = take(st, uid)
    else:
        err = "Непонятный запрос"
    if err:
        return {"err": err}, ev
    settle(st, time.time(), notes)
    ev["changed"] = True
    ev["done"] = st["phase"] == "end"
    return {"s": json.loads(view(st, uid))}, ev


async def _answer(bot: Bot, qid: str, answer: dict) -> None:
    data = json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
    if len(data.encode()) > MAX_DATA:
        data = '{"ok":1}'
    try:
        await bot(AnswerWidgetQuery(widget_query_id=qid, data=data))
    except Exception as e:  # noqa: BLE001
        logger.info("answerWidgetQuery %s: %s", qid, e)


async def on_query(bot: Bot, db: Database, q: dict) -> None:
    """Запрос стола-виджета (data начинается с «u»)."""
    qid = str(q.get("id") or "")
    uid = int((q.get("from") or {}).get("id") or 0)
    chat, mid = (q.get("chat") or {}).get("id"), q.get("message_id")
    kind, gid, arg = (str(q.get("data") or "").split(":") + ["", "", ""])[:3]
    if not gid or not uid:
        await _answer(bot, qid, {"err": "Непонятный запрос"})
        return
    async with _lock(gid):
        st = await _load(db, gid)
        notes: dict = {}
        try:
            answer, ev = _apply(st, uid, kind, arg, notes)
        except Exception:  # noqa: BLE001
            logger.exception("дурак %s: запрос %r", gid, q.get("data"))
            answer, ev = {"err": "Ошибка у бота - попробуй ещё"}, {"changed": False, "done": False, "rematch": False}
        if st and chat == uid and mid and (st.get("msgs") or {}).get(str(uid)) not in (None, mid):
            st["msgs"][str(uid)] = mid  # играет с этого сообщения — его и править
            ev["changed"] = True
        if not ev["changed"]:
            await _answer(bot, qid, answer)
        else:
            await _save(db, gid, st)
            await _answer(bot, qid, answer)  # нажавшему — сразу
            await _push(bot, st)
            await _send_notes(bot, st, notes, [uid])
            if ev["done"]:
                await _finish(bot, db, st)
            await _save(db, gid, st)
    if ev.get("started") and await db.get_setting(OPEN, "") == gid:
        await db.set_setting(OPEN, "")
    if ev["rematch"]:
        await _rematch(bot, db, gid)


async def _finish(bot: Bot, db: Database, st: dict) -> None:
    """Конец: итог каждому, пост в канал. Вызывается под замком партии."""
    from handlers.channel_posts import channel_id, mark_own

    await _set_live(db, st["id"], False)
    durak = st.get("durak")
    for u in _ids(st):
        await _unnote(bot, st, u)
        text = ("🤝 Ничья - дурака нет." if durak is None else "🤡 Ты дурак!" if u == durak
                else f"🎉 Ты не дурак! Дурак - {html.escape(_nm(st, durak))}.")
        await _note(bot, st, u, text)
    logger.info("дурак %s: конец, дурак %s", st["id"], _nm(st, durak) if durak else "-")
    channel = await channel_id(bot, db)
    if not channel:
        return
    names = ", ".join(html.escape(n) for _u, n in st["players"])
    first = html.escape(_nm(st, st["out"][0])) if st.get("out") else ""
    text = (f"🃏 <b>Дурак у Пиздюка</b>\nИграли: {names}.\n"
            + ("Ничья - дурака нет." if durak is None else f"Дурак - <b>{html.escape(_nm(st, durak))}</b> 🤡")
            + (f"\nПервым вышел {first}." if first and len(st["players"]) > 2 else ""))
    try:
        post = await bot.send_message(channel, text)
        await mark_own(db, post.message_id)
    except Exception as e:  # noqa: BLE001
        logger.info("дурак: итог в канал не ушёл: %s", e)


async def _rematch(bot: Bot, db: Database, gid: str) -> None:
    """«Ещё раз»: те же игроки, новая раздача — в тех же сообщениях со столами."""
    async with _lock(gid):
        old = await _load(db, gid)
        if not old or old.get("next"):
            return
        new = secrets.token_urlsafe(6)
        old["next"] = new
        for u in _ids(old):
            await _unnote(bot, old, u)
        await _save(db, gid, old)
    st = {"id": new, "host": old["host"], "players": old["players"], "phase": "lobby", "created": time.time(),
          "out": [], "hands": {}, "table": [], "passed": [], "msgs": dict(old.get("msgs") or {}), "notes": {}}
    async with _lock(new):
        notes: dict = {}
        deal(st, random.Random())
        settle(st, time.time(), notes)
        notes.update(_deal_notes(st))
        await _save(db, new, st)
        await _set_live(db, new, True)
        await _push(bot, st)
        await _send_notes(bot, st, notes)
        await _save(db, new, st)
    logger.info("дурак %s: ещё раз - %s", gid, new)


async def _tick_one(bot: Bot, db: Database, gid: str, now: float) -> None:
    async with _lock(gid):
        st = await _load(db, gid)
        if not st or st["phase"] not in ("lobby", "play"):
            await _set_live(db, gid, False)
            return
        notes: dict = {}
        if st["phase"] == "lobby":
            if now - st.get("created", now) < LOBBY_TTL:
                return
            st.update(phase="end", cancel="Стол закрыт - так и не начали")
        elif now - st.get("start", now) > GAME_TTL:
            st.update(phase="end", durak=None, cancel="Игра закрыта - слишком долго")
        elif st.get("due") and now >= st["due"]:
            timeout(st, now, notes)
        else:
            return
        await _push(bot, st)
        await _send_notes(bot, st, notes)
        if st["phase"] == "end":
            if st.get("cancel"):
                await _set_live(db, gid, False)
            else:
                await _finish(bot, db, st)
        await _save(db, gid, st)


async def ticker(bot: Bot, db: Database) -> None:
    """Из bot.py: таймеры зависших ходов и закрытие старых столов."""
    try:
        _live.update(json.loads(await db.get_setting(LIVE, "[]") or "[]"))
    except ValueError:
        pass
    while True:
        await asyncio.sleep(TICK)
        now = time.time()
        for gid in list(_live):
            try:
                await _tick_one(bot, db, gid, now)
            except Exception:  # noqa: BLE001
                logger.exception("дурак %s: таймер", gid)


tgbeta.on_widget("u", on_query)


@router.message(Command("durak"))
async def cmd_durak(message: Message, bot: Bot, db: Database, config: Config) -> None:
    """/durak — стол в дурака: стол и картинка-ссылка в личку, ссылка — пересылкой в канал."""
    user = message.from_user
    if user is None or not config.is_admin(user.id):
        return
    if not tgbeta.active():
        from handlers.admin import _restart_when_free

        await tgbeta.enable(db, {"kind": "durak", "user": user.id, "name": player_name(user)})
        await bot.send_message(user.id, "🃏 Стол-виджет работает только через бету Telegram (Bot API 10.4) - "
                                        "включаю её и перезапускаюсь. Через ~15 сек пришлю стол и ссылку для канала."
                                        "\n\nЕсли после этого бот начнёт глючить - /beta off.")
        _restart_when_free()
        return
    await create(bot, db, user.id, player_name(user))
