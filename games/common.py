"""Общее для игр: одинаковый вид подписи лобби у Бункера и Мафии (просьба владельца)."""
from __future__ import annotations

import html

HR = "━━━━━━━━━━━━━━━"


def lobby_text(title: str, players: list[tuple[str, bool, bool]], max_players: int, min_players: int,
               mode: str, block_title: str = "", block: list[str] | None = None) -> str:
    """Подпись лобби. players — [(имя, ведущий ли, открыл ли бота)]; block — строки «• …» под заголовком
    block_title (состав Мафии, места в Бункере), показываются, когда игроков уже хватает."""
    n = len(players)
    lines = [f"<b>{title}</b>", HR, "",
             f"Игроков: <b>{n}</b> из {max_players} (нужно от {min_players})",
             f"Ведущий: {mode}", ""]
    for name, host, dm_ok in players:
        lines.append(f"• {html.escape(name)}" + (" (ведущий)" if host else "")
                     + ("" if dm_ok else " — <i>не открыл бота</i>"))
    if block and n >= min_players:
        lines += ["", f"<b>{block_title}</b>", *block]
    return "\n".join(lines)
