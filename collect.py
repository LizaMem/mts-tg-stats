"""Собирает статистику постов Telegram-канала в docs/data.json — его читает дашборд docs/index.html.

Запуск:
    python collect.py
"""

import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import (
    MessageEntityTextUrl,
    MessageEntityUrl,
    ReactionCustomEmoji,
    ReactionEmoji,
    ReactionPaid,
)

def load_dotenv(path=".env"):
    """Локальный запуск: подхватывает переменные из .env. В GitHub Actions файла нет — там Secrets."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    except FileNotFoundError:
        pass


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None or value == "":
        sys.exit(f"Не задана переменная окружения {name}")
    return value


def reaction_label(reaction):
    if isinstance(reaction, ReactionEmoji):
        return reaction.emoticon
    if isinstance(reaction, ReactionCustomEmoji):
        return "custom"
    if isinstance(reaction, ReactionPaid):
        return "⭐"
    return "other"


def reactions_of(msg):
    """Возвращает (сумма, строка вида '❤ 12, 🔥 5')."""
    if not msg.reactions or not msg.reactions.results:
        return 0, ""
    counts = {}
    for rc in msg.reactions.results:
        label = reaction_label(rc.reaction)
        counts[label] = counts.get(label, 0) + rc.count
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    return sum(counts.values()), ", ".join(f"{k} {v}" for k, v in ordered)


def links_of(msg):
    """Все ссылки поста: из текста, из гиперссылок и из кнопок под постом."""
    urls = []
    for entity, text in msg.get_entities_text():
        if isinstance(entity, MessageEntityTextUrl):
            urls.append(entity.url)
        elif isinstance(entity, MessageEntityUrl):
            urls.append(text)
    if msg.reply_markup and getattr(msg.reply_markup, "rows", None):
        for row in msg.reply_markup.rows:
            for button in row.buttons:
                if getattr(button, "url", None):
                    urls.append(button.url)
    return urls


def utm_contents(urls):
    values = []
    for url in urls:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        for value in parse_qs(urlparse(url).query).get("utm_content", []):
            if value not in values:
                values.append(value)
    return ", ".join(values)


async def fetch(days):
    client = TelegramClient(
        StringSession(env("TG_SESSION")), int(env("TG_API_ID")), env("TG_API_HASH")
    )
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("Сессия Telegram недействительна — заново запустите login.py")

    target = env("TG_CHANNEL")
    if target.lstrip("-").isdigit():  # числовой id вида -100123…
        target = int(target)
    channel = await client.get_entity(target)
    username = getattr(channel, "username", None)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    posts = {}  # ключ — пост; альбом из нескольких фото = один пост
    async for msg in client.iter_messages(channel):
        if msg.date < since:
            break
        if msg.views is None:  # служебные сообщения: закрепы, смена названия и т.п.
            continue
        key = msg.grouped_id or msg.id
        total, breakdown = reactions_of(msg)
        urls = links_of(msg)
        post = posts.get(key)
        if post is None:
            posts[key] = post = {
                "post_id": msg.id,
                "date": msg.date.isoformat(timespec="minutes"),
                "text": "",
                "views": msg.views,
                "forwards": msg.forwards or 0,
                "comments": msg.replies.replies if msg.replies else 0,
                "reactions_total": total,
                "reactions": breakdown,
                "urls": urls,
            }
        else:
            # В альбоме статистика бывает только на одном из сообщений — берём максимум,
            # а id поста — самого раннего сообщения (на него ведёт ссылка t.me).
            post["post_id"] = min(post["post_id"], msg.id)
            post["views"] = max(post["views"], msg.views)
            post["forwards"] = max(post["forwards"], msg.forwards or 0)
            if msg.replies:
                post["comments"] = max(post["comments"], msg.replies.replies)
            if total > post["reactions_total"]:
                post["reactions_total"], post["reactions"] = total, breakdown
            post["urls"] += urls
        if msg.message and not post["text"]:
            post["text"] = msg.message.replace("\n", " ")[:200]

    full = await client(GetFullChannelRequest(channel))
    subscribers = full.full_chat.participants_count
    await client.disconnect()

    rows = []
    for post in sorted(posts.values(), key=lambda p: p["post_id"], reverse=True):
        urls = post.pop("urls")
        pid = post["post_id"]
        if username:
            link = f"https://t.me/{username}/{pid}"
        else:  # приватный канал
            link = f"https://t.me/c/{channel.id}/{pid}"
        rows.append({
            **post,
            "link": link,
            "utm_content": utm_contents(urls),
            "links": list(dict.fromkeys(urls)),
        })
    return channel.title, rows, subscribers


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"posts": [], "history": {}, "subscribers": {}}


def save(path, title, fresh, subscribers):
    """Сливает свежие цифры с уже накопленными в data.json.

    Посты старше DAYS_BACK больше не обновляются, но остаются в файле с последними цифрами.
    history — срез по дням: {"2026-10-14": {"123": [просмотры, пересылы, комменты, реакции]}}.
    Повторный запуск в тот же день перезаписывает сегодняшний срез, а не дублирует его.
    """
    data = load(path)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    posts = {p["post_id"]: p for p in data["posts"]}
    posts.update({p["post_id"]: p for p in fresh})
    data["posts"] = sorted(posts.values(), key=lambda p: p["post_id"], reverse=True)
    data["history"][today] = {
        str(p["post_id"]): [p["views"], p["forwards"], p["comments"], p["reactions_total"]]
        for p in fresh
    }
    data["subscribers"][today] = subscribers
    data["channel"] = title
    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    write_csv(os.path.dirname(path), data)
    print(f"Готово: {len(fresh)} постов обновлено, всего в {path}: {len(data['posts'])}, "
          f"подписчиков: {subscribers}")


MINSK = timezone(timedelta(hours=3))


def write_csv(folder, data):
    """CSV рядом с дашбордом: Google Таблица подтягивает их формулой IMPORTDATA."""
    def dump(name, header, rows):
        with open(os.path.join(folder, name), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    def local(iso):
        return datetime.fromisoformat(iso).astimezone(MINSK).strftime("%Y-%m-%d %H:%M")

    dump("posts.csv",
         ["post_id", "date", "text", "views", "reactions", "forwards", "comments",
          "engagement", "reactions_detail", "utm_content", "link"],
         [[p["post_id"], local(p["date"]), p["text"], p["views"], p["reactions_total"],
           p["forwards"], p["comments"],
           round((p["reactions_total"] + p["forwards"] + p["comments"]) / p["views"], 4)
           if p["views"] else 0,
           p["reactions"], p["utm_content"], p["link"]] for p in data["posts"]])
    dump("subscribers.csv", ["date", "subscribers"], sorted(data["subscribers"].items()))
    dump("history.csv", ["date", "post_id", "views", "forwards", "comments", "reactions"],
         [[day, pid, *vals] for day, posts in sorted(data["history"].items())
          for pid, vals in posts.items()])


def main():
    load_dotenv()
    days = int(os.environ.get("DAYS_BACK") or 60)
    title, posts, subscribers = asyncio.run(fetch(days))
    save(os.environ.get("DATA_PATH") or "docs/data.json", title, posts, subscribers)


if __name__ == "__main__":
    main()
