"""Вход в аккаунт, которым Пиздюк заходит в голосовой чат. Запускать ОДИН раз на сервере:

    cd /opt/bunkerbot && .venv/bin/python tools/voice_login.py

Спросит API ID и API HASH (my.telegram.org → API development tools, войти номером ЭТОГО аккаунта), потом телефон,
код из Telegram и облачный пароль, если есть. В конце допишет в .env три строки VC_API_ID, VC_API_HASH, VC_SESSION
и попросит перезапустить бота. Строка VC_SESSION — полный доступ к аккаунту: никому не показывать, в чат и в
репозиторий не класть (.env в репозиторий не попадает).
"""
from __future__ import annotations

import asyncio
import getpass
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"


def _save(values: dict[str, str]) -> None:
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in values]
    lines += [f"{k}={v}" for k, v in values.items()]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")  # владелец и права файла остаются прежними


async def login(api_id: str, api_hash: str) -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.start(phone=lambda: input("Телефон аккаунта (+7…): ").strip(),
                       code_callback=lambda: input("Код из Telegram: ").strip(),
                       password=lambda: getpass.getpass("Облачный пароль (если нет — Enter): "))
    me = await client.get_me()
    session = client.session.save()
    await client.disconnect()
    _save({"VC_API_ID": api_id, "VC_API_HASH": api_hash, "VC_SESSION": session})
    print(f"\nГотово: вошёл как {me.first_name} (@{me.username or '-'}). Записал в {ENV}.")
    print("Добавь этот аккаунт в группу и перезапусти бота: systemctl restart bunkerbot")
    print("Потом в группе: запусти голосовой чат и напиши /voice")


if __name__ == "__main__":
    asyncio.run(login(input("API ID (цифры с my.telegram.org): ").strip(), input("API HASH: ").strip()))
