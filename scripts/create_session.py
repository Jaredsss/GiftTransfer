"""Создаёт строку сессии Telethon для аккаунта-сканера.

Запуск: python -m scripts.create_session [путь к .env]
Если файл .env найден, строка сразу записывается в него как TG_SESSION.
Никому её не передавайте — это полный доступ к аккаунту.
"""
import sys

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from upgrade_gift import config


def write_env(path: str, key: str, value: str) -> None:
    # Пишем в тот же файл, без переименования: .env примонтирован в контейнер
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    lines = [line for line in lines if not line.startswith(key + "=")]
    lines.append(f"{key}={value}")
    with open(path, "r+", encoding="utf-8") as f:
        f.seek(0)
        f.write("\n".join(lines) + "\n")
        f.truncate()


env_path = sys.argv[1] if len(sys.argv) > 1 else ".env"
api_id = config.TG_API_ID or int(input("api_id: "))
api_hash = config.TG_API_HASH or input("api_hash: ")

print("Вход в аккаунт-сканер: введите номер телефона, код из Telegram и пароль 2FA, если есть.")
with TelegramClient(StringSession(), api_id, api_hash) as client:
    session = client.session.save()

try:
    write_env(env_path, "TG_SESSION", session)
    print(f"\nГотово: TG_SESSION записана в {env_path}")
except OSError as e:
    print(f"\nНе удалось записать {env_path} ({e}). Добавьте строку в .env вручную:")
    print("TG_SESSION=" + session)
