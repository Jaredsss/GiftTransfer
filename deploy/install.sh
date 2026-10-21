#!/usr/bin/env bash
# Установка / обновление Upgrade Gift. Запускать из папки проекта (например /opt/gift):
#   BOT_TOKEN=... XROCKET_API_KEY=... TG_API_ID=... TG_API_HASH=... bash deploy/install.sh
# Повторный запуск без переменных — просто обновит и перезапустит бота.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от root (sudo)." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Устанавливаю Docker"
  curl -fsSL https://get.docker.com | sh
fi

[ -f .env ] || cp .env.example .env

set_env() {  # set_env KEY VALUE — заменяет или добавляет строку в .env
  local key="$1" value="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${value}|" .env
  else
    echo "${key}=${value}" >> .env
  fi
}
get_env() { grep "^$1=" .env | head -1 | cut -d= -f2-; }

for key in BOT_TOKEN XROCKET_API_KEY TG_API_ID TG_API_HASH ADMIN_IDS WEBAPP_URL; do
  if [ -n "${!key:-}" ]; then set_env "$key" "${!key}"; fi
done

if [ -z "$(get_env ADMIN_IDS)" ] && [ -t 0 ]; then
  read -rp "Ваш Telegram ID для админ-команд /grant и /stats (Enter — пропустить): " admin
  [ -n "$admin" ] && set_env ADMIN_IDS "$admin"
fi

for key in BOT_TOKEN XROCKET_API_KEY TG_API_ID TG_API_HASH; do
  if [ -z "$(get_env "$key")" ]; then
    echo "В .env не заполнен $key" >&2
    exit 1
  fi
done

mkdir -p data
echo "==> Собираю образ"
docker compose build

if [ -z "$(get_env TG_SESSION)" ]; then
  echo "==> Вход в аккаунт-сканер (через него бот видит улучшения подарков)"
  docker compose run --rm -v "$PWD/.env:/app/.env" bot python -m scripts.create_session /app/.env
fi

echo "==> Запускаю"
docker compose up -d
sleep 5
docker compose ps
echo
echo "Готово. Логи: cd $PWD && docker compose logs -f bot"
