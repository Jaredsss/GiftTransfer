"""Создаёт строку сессии Telethon для аккаунта-сканера.

Запуск: python -m scripts.create_session [путь к .env]
Если файл .env найден, строка сразу записывается в него как TG_SESSION.
Никому её не передавайте — это полный доступ к аккаунту.
"""
import os
import sys

from dotenv import set_key
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from upgrade_gift import config

env_path = sys.argv[1] if len(sys.argv) > 1 else ".env"
api_id = config.TG_API_ID or int(input("api_id: "))
api_hash = config.TG_API_HASH or input("api_hash: ")

print("Вход в аккаунт-сканер: введите номер телефона, код из Telegram и пароль 2FA, если есть.")
with TelegramClient(StringSession(), api_id, api_hash) as client:
    session = client.session.save()

if os.path.exists(env_path):
    set_key(env_path, "TG_SESSION", session, quote_mode="never")
    print(f"\nГотово: TG_SESSION записана в {env_path}")
else:
    print("\nTG_SESSION=" + session)
