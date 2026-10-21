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

# --- HTTPS для мини-аппа: свой Caddy, если 80/443 свободны, иначе через nginx ---
DOMAIN=$(get_env WEBAPP_URL | sed -e 's|^https\?://||' -e 's|/.*||')
port_owner() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'users:(("[^"]*' | head -1 | cut -d'"' -f2; }

PROXY_MODE=$(get_env PROXY_MODE)
if [ -z "$PROXY_MODE" ]; then
  docker compose --profile caddy rm -sf caddy >/dev/null 2>&1 || true
  p80=$(port_owner 80); p443=$(port_owner 443)
  if [ -z "$p80" ] && [ -z "$p443" ]; then
    PROXY_MODE=caddy
  elif [ "${p80:-nginx}" = nginx ] && [ "${p443:-nginx}" = nginx ]; then
    PROXY_MODE=nginx
  else
    echo "Порты заняты: 80 -> ${p80:-свободен}, 443 -> ${p443:-свободен}" >&2
    ss -ltnp 'sport = :80 or sport = :443' >&2
    echo "Освободите порты или пришлите этот вывод." >&2
    exit 1
  fi
  set_env PROXY_MODE "$PROXY_MODE"
fi

if [ "$PROXY_MODE" = nginx ]; then
  echo "==> Настраиваю nginx для $DOMAIN"
  backup=/etc/nginx/upgrade-gift-backup
  # Старые конфиги этого домена (например, от прошлого бота) отключаем, копия — в $backup
  for f in $(grep -rlE "server_name[^;]*[[:space:]]$DOMAIN[[:space:];]" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null); do
    [ "$(basename "$f")" = "upgrade-gift.conf" ] && continue
    others=$(grep -hoE "server_name[^;]*" "$f" | sed 's/server_name//' | tr -s ' \t' '\n' \
      | grep -vxE "|_|$DOMAIN|www\.$DOMAIN" || true)
    if [ -n "$others" ]; then
      echo "Конфиг $f обслуживает и другие сайты ($others) — уберите из него $DOMAIN вручную и запустите снова." >&2
      exit 1
    fi
    mkdir -p "$backup"
    cp -L "$f" "$backup/$(basename "$f")"
    rm -f "$f"
    echo "   отключён старый конфиг $f (копия в $backup)"
  done
  sed "s/jsqvpn.online/$DOMAIN/" deploy/nginx.conf > /etc/nginx/conf.d/upgrade-gift.conf
  nginx -t
  systemctl reload nginx
  if ! command -v certbot >/dev/null 2>&1; then
    apt-get install -y certbot python3-certbot-nginx
  fi
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email \
    --keep-until-expiring --redirect
  docker compose up -d --remove-orphans
else
  docker compose --profile caddy up -d --remove-orphans
fi

sleep 5
docker compose --profile caddy ps
echo
echo "Готово. Мини-апп: https://$DOMAIN"
echo "Логи: cd $PWD && docker compose logs -f bot"
