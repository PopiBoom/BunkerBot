#!/usr/bin/env bash
# Автообновление: если в GitHub появились новые коммиты — забрать их и перезапустить бота.
# Запускается таймером bunkerbot-update.timer раз в 2 минуты. .env и bot.db не трогает.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
G=(git -c safe.directory="$DIR" -C "$DIR")
BRANCH="$("${G[@]}" rev-parse --abbrev-ref HEAD)"

"${G[@]}" fetch -q origin "$BRANCH"
LOCAL="$("${G[@]}" rev-parse HEAD)"
REMOTE="$("${G[@]}" rev-parse "origin/$BRANCH")"
[ "$LOCAL" = "$REMOTE" ] && exit 0

# Идёт игра (бот держит файл-флаг свежим) — не перезапускаемся, иначе партия оборвётся.
# Флаг старше 2 минут — бот не работает или игр нет, тогда обновляемся.
FLAG="$DIR/.game_running"
if [ -f "$FLAG" ] && [ $(( $(date +%s) - $(stat -c %Y "$FLAG") )) -lt 120 ]; then
  echo "Идёт игра — обновлюсь до ${REMOTE:0:7} после партии"
  exit 0
fi

OLD_REQ="$("${G[@]}" show HEAD:requirements.txt | sha1sum)"
"${G[@]}" reset -q --hard "origin/$BRANCH"
NEW_REQ="$(sha1sum < "$DIR/requirements.txt")"
if [ "$OLD_REQ" != "$NEW_REQ" ]; then
  # Если пакет не встал — всё равно перезапускаемся: код умеет жить без необязательных пакетов,
  # а без перезапуска бот застрял бы на старой версии (следующий запуск увидит, что код уже свежий).
  "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt" \
    || logger -t bunkerbot "pip install не удался — перезапускаю без новых пакетов"
fi

chmod +x "$DIR/deploy/update.sh"
chown -R bunkerbot:bunkerbot "$DIR"
systemctl restart bunkerbot
logger -t bunkerbot "обновлён до ${REMOTE:0:7}"
echo "Обновлён: ${LOCAL:0:7} -> ${REMOTE:0:7}"
