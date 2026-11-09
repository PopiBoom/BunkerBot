"""/stop_all — завершить все игры разом (09.11 владелец: «добавь команду, чтобы я мог завершить все игры»).

Бункер и Мафия во всех чатах (как /bunker_stop и /mafia_stop: лобби удаляется, табло откреплено, в чат —
«остановлена»), шахматы кнопками и шахматы-виджет (на досках «Партия остановлена», вызов из канала удаляется),
дурак (все столы, `durak.close_all`), блэкджек (столы закрываются, посреди раунда ставки назад). Только админ (как
все команды); работает и в личке, и в чате. Ответ — что сколько закрыл.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from database import Database
from ephemeral import temp_reply

logger = logging.getLogger("stopall")
router = Router(name="stopall")


async def _stop_rooms(bot: Bot) -> tuple[int, int]:
    """Бункер и Мафия: (сколько партий Бункера, сколько Мафии)."""
    from games.bunker import game as bunker
    from games.mafia import game as mafia

    counts = []
    for mod, title in ((bunker, "Бункер"), (mafia, "Мафия")):
        n = 0
        for chat_id, g in list(mod.games.items()):
            in_lobby = g.phase == "lobby"
            mod._end_game(g)
            n += 1
            if in_lobby and getattr(g, "lobby_msg_id", None):
                try:
                    await bot.delete_message(chat_id, g.lobby_msg_id)
                except Exception:  # noqa: BLE001 — уже удалено
                    pass
            if getattr(g, "board_msg_id", None):
                try:
                    await bot.unpin_chat_message(chat_id, message_id=g.board_msg_id)
                except Exception:  # noqa: BLE001
                    pass
            try:
                await bot.send_message(chat_id, f"🛑 {title} остановлен{'а' if title == 'Мафия' else ''} админом.")
            except Exception as e:  # noqa: BLE001
                logger.info("stop_all: %s в %s: %s", title, chat_id, e)
        counts.append(n)
    return counts[0], counts[1]


@router.message(Command("stop_all"))
async def cmd_stop_all(message: Message, bot: Bot, db: Database) -> None:
    """/stop_all — все игры во всех чатах и в личке завершить."""
    from games.blackjack import game as blackjack
    from games.chess import game as chess
    from games.chess import widget as wchess
    from games.durak import game as durak

    done: list[tuple[str, int]] = []
    try:
        b, m = await _stop_rooms(bot)
        done += [("Бункер", b), ("Мафия", m)]
    except Exception:
        logger.exception("stop_all: бункер/мафия")
    for title, close in (("шахматы-виджет", wchess.close_all), ("шахматы кнопками", chess.close_all),
                         ("дурак", durak.close_all), ("блэкджек", blackjack.close_all)):
        try:
            done.append((title, await close(bot, db)))
        except Exception:
            logger.exception("stop_all: %s", title)
            done.append((title + " (ошибка, см. лог)", 0))
    stopped = [f"{t} - {n}" for t, n in done if n or "ошибка" in t]
    logger.info("stop_all: %s", ", ".join(f"{t} {n}" for t, n in done))
    await temp_reply(message, "🛑 Остановил: " + ", ".join(stopped) + "." if stopped else "🛑 Ни одна игра не шла.")
