"""Админ-панель: статистика, рассылка, редактирование текстов."""
from __future__ import annotations

import asyncio

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
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
