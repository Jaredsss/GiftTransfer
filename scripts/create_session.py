"""Создаёт строку сессии Telethon для аккаунта-сканера.

Запуск: python -m scripts.create_session
Полученную строку вставьте в .env как TG_SESSION. Никому её не передавайте —
это полный доступ к аккаунту. Лучше использовать отдельный аккаунт.
"""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from upgrade_gift import config

api_id = config.TG_API_ID or int(input("api_id: "))
api_hash = config.TG_API_HASH or input("api_hash: ")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nTG_SESSION=" + client.session.save())
