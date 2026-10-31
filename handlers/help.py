"""/help — все команды по разделам (тот же список, что в меню «/»)."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from commands import ADMIN_SECTION, GROUP_SECTIONS, PRIVATE_SECTIONS, help_text
from config import Config

router = Router(name="help")


@router.message(Command("help"))
async def cmd_help(message: Message, config: Config) -> None:
    if message.chat.type == "private":
        sections = list(PRIVATE_SECTIONS)
        if message.from_user and config.is_admin(message.from_user.id):
            sections.insert(-1, ADMIN_SECTION)
    else:
        sections = GROUP_SECTIONS
    await message.answer(help_text(sections), disable_web_page_preview=True)
