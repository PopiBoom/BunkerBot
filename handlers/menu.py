"""/menu — меню бота кнопками: картинка с разделами, под ней кнопки.

Нажал раздел — в том же сообщении меняются картинка (своя у каждого раздела) и кнопки действий;
нажал действие — бот выполняет ту же команду, что и при наборе (/mafia, /poll…), от имени нажавшего.
Ходить по разделам может любой, выполнять действия — только админ (как и команды).
Картинки — готовые файлы в assets/menu (их рисует tools/menu_images.py из данных ниже).
"""
from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message

from config import Config
from database import Database

logger = logging.getLogger("menu")
router = Router(name="menu")

IMAGES = Path(__file__).resolve().parents[1] / "assets" / "menu"

YELLOW, RED, PURPLE, BLUE, GOLD, GREY = ((247, 196, 37), (206, 38, 52), (170, 90, 230), (70, 150, 235),
                                         (230, 170, 60), (140, 146, 156))

# Разделы: эмодзи, название, подпись, цвет, строки (эмодзи, название, что делает, команда, действие или
# None — только подсказка на картинке, без кнопки) и примечание внизу картинки.
SECTIONS: dict[str, dict] = {
    "bunker": {"emoji": "🏚", "title": "Бункер", "tagline": "выжить и не пустить лишних", "color": YELLOW,
               "rows": [("▶️", "Начать", "лобби, карточки в личку", "/bunker", "bunker"),
                        ("⏹", "Остановить", "прервать партию", "/bunker_stop", "bunker_stop"),
                        ("🏆", "Рейтинг", "выжившие и звания", "/top", "top"),
                        ("🤖", "Тестовые боты", "добавить в лобби", "/bunker_bots", "bunker_bots")],
               "note": "Режим ведущего: /bunker злой · добрый · кринж"},
    "mafia": {"emoji": "🕵️", "title": "Мафия", "tagline": "ночью убивают, днём казнят", "color": RED,
              "rows": [("▶️", "Начать", "лобби, роли в личку", "/mafia", "mafia"),
                       ("⏹", "Остановить", "прервать партию", "/mafia_stop", "mafia_stop"),
                       ("🏆", "Рейтинг", "победы и звания", "/mafia_top", "mafia_top"),
                       ("🤖", "Тестовые боты", "добавить в лобби", "/mafia_bots", "mafia_bots")],
              "note": "Ночью чат закрыт, мёртвые молчат"},
    "roast": {"emoji": "😈", "title": "Стёб", "tagline": "прожарка, итоги недели, клоуны", "color": PURPLE,
              "rows": [("🔥", "Прожарка", "ответом на сообщение или @ник", "/roast", None),
                       ("📅", "Итоги недели", "номинации по чату", "/week", "week"),
                       ("🤡", "Клоуны дня", "таблица позора", "/clowns", "clowns"),
                       ("📝", "Обещания", "что бот проверит в срок", "/promises", "promises")],
              "note": "Прожарка - ответь /roast на сообщение человека"},
    "fun": {"emoji": "🎲", "title": "Развлечения", "tagline": "опрос, цитаты, статистика", "color": BLUE,
            "rows": [("📊", "Опрос", "«Кто из нас…» по чату", "/poll", "poll"),
                     ("📜", "Цитата", "из золотого фонда", "/quote", "quote"),
                     ("📈", "Статистика", "кто болтает и матерится", "/stats", "stats")],
            "note": "Ответь /quote на сообщение - оно уйдёт в золотой фонд"},
    "top": {"emoji": "🏆", "title": "Рейтинги", "tagline": "звания в Бункере и Мафии", "color": GOLD,
            "rows": [("🏚", "Бункер", "выжившие и звания", "/top", "top"),
                     ("🕵️", "Мафия", "победы и звания", "/mafia_top", "mafia_top"),
                     ("🤡", "Клоуны", "кто чаще клоун дня", "/clowns", "clowns")],
            "note": "Профиль игрока - ответь /top или /mafia_top на его сообщение"},
    "other": {"emoji": "⚙️", "title": "Прочее", "tagline": "помощь, забыть человека", "color": GREY,
              "rows": [("📖", "Все команды", "с описанием", "/help", "help"),
                       ("🧹", "Забыть человека", "ответом на его сообщение", "/forget_me", None),
                       ("😈", "Вернуть", "снова запоминать", "/remember_me", None)],
              "note": "Все команды работают и если набрать их руками"},
}
ORDER = ["bunker", "mafia", "roast", "fun", "top", "other"]
MAIN_CAPTION = "Выбери раздел - кнопки ниже."

_file_ids: dict[str, str] = {}  # картинка -> file_id в Telegram (после первой отправки не грузим заново)


