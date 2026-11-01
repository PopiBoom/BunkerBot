"""Админ-панель: статистика, рассылка, редактирование текстов."""
from __future__ import annotations

import asyncio
import os
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import texts
from config import Config
from database import Database
from keyboards import (
    admin_keyboard,
    cancel_keyboard,
    confirm_broadcast_keyboard,
)

router = Router(name="admin")
logger = logging.getLogger("admin")
# Админка — только в личке, чтобы не светить панель и рассылку в группах.
router.message.filter(F.chat.type == "private")


class AdminStates(StatesGroup):
    broadcast = State()
    edit_welcome = State()
    edit_reward = State()


def _is_admin(user_id: int, config: Config) -> bool:
    return config.is_admin(user_id)


@router.message(Command("admin"))
async def cmd_admin(message: Message, config: Config) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, config):
        await message.answer(texts.ADMIN_ONLY)
        return
    await message.answer("⚙️ Панель администратора", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:cancel")
async def admin_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Отменено")
    if call.message:
        await call.message.answer("⚙️ Панель администратора", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:stats")
async def admin_stats(call: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    total = await db.count_users()
    rewarded = await db.count_rewarded()
    await call.answer()
    if call.message:
        await call.message.answer(
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Запускали бота: <b>{total}</b>\n"
            f"🎁 Получили контент: <b>{rewarded}</b>"
        )


@router.callback_query(F.data == "admin:roast_toggle")
async def admin_roast_toggle(call: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    on = (await db.get_setting("roast_enabled", "1")) == "1"
    await db.set_setting("roast_enabled", "0" if on else "1")
    state = "выключен ⛔" if on else "включён 🔥"
    await call.answer(f"Стёб-бот {state}", show_alert=True)
    if call.message:
        await call.message.answer(
            f"🔥 Стёб-бот {state}\n\n"
            "Когда включён: запоминает сообщения в группах (30 дней), ведёт досье, "
            "иногда сам подкалывает, отвечает на обращения, /roast, /week. "
            "Участники могут отказаться: /forget_me."
        )


@router.message(Command("setkey"))
async def cmd_setkey(message: Message, command: CommandObject, db: Database, config: Config) -> None:
    """Ключ OpenRouter — в базу бота на сервере (не в GitHub). Сообщение с ключом бот сразу удаляет."""
    if message.from_user is None or not _is_admin(message.from_user.id, config):
        await message.answer(texts.ADMIN_ONLY)
        return
    import ai

    arg = (command.args or "").strip()
    if not arg:
        await message.answer(
            "🔑 <b>Ключ OpenRouter</b>\n\n<code>/setkey sk-or-...</code> — сохранить (сообщение с ключом удалю)\n"
            "<code>/setkey off</code> — удалить\n\n"
            f"Сейчас: {'есть ✅' if ai.openrouter_available() else 'нет'}. Ключ хранится в базе бота на сервере, "
            "в GitHub не попадает (но есть в файлах /backup — их не пересылай).")
        return
    try:
        await message.delete()  # ключ не должен висеть в переписке
    except Exception:
        pass
    if arg.lower() in ("off", "удалить", "-"):
        await db.set_setting("openrouter_key", "")
        ai.set_openrouter_key("")
        await message.answer("🔑 Ключ OpenRouter удалён из базы бота.")
        return
    if not arg.startswith("sk-or-"):
        await message.answer("Это не похоже на ключ OpenRouter (он начинается с <code>sk-or-</code>).")
        return
    balance = await _openrouter_balance(arg)
    if balance is None:
        await message.answer("❌ OpenRouter не принял ключ — проверь, что он скопирован целиком и не удалён.")
        return
    await db.set_setting("openrouter_key", arg)
    ai.set_openrouter_key(arg)
    await message.answer(f"✅ Ключ OpenRouter сохранён на сервере, сообщение с ним удалил.\n"
                         f"Баланс: <b>${balance:.2f}</b>. Теперь работают /compare и Gemini в постах.")


async def _openrouter_balance(key: str) -> float | None:
    """Остаток на OpenRouter или None, если ключ не принят."""
    import aiohttp

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=True) as s:
            async with s.get("https://openrouter.ai/api/v1/credits",
                             headers={"Authorization": f"Bearer {key}"}) as r:
                if r.status != 200:
                    return None
                d = (await r.json()).get("data") or {}
                return float(d.get("total_credits") or 0) - float(d.get("total_usage") or 0)
    except Exception as e:
        logger.warning("openrouter balance check failed: %s", e)
        return None


@router.callback_query(F.data == "admin:chan_posts")
async def admin_chan_posts(call: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    from handlers import channel_posts as CP

    if (await db.get_setting(CP.MODE_KEY, "off")) == "test":
        await CP.stop(db)
        await call.answer("Посты в канал выключены ⛔", show_alert=True)
        return
    chan = await CP.channel_id(bot, db)
    if chan is None:
        await call.answer("Не нашёл канал: привяжи его к чату (Канал → Обсуждение) "
                          "или впиши BOT_CHANNEL_ID в .env.", show_alert=True)
        return
    await CP.start_test(db, call.from_user.id)
    await call.answer("Тест включён 📢", show_alert=True)
    if call.message:
        await call.message.answer(
            f"📢 <b>Тест постов в канал включён</b>\n\n"
            f"Бот пишет в канал от себя каждые {CP.TEST_EVERY // 60} мин, через {CP.TEST_FOR // 60} мин "
            "выключится сам и напишет тебе. Выключить раньше — та же кнопка.\n\n"
            "Нужно, чтобы бот был админом канала с правом «Публикация сообщений». В постах — его мысли "
            "в духе канала, без имён и переписки из чата (канал видят подписчики). Свои посты он в чате "
            "не комментирует. Пишут по очереди Opus и Gemini (без ключа OpenRouter — только Opus) или победитель /compare — каждый пост ≈ $0.003-0.03."
        )


@router.callback_query(F.data == "admin:quiet_kick")
async def admin_quiet_kick(call: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    from handlers.fun import DAY_FROM, DAY_TO, QUIET_HOURS, QUIET_KEY

    on = (await db.get_setting(QUIET_KEY, "1")) == "1"
    await db.set_setting(QUIET_KEY, "0" if on else "1")
    state = "выключен ⛔" if on else "включён 🤔"
    await call.answer(f"Вопрос дня {state}", show_alert=True)
    if call.message:
        await call.message.answer(
            f"🤔 Вопрос дня {state}\n\n"
            f"Когда включён: если в чате {QUIET_HOURS} часа никто не пишет (с {DAY_FROM}:00 до {DAY_TO}:00 МСК), "
            "бот подкидывает провокационный вопрос по темам чата или опрос «Кто из нас…». "
            "Не чаще раза в сутки; в чат, который молчит больше 3 дней, не пишет."
        )


@router.callback_query(F.data == "admin:roast_freq")
async def admin_roast_freq(call: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    from handlers.roast import get_interval, interval_label
    from keyboards import roast_interval_keyboard

    current = await get_interval(db)
    await call.answer()
    if call.message:
        await call.message.answer(_freq_text(interval_label(current)), reply_markup=roast_interval_keyboard(current))


def _freq_text(label: str) -> str:
    return (
        f"⏱ <b>Как часто стёб-бот сам встревает в разговор</b>\n\n"
        f"Сейчас: <b>{label}</b>\n\n"
        "Бот встревает, только если с прошлого раза в чате появились новые сообщения: "
        "чем чаще интервал, тем меньше их нужно. На ответы на грубость и обращения это не влияет."
    )


@router.callback_query(F.data.startswith("admin:roast_freq:"))
async def admin_roast_freq_set(call: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    from handlers.roast import INTERVAL_KEY, INTERVALS, interval_label
    from keyboards import roast_interval_keyboard

    try:
        sec = int((call.data or "").rsplit(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    if sec not in {s for s, _, _ in INTERVALS}:
        await call.answer()
        return
    await db.set_setting(INTERVAL_KEY, str(sec))
    await call.answer(f"Готово: {interval_label(sec)}")
    if call.message:
        try:  # обновляем и текст «Сейчас: …», и галочку
            await call.message.edit_text(_freq_text(interval_label(sec)), reply_markup=roast_interval_keyboard(sec))
        except Exception as e:
            logger.warning("interval menu edit failed: %s", e)


@router.callback_query(F.data == "admin:roast_import")
async def admin_roast_import(call: CallbackQuery, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    await call.answer()
    if call.message:
        await call.message.answer(
            "📥 <b>Загрузить историю чата</b>\n\n"
            "Боты не могут читать старые сообщения, поэтому историю нужно выгрузить вручную:\n\n"
            "1. Telegram Desktop → откройте чат → ⋮ → «Экспорт истории чата».\n"
            "2. Снимите галочки с фото/видео/файлов, формат — <b>JSON</b>.\n"
            "3. Пришлите сюда, в личку боту, файл <code>result.json</code>.\n\n"
            "Бот возьмёт последние 2000 сообщений и составит досье на всех участников."
        )


@router.message(F.document)
async def admin_import_file(message: Message, bot: Bot, db: Database, config: Config) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, config):
        return
    doc = message.document
    if doc and (doc.file_name or "").lower().endswith(".db"):
        await _restore_db(message, bot, db)
        return
    if not doc or not (doc.file_name or "").lower().endswith(".json"):
        await message.answer("Пришлите выгрузку чата (result.json) или бэкап базы (*.db).")
        return
    if (doc.file_size or 0) > 20 * 1024 * 1024:
        await message.answer("Файл больше 20 МБ — Telegram не даст боту его скачать. "
                             "Выгрузите без медиа или за более короткий период.")
        return
    import json

    from handlers.roast import import_history

    status = await message.answer("📥 Скачиваю выгрузку…")
    try:
        buf = await bot.download(doc)
        data = json.loads(buf.read().decode("utf-8"))
    except Exception as e:
        await status.edit_text(f"Не получилось прочитать файл: {e}")
        return

    async def progress(text: str) -> None:
        try:
            await status.edit_text(text)
        except Exception:
            pass

    try:
        chat_id, n, people, built = await import_history(db, data, progress)
    except Exception as e:
        logger.exception("import failed")
        await status.edit_text(f"Импорт сломался: {e}")
        return
    await status.edit_text(
        f"✅ Готово!\n\nЧат: <code>{chat_id}</code>\nНовых сообщений загружено: <b>{n}</b>\n"
        f"Участников: <b>{people}</b>\nДосье составлено: <b>{built}</b>\n\n"
        "Теперь бот знает ваши фразы и может стебать по старым грехам. "
        "Хранятся последние 3000 сообщений чата, досье — бессрочно."
    )


def _react_text(label: str) -> str:
    return (
        "😈 <b>Как часто бот ставит реакции на сообщения</b>\n\n"
        f"Сейчас: <b>{label}</b>\n\n"
        "Редко ≈ 5% сообщений, иногда ≈ 15%, часто ≈ 35%. Реакцию по смыслу выбирает ИИ "
        "(🤡 понты, 💩 бред, 🥱 нытьё, 🤣 смешно…). На грубость боту и наезды на батю — 🖕 всегда, "
        "бате — ❤️/🔥 чаще остальных."
    )


@router.callback_query(F.data == "admin:react")
async def admin_react(call: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    from handlers.roast import get_react_rate, react_label
    from keyboards import react_rate_keyboard

    rate = await get_react_rate(db)
    await call.answer()
    if call.message:
        await call.message.answer(_react_text(react_label(rate)), reply_markup=react_rate_keyboard(rate))


@router.callback_query(F.data.startswith("admin:react:"))
async def admin_react_set(call: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    from handlers.roast import REACT_KEY, REACT_OPTIONS, react_label
    from keyboards import react_rate_keyboard

    try:
        rate = float((call.data or "").rsplit(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    if not any(abs(rate - r) < 1e-9 for r, _ in REACT_OPTIONS):
        await call.answer()
        return
    await db.set_setting(REACT_KEY, str(rate))
    await call.answer(f"Готово: {react_label(rate)}")
    if call.message:
        try:
            await call.message.edit_text(_react_text(react_label(rate)), reply_markup=react_rate_keyboard(rate))
        except Exception as e:
            logger.warning("react menu edit failed: %s", e)


# --- Бэкап и восстановление базы (для переезда на сервер) ---

_DB_TABLES = {"users", "settings", "chat_messages", "dossiers", "bunker_stats"}  # stickers досоздаст init()


AUTO_BACKUP_KEY = "auto_backup"         # "1" — каждый день присылать бэкап админу в личку
AUTO_BACKUP_LAST = "auto_backup_last"   # дата (МСК) последнего автобэкапа
AUTO_BACKUP_HOUR = 5                    # МСК: ночью, когда в чате тихо
MAX_BACKUP_SIZE = 48 * 1024 * 1024      # Telegram не даст боту отправить файл больше 50 МБ


@router.message(Command("backup"))
async def cmd_backup(message: Message, bot: Bot, db: Database, config: Config) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, config):
        await message.answer(texts.ADMIN_ONLY)
        return
    await _send_backup(bot, db, message.chat.id)


@router.callback_query(F.data == "admin:backup")
async def admin_backup(call: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    await call.answer()
    if call.message:
        await _send_backup(bot, db, call.message.chat.id)


@router.callback_query(F.data == "admin:auto_backup")
async def admin_auto_backup(call: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    on = (await db.get_setting(AUTO_BACKUP_KEY, "1")) == "1"
    await db.set_setting(AUTO_BACKUP_KEY, "0" if on else "1")
    state = "выключен ⛔" if on else "включён ✅"
    await call.answer(f"Автобэкап {state}", show_alert=True)
    if call.message:
        await call.message.answer(
            f"🗄 Автобэкап {state}\n\n"
            f"Когда включён: каждую ночь (около {AUTO_BACKUP_HOUR}:00 МСК) бот присылает сюда файл "
            "с базой — досье, переписка, рейтинг, обещания, настройки. Если с сервером что-то "
            "случится, пришлите последний файл боту в личку — всё восстановится."
        )


def _make_backup(path: str) -> str:
    """Согласованная копия базы через sqlite backup API (работает и пока бот пишет в базу)."""
    import sqlite3
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    src = sqlite3.connect(path)
    dst = sqlite3.connect(tmp)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return tmp


async def _send_backup(bot: Bot, db: Database, chat_id: int, auto: bool = False) -> bool:
    """Бэкап базы файлом в личку. True — отправлен."""
    import time as _t

    from aiogram.types import FSInputFile

    tmp = await asyncio.to_thread(_make_backup, db.path)
    try:
        size = await asyncio.to_thread(os.path.getsize, tmp)
        if size > MAX_BACKUP_SIZE:
            await bot.send_message(chat_id, f"⚠️ База выросла до {size // (1024 * 1024)} МБ — "
                                            "Telegram не даст отправить её файлом. Скажи Claude, чтобы ужал бэкап.")
            return False
        name = f"bunkerbot-backup-{_t.strftime('%Y%m%d-%H%M')}.db"
        head = "🗄 Ночной автобэкап базы" if auto else "💾 Бэкап базы"
        await bot.send_document(
            chat_id, FSInputFile(tmp, filename=name), disable_notification=auto,
            caption=f"{head}: досье, переписка, рейтинги, обещания, настройки.\n\n"
                    "Чтобы восстановить на другом сервере — просто пришлите этот файл боту в личку.",
        )
        return True
    finally:
        os.remove(tmp)


async def auto_backup_loop(bot: Bot, db: Database, config: Config) -> None:
    """Раз в сутки ночью присылает бэкап базы первому админу (бате). Запускается из bot.py."""
    from datetime import datetime, timedelta, timezone

    msk = timezone(timedelta(hours=3))
    while True:
        try:
            now = datetime.now(msk)
            today = now.strftime("%Y-%m-%d")
            if (now.hour >= AUTO_BACKUP_HOUR and config.admin_ids
                    and (await db.get_setting(AUTO_BACKUP_KEY, "1")) == "1"
                    and (await db.get_setting(AUTO_BACKUP_LAST, "")) != today):
                await db.set_setting(AUTO_BACKUP_LAST, today)  # не чаще раза в сутки, даже если сбой
                await _send_backup(bot, db, config.admin_ids[0], auto=True)
                logger.info("auto backup sent")
        except Exception:
            logger.exception("auto backup failed")
        await asyncio.sleep(600)


async def _restore_db(message: Message, bot: Bot, db: Database) -> None:
    import shutil
    import sqlite3
    import tempfile
    import time as _t

    doc = message.document
    if (doc.file_size or 0) > 20 * 1024 * 1024:
        await message.answer("Файл больше 20 МБ — Telegram не даст боту его скачать.")
        return
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    await bot.download(doc, destination=tmp)
    try:
        con = sqlite3.connect(tmp)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
    except Exception as e:
        await message.answer(f"Это не база бота: {e}")
        os.remove(tmp)
        return
    if not _DB_TABLES <= tables:
        await message.answer("Это не бэкап этого бота — не хватает таблиц: " + ", ".join(sorted(_DB_TABLES - tables)))
        os.remove(tmp)
        return
    old = f"{db.path}.before-restore-{_t.strftime('%Y%m%d-%H%M%S')}"
    if await asyncio.to_thread(os.path.exists, db.path):
        await asyncio.to_thread(shutil.copy2, db.path, old)
    await asyncio.to_thread(shutil.move, tmp, db.path)
    await db.init()  # досоздаст таблицы, если бэкап старее кода
    await message.answer(
        "✅ База восстановлена: досье, переписка, рейтинги, обещания и настройки на месте.\n"
        f"Прежняя база сохранена рядом: <code>{os.path.basename(old)}</code>"
    )


@router.callback_query(F.data == "admin:ai_costs")
async def admin_ai_costs(call: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    import ai

    r = await db.ai_usage_report()

    def money(x: float) -> str:
        return f"${x:.2f}" if x >= 0.01 or x == 0 else f"${x:.4f}"

    games_n = r["games"] or 0
    avg = (r["total"][0] / games_n) if games_n else 0
    lines = [
        "💰 <b>Расходы на ИИ</b>",
        "━━━━━━━━━━━━━━━",
        "",
        f"🎲 Последняя партия: <b>{money(r['last'][0])}</b> · запросов: {r['last'][1]}",
        f"📅 Сегодня: <b>{money(r['today'][0])}</b>",
        f"🗓 За 30 дней: <b>{money(r['month'][0])}</b>",
        f"♾ За всё время: <b>{money(r['total'][0])}</b> · партий: {games_n} · в среднем {money(avg)}",
    ]
    if r["models"]:
        lines += ["", "<b>По моделям:</b>"]
        for model, cost, tin, tout, n in r["models"]:
            lines.append(f"• {model}: {money(cost)} · {n} запр. · {tin // 1000}K вход / {tout // 1000}K выход")
    lines += [
        "",
        f"⚙️ Бункер (сценарий, вердикт), прожарки, эпилог Мафии: <code>{ai.MODEL}</code>",
        f"💬 Ответы в чате, ведущий Мафии, редактор: <code>{ai.REPLY_MODEL}</code>",
        f"⚡️ Реплики ведущего Бункера, описания фото и стикеров: <code>{ai.FAST_MODEL}</code>",
        "",
        "<i>Оценка по ценам Anthropic, без налогов. Точный счёт и баланс — "
        "platform.claude.com → Billing.</i>",
    ]
    await call.answer()
    if call.message:
        await call.message.answer("\n".join(lines))


@router.callback_query(F.data == "admin:edit_welcome")
async def admin_edit_welcome(call: CallbackQuery, state: FSMContext, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    await state.set_state(AdminStates.edit_welcome)
    await call.answer()
    if call.message:
        await call.message.answer(
            "Пришли новый текст приветствия.\n"
            "Можно использовать <code>{name}</code> — подставится имя пользователя.",
            reply_markup=cancel_keyboard(),
        )


@router.callback_query(F.data == "admin:edit_reward")
async def admin_edit_reward(call: CallbackQuery, state: FSMContext, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    await state.set_state(AdminStates.edit_reward)
    await call.answer()
    if call.message:
        await call.message.answer(
            "Пришли новый текст награды (ссылка / промокод / инструкция).",
            reply_markup=cancel_keyboard(),
        )


@router.message(AdminStates.edit_welcome)
async def save_welcome(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, config):
        return
    await db.set_setting(texts.KEY_WELCOME, message.html_text)
    await state.clear()
    await message.answer("✅ Текст приветствия обновлён.", reply_markup=admin_keyboard())


@router.message(AdminStates.edit_reward)
async def save_reward(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, config):
        return
    await db.set_setting(texts.KEY_REWARD, message.html_text)
    await state.clear()
    await message.answer("✅ Текст награды обновлён.", reply_markup=admin_keyboard())


# --- Рассылка ---

@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast(call: CallbackQuery, state: FSMContext, config: Config) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    await state.set_state(AdminStates.broadcast)
    await call.answer()
    if call.message:
        await call.message.answer(
            "Пришли сообщение для рассылки (текст). Потом подтвердишь отправку.",
            reply_markup=cancel_keyboard(),
        )


@router.message(AdminStates.broadcast)
async def broadcast_preview(message: Message, state: FSMContext, config: Config) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, config):
        return
    if not (message.text or "").strip():
        await message.answer("Рассылка — только текстом. Пришли текст сообщения.", reply_markup=cancel_keyboard())
        return
    await state.update_data(text=message.html_text)
    await message.answer("👆 Так будет выглядеть рассылка. Отправляем?")
    await message.answer(message.html_text, reply_markup=confirm_broadcast_keyboard())


@router.callback_query(F.data == "admin:broadcast_go")
async def broadcast_send(
    call: CallbackQuery, state: FSMContext, bot: Bot, db: Database, config: Config
) -> None:
    if not _is_admin(call.from_user.id, config):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    data = await state.get_data()
    text = data.get("text")
    await state.clear()
    await call.answer()
    if not text:
        if call.message:
            await call.message.answer("Нечего отправлять. Начни заново через /admin.")
        return

    user_ids = await db.all_user_ids()
    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            failed += 1  # пользователь заблокировал бота
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # ~20 сообщений/сек, лимит Telegram

    if call.message:
        await call.message.answer(
            f"📢 Рассылка завершена.\n✅ Доставлено: {sent}\n⚠️ Не доставлено: {failed}"
        )
