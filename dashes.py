"""Обычный дефис «-» вместо длинного тире «—» (и «–») во всём, что пишет бот, — так хочет владелец.

Чтобы не ловить тире по сотням строк и в ответах ИИ, бот подменяет их на выходе: middleware сессии
правит каждый запрос к Telegram (текст, подписи, опросы, кнопки, алерты, меню команд). Картинки
(карточки, титры, цитаты) — через plain_dashes в их отрисовке.
"""
from __future__ import annotations

from aiogram.client.session.middlewares.base import BaseRequestMiddleware

_DASHES = str.maketrans({"—": "-", "–": "-"})
_TEXT_FIELDS = ("text", "caption", "question", "explanation", "description", "short_description")


def plain_dashes(text):
    return text.translate(_DASHES) if isinstance(text, str) else text


def _fix_obj(obj, field: str):
    """Копия «замороженного» объекта aiogram с поправленным полем (или сам объект, если править нечего)."""
    value = getattr(obj, field, None)
    if isinstance(value, str) and ("—" in value or "–" in value):
        return obj.model_copy(update={field: plain_dashes(value)})
    return obj


def _fix_markup(markup):
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return markup
    new_rows = [[_fix_obj(b, "text") for b in row] for row in rows]
    return markup.model_copy(update={"inline_keyboard": new_rows})


class NoLongDashes(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        try:
            for f in _TEXT_FIELDS:
                value = getattr(method, f, None)
                if isinstance(value, str):
                    setattr(method, f, plain_dashes(value))
            if isinstance(getattr(method, "options", None), list):  # варианты опроса
                method.options = [plain_dashes(o) if isinstance(o, str) else _fix_obj(o, "text")
                                  for o in method.options]
            if isinstance(getattr(method, "media", None), list):  # альбом: подписи у фото
                method.media = [_fix_obj(m, "caption") for m in method.media]
            if isinstance(getattr(method, "commands", None), list):  # меню «/»
                method.commands = [_fix_obj(c, "description") for c in method.commands]
            if getattr(method, "reply_markup", None) is not None:
                method.reply_markup = _fix_markup(method.reply_markup)
        except Exception:  # подмена тире не должна ломать отправку
            pass
        return await make_request(bot, method)