def _photo(key: str):
    return _file_ids.get(key) or FSInputFile(IMAGES / f"{key}.jpg")


def _remember_file(key: str, msg) -> None:
    photos = getattr(msg, "photo", None)
    if photos:
        _file_ids[key] = photos[-1].file_id


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _main_kb() -> InlineKeyboardMarkup:
    buttons = [_btn(f"{SECTIONS[k]['emoji']} {SECTIONS[k]['title']}", f"menu:sec:{k}") for k in ORDER]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i:i + 2] for i in range(0, len(buttons), 2)])


def _section_kb(key: str) -> InlineKeyboardMarkup:
    acts = [_btn(f"{em} {name}", f"menu:act:{key}:{action}") for em, name, _d, _c, action in SECTIONS[key]["rows"]
            if action]
    rows = [acts[i:i + 2] for i in range(0, len(acts), 2)]
    rows.append([_btn("⬅️ Назад", "menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _section_caption(key: str) -> str:
    s = SECTIONS[key]
    return f"{s['emoji']} <b>{s['title']}</b> - {s['tagline']}."


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    if message.chat.type == "private":
        await message.answer("Меню - в чате: напиши там /menu.")
        return
    sent = await message.answer_photo(_photo("main"), caption=MAIN_CAPTION, reply_markup=_main_kb())
    _remember_file("main", sent)


async def _show(call: CallbackQuery, key: str, caption: str, kb: InlineKeyboardMarkup) -> None:
    if call.message is None:
        return
    try:
        edited = await call.message.edit_media(InputMediaPhoto(media=_photo(key), caption=caption), reply_markup=kb)
        _remember_file(key, edited)
    except Exception as e:  # например, «message is not modified» при двойном нажатии
        logger.debug("menu edit failed: %s", e)


@router.callback_query(F.data == "menu:home")
async def cb_home(call: CallbackQuery) -> None:
    await call.answer()
    await _show(call, "main", MAIN_CAPTION, _main_kb())


@router.callback_query(F.data.startswith("menu:sec:"))
async def cb_section(call: CallbackQuery) -> None:
    key = (call.data or "").split(":", 2)[2]
    if key not in SECTIONS:
        await call.answer()
        return
    await call.answer()
    await _show(call, key, _section_caption(key), _section_kb(key))


@router.callback_query(F.data.startswith("menu:act:"))
async def cb_action(call: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    try:
        _, _, key, action = (call.data or "").split(":", 3)
    except ValueError:
        await call.answer()
        return
    if not config.is_admin(call.from_user.id):
        await call.answer("Это может только админ.", show_alert=True)
        return
    if call.message is None or not any(a == action for *_r, a in SECTIONS.get(key, {}).get("rows", [])):
        await call.answer()
        return
    await call.answer()
    await run_action(action, call, bot, db, config)


async def run_action(action: str, call: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    """Выполняет команду так же, как если бы нажавший набрал её в этом чате."""
    from games.bunker import game as bunker
    from games.mafia import game as mafia
    from handlers import fun, help as help_, quotes, roast, stats

    msg = call.message.model_copy(update={"from_user": call.from_user, "text": f"/{action}",
                                          "reply_to_message": None})
    cmd = CommandObject(prefix="/", command=action, args=None)
    actions = {
        "bunker": lambda: bunker.cmd_bunker(msg, cmd, db),
        "bunker_stop": lambda: bunker.cmd_bunker_stop(msg),
        "top": lambda: bunker.cmd_top(msg, cmd, db),
        "bunker_bots": lambda: bunker.cmd_bunker_bots(msg, cmd),
        "mafia": lambda: mafia.cmd_mafia(msg, cmd, db),
        "mafia_stop": lambda: mafia.cmd_mafia_stop(msg),
        "mafia_top": lambda: mafia.cmd_mafia_top(msg, cmd, db),
        "mafia_bots": lambda: mafia.cmd_mafia_bots(msg, cmd),
        "week": lambda: roast.cmd_week(msg, bot, db),
        "clowns": lambda: roast.cmd_clowns(msg, db),
        "promises": lambda: roast.cmd_promises(msg, db),
        "poll": lambda: fun.cmd_poll(msg, bot, db),
        "quote": lambda: quotes.cmd_quote(msg, cmd, bot, db),
        "stats": lambda: stats.cmd_stats(msg, cmd, bot, db),
        "help": lambda: help_.cmd_help(msg, config),
    }
    run = actions.get(action)
    if run is None:
        return
    from ephemeral import TEMP_COMMANDS, TTL, auto_delete

    token = auto_delete.set(TTL if action in TEMP_COMMANDS else 0)  # ответы-справки удалятся через минуту
    try:
        await run()
    except Exception:
        logger.exception("menu action %s failed", action)
    finally:
        auto_delete.reset(token)
