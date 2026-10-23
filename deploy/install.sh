#!/usr/bin/env bash
# Установка / обновление Rocket. Запуск из папки проекта:
#   BOT_TOKEN=... ADMIN_IDS=... bash deploy/install.sh
# Повторный запуск без переменных — обновит и перезапустит бота.
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
  if grep -q "^$1=" .env; then
    sed -i "s|^$1=.*|$1=$2|" .env
  else
    echo "$1=$2" >> .env
  fi
}
get_env() { grep "^$1=" .env | head -1 | cut -d= -f2-; }

for key in BOT_TOKEN ADMIN_IDS API_BASE STAR_PRICE_USD GRAM_PRICE_USD; do
  if [ -n "${!key:-}" ]; then set_env "$key" "${!key}"; fi
done

if [ -z "$(get_env ADMIN_IDS)" ] && [ -t 0 ]; then
  read -rp "Ваш ID в OceanGram для админ-команд (Enter — пропустить): " admin
  [ -n "$admin" ] && set_env ADMIN_IDS "$admin"
fi
[ -n "$(get_env BOT_TOKEN)" ] || { echo "В .env не заполнен BOT_TOKEN" >&2; exit 1; }

mkdir -p data
docker compose up -d --build
sleep 5
docker compose ps
echo
echo "Готово. Логи: cd $PWD && docker compose logs -f rocket"
