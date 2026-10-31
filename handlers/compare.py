"""/compare — слепое сравнение моделей на постах для канала (только админ, в личке).

Бот пишет пост на одну и ту же тему каждой моделью из ai.COMPARE_MODELS (Claude — напрямую,
остальные — через OpenRouter), перемешивает и присылает под буквами A, B, C… без названий.
Админ жмёт лучший — бот раскрывает, где какая модель, и предлагает поставить победителя
писать посты (настройка post_model в базе, перекрывает AI_MODEL_POSTS из .env).
"""
from __future__ import annotations

import asyncio
import html
import logging
import random
import secrets
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import ai
import texts
from config import Config
from database import Database

logger = logging.getLogger("compare")
router = Router(name="compare")
router.message.filter(F.chat.type == "private")

POST_MODEL_KEY = "post_model"
LETTERS = "ABCDEFGHIJ"
_runs: dict[str, dict] = {}  # id сравнения -> {"models": {буква: модель}, "costs": {...}}


def _short(model: str) -> str:
    return model.split("/")[-1]


@router.message(Command("compare"))
async def cmd_compare(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    if message.from_user is None or not config.is_admin(message.from_user.id):
        await message.answer(texts.ADMIN_ONLY)
        return
    models = [m for m in ai.COMPARE_MODELS if not ai.is_openrouter(m) or ai.openrouter_available()]
    note = "" if ai.openrouter_available() else (
        "\n\n⚠️ Ключа OpenRouter нет — сравниваю только модели Claude. Добавь в .env на сервере "
        "<code>OPENROUTER_API_KEY=...</code> и перезапусти бота.")
    if len(models) < 2:
        await message.answer("Сравнивать нечего: нужен ключ ИИ." + note)
        return
    from handlers.channel_posts import _style_examples

    topic = (command.args or "").strip() or ai.pick_post_topic([])
    status = await message.answer(f"🧪 Пишу пост на тему «{html.escape(topic)}» {len(models)} моделями… "
                                  f"(до пары минут){note}")
    examples = await _style_examples(db)
    spent: dict[str, float] = {}

    async def one(model: str) -> tuple[str, str]:
        t0 = time.time()
        try:
            text = await ai.own_channel_post(examples, [], model=model, topic=topic)
        except Exception as e:
            logger.warning("compare %s failed: %s", model, e)
            text = f"(не ответила: {str(e)[:150]})"
        spent[model] = time.time() - t0
        return model, text or "(пусто)"

    results = await asyncio.gather(*(one(m) for m in models))
    random.shuffle(results)
    run_id = secrets.token_hex(4)
    _runs[run_id] = {"models": {LETTERS[i]: m for i, (m, _t) in enumerate(results)}, "topic": topic}
    if len(_runs) > 20:
        _runs.pop(next(iter(_runs)))
    try:
        await status.delete()
    except Exception:
        pass
    await message.answer(f"🧪 <b>Слепое сравнение</b> · тема: {html.escape(topic)}\n"
                         "Читай и выбирай, что смешнее — названия моделей покажу после.")
    for i, (_m, text) in enumerate(results):
        await message.answer(f"<b>{LETTERS[i]}</b>\n\n{html.escape(text)}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🏆 {LETTERS[i]}", callback_data=f"cmp:{run_id}:{LETTERS[i]}")
         for i in range(j, min(j + 3, len(results)))]
        for j in range(0, len(results), 3)
    ])
    await message.answer("Какой смешнее всего?", reply_markup=kb)


@router.callback_query(F.data.startswith("cmp:"))
async def cb_compare_vote(call: CallbackQuery, db: Database, config: Config) -> None:
    if not config.is_admin(call.from_user.id):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    _, run_id, letter = (call.data or "::").split(":", 2)
    run = _runs.get(run_id)
    if not run:
        await call.answer("Это сравнение уже устарело — запусти /compare заново.", show_alert=True)
        return
    winner = run["models"].get(letter)
    await call.answer(f"Победил {letter}")
    current = await db.get_setting(POST_MODEL_KEY, "") or ai.POST_MODEL
    lines = [f"🏆 <b>Победил {letter}: {html.escape(_short(winner))}</b>", ""]
    for ltr, model in run["models"].items():
        mark = "🏆" if ltr == letter else "•"
        now = " ← сейчас пишет посты" if model == current else ""
        lines.append(f"{mark} {ltr} — <code>{html.escape(model)}</code>{now}")
    kb = None
    if winner != current:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"📢 Пусть {_short(winner)} пишет посты", callback_data=f"cmpset:{run_id}:{letter}"),
        ]])
        lines += ["", "Поставить победителя писать посты в канал?"]
    if call.message:
        await call.message.edit_text("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("cmpset:"))
async def cb_compare_set(call: CallbackQuery, db: Database, config: Config) -> None:
    if not config.is_admin(call.from_user.id):
        await call.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    _, run_id, letter = (call.data or "::").split(":", 2)
    run = _runs.get(run_id)
    model = run["models"].get(letter) if run else None
    if not model:
        await call.answer("Сравнение устарело — запусти /compare заново.", show_alert=True)
        return
    await db.set_setting(POST_MODEL_KEY, model)
    await call.answer("Готово ✅", show_alert=False)
    if call.message:
        await call.message.edit_text(f"✅ Посты в канал теперь пишет <code>{html.escape(model)}</code>.\n"
                                     "Поменять — снова /compare и выбрать другую.")
