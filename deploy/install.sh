#!/usr/bin/env bash
# Установка BunkerBot на чистый сервер Ubuntu 22.04/24.04 (или Debian 12) одной командой:
#   curl -fsSL https://raw.githubusercontent.com/PopiBoom/BunkerBot/main/deploy/install.sh | sudo bash
# Что делает: ставит Python и git, скачивает бота, спрашивает токены, создаёт службу
# с автозапуском и перезапуском при сбое и включает автообновление из GitHub (раз в 2 минуты).
set -euo pipefail

REPO="${REPO:-https://github.com/PopiBoom/BunkerBot.git}"
BRANCH="${BRANCH:-main}"
DIR="${DIR:-/opt/bunkerbot}"
SVC_USER="bunkerbot"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запусти от root: sudo bash install.sh (или добавь sudo перед командой)"; exit 1
fi

echo "==> Ставлю Python и git…"
apt-get update -y -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip git >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Нужен Python 3.10+. Возьми сервер с Ubuntu 22.04 или 24.04."; exit 1; }

id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SVC_USER"

echo "==> Скачиваю бота (ветка $BRANCH)…"
if [ -d "$DIR/.git" ]; then
  git -c safe.directory="$DIR" -C "$DIR" fetch -q origin "$BRANCH"
  git -c safe.directory="$DIR" -C "$DIR" checkout -q -B "$BRANCH" "origin/$BRANCH"
else
  git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi

echo "==> Ставлю зависимости (пара минут)…"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

if [ ! -f "$DIR/.env" ]; then
  echo
  echo "==> Настройка. Вставляй значения и жми Enter."
  ask() { local v; read -r -p "$1: " v </dev/tty; printf '%s' "$v"; }
  TOKEN=$(ask "BOT_TOKEN (токен от @BotFather)")
  ADMINS=$(ask "ADMIN_IDS (твой Telegram ID, можно несколько через пробел)")
  CHATS=$(ask "REQUIRED_CHATS (@канал и/или -100… id чата через пробел)")
  AKEY=$(ask "ANTHROPIC_API_KEY (ключ Claude; можно оставить пустым)")
  cat > "$DIR/.env" <<ENV
BOT_TOKEN=$TOKEN
ADMIN_IDS=$ADMINS
REQUIRED_CHATS=$CHATS
ANTHROPIC_API_KEY=$AKEY
AI_MODEL=claude-opus-5-5
AI_MODEL_FAST=claude-haiku-5-5
AI_MODEL_EDIT=claude-sonnet-5-5
DB_PATH=bot.db
ENV
  chmod 600 "$DIR/.env"
else
  echo "==> .env уже есть — оставляю как есть."
fi

chmod +x "$DIR/deploy/update.sh"
chown -R "$SVC_USER:$SVC_USER" "$DIR"

echo "==> Создаю службы…"
cat > /etc/systemd/system/bunkerbot.service <<UNIT
[Unit]
Description=BunkerBot (Telegram)
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/bunkerbot-update.service <<UNIT
[Unit]
Description=BunkerBot: обновление из GitHub

[Service]
Type=oneshot
ExecStart=$DIR/deploy/update.sh
UNIT

cat > /etc/systemd/system/bunkerbot-update.timer <<UNIT
[Unit]
Description=BunkerBot: проверять обновления каждые 2 минуты

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable -q --now bunkerbot.service bunkerbot-update.timer
sleep 3

echo
echo "✅ Готово! Бот запущен и будет сам обновляться из GitHub (ветка $BRANCH)."
echo
echo "Полезное:"
echo "  статус:       systemctl status bunkerbot"
echo "  логи:         journalctl -u bunkerbot -f"
echo "  перезапуск:   systemctl restart bunkerbot"
echo "  настройки:    nano $DIR/.env  (потом перезапуск)"
echo
echo "Чтобы перенести досье и всё остальное: пришли боту в личку файл бэкапа (*.db)."
