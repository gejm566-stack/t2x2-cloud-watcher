#!/usr/bin/env bash
set -euo pipefail

# Установка 24/7 Telegram-бота на бесплатную VM Google Cloud (e2-micro, Ubuntu/Debian)
# Использование:
#   chmod +x setup_gcp_e2micro.sh
#   ./setup_gcp_e2micro.sh

APP_DIR="/opt/t2x2_bot"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Установка 24/7 бота в $APP_DIR ==="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

sudo mkdir -p "$APP_DIR"
sudo cp "$SCRIPT_DIR/cloud_watcher.py" "$APP_DIR/"
sudo cp "$SCRIPT_DIR/telegram_bot.py" "$APP_DIR/"
sudo cp "$SCRIPT_DIR/processor_template.py" "$APP_DIR/"
sudo cp "$SCRIPT_DIR/requirements.txt" "$APP_DIR/"

if [ ! -d "$APP_DIR/venv" ]; then
  sudo python3 -m venv "$APP_DIR/venv"
fi
sudo "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  read -r -p "TELEGRAM_BOT_TOKEN: " TG_TOKEN
  read -r -p "TELEGRAM_CHAT_ID: " TG_CHAT
  read -r -p "KAGGLE_API_TOKEN (KGAT_...): " KG_TOKEN
  sudo tee "$APP_DIR/.env" >/dev/null <<EOF
TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_CHAT_ID=${TG_CHAT}
KAGGLE_API_TOKEN=${KG_TOKEN}
STREAMER_CHANNEL=t2x2
KAGGLE_KERNEL_ID=istfarmen/t2x2-clips-processor
KAGGLE_DATASET_ID=istfarmen/t2x2-clips-bundle
AUTO_START=true
AUTO_MODE=both
INTERVAL_MINUTES=30
AUTO_UPLOAD=true
CHECK_INTERVAL_SECONDS=120
EOF
  sudo chmod 600 "$APP_DIR/.env"
fi

sudo cp "$SCRIPT_DIR/t2x2-bot.service" /etc/systemd/system/t2x2-bot.service
sudo systemctl daemon-reload
sudo systemctl enable t2x2-bot.service
sudo systemctl restart t2x2-bot.service

echo "✅ Готово! Бот работает 24/7. Проверка логов:"
echo "   sudo systemctl status t2x2-bot"
echo "   journalctl -u t2x2-bot -f"
