"""Запускается ОДИН раз на своём компьютере: логинится в Telegram и печатает строку сессии.

    pip install -r requirements.txt
    python login.py

Спросит api_id, api_hash, номер телефона, код из Telegram и облачный пароль (если включён).
ВНИМАНИЕ: строка сессии = полный доступ к аккаунту. Только в GitHub Secrets, никому не пересылать.
"""

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("api_id: ").strip())
api_hash = input("api_hash: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    session = client.session.save()
    me = client.get_me()

print(f"\nВход выполнен как {me.first_name} (@{me.username}).")
print("Строка сессии — положите её в секрет TG_SESSION:\n")
print(session)
