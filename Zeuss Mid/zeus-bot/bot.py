"""
Zaetheron Industry — Telegram-бот.
Открывает Mini App и пересылает заявки на покупку админу в личку.

Переменные окружения:
    BOT_TOKEN        — токен бота от @BotFather
    ADMIN_CHAT_ID    — твой Telegram ID (числом), куда падают заявки на покупку
    WEBAPP_URL       — https-ссылка на мини-апп, например https://your-app.up.railway.app/app
    DATABASE_URL     — та же Postgres-БД, что использует key_server.py (нужна для /addkey и /delkey)

Запуск локально:
    pip install aiogram psycopg2-binary
    BOT_TOKEN=... ADMIN_CHAT_ID=... WEBAPP_URL=... DATABASE_URL=... python bot.py

Админ-команды (доступны только ADMIN_CHAT_ID):
    /addkey КЛЮЧ ДНИ [заметка]   — добавить ключ в базу, например:
                                    /addkey ZEUS-ABCD-1234 30 заказ от @username
                                    ДНИ = 0 означает "бессрочно"
    /delkey КЛЮЧ                 — отозвать (деактивировать) ключ
"""
import asyncio
import json
import logging
import os
import time
from contextlib import closing

import psycopg2
import psycopg2.extras
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
WEBAPP_URL = os.environ["WEBAPP_URL"]
DATABASE_URL = os.environ.get("DATABASE_URL")  # нужна только для /addkey и /delkey

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def db():
    return psycopg2.connect(DATABASE_URL)


def is_admin(message: Message) -> bool:
    return message.from_user.id == ADMIN_CHAT_ID


def webapp_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Открыть Zaetheron Industry", web_app=WebAppInfo(url=WEBAPP_URL))
        ]]
    )


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Zaetheron Industry\n\n"
        "Тарифы, статус ключа и скачивание клиента — в мини-приложении ниже.",
        reply_markup=webapp_keyboard(),
    )


@dp.message(Command("addkey"))
async def add_key(message: Message):
    if not is_admin(message):
        return  # чужие команды тихо игнорируем

    if not DATABASE_URL:
        await message.answer("DATABASE_URL не настроена на этом сервисе, добавить ключ не могу.")
        return

    parts = message.text.split(maxsplit=3)
    if len(parts) < 3:
        await message.answer(
            "Формат: /addkey КЛЮЧ ДНИ [заметка]\n"
            "Пример: /addkey ZEUS-ABCD-1234 30 заказ от @username\n"
            "ДНИ = 0 означает «бессрочно»"
        )
        return

    _, raw_key, raw_days, *rest = parts
    key = raw_key.strip().upper()
    note = rest[0] if rest else None

    try:
        days = int(raw_days)
    except ValueError:
        await message.answer("ДНИ должно быть числом (0 = бессрочно).")
        return

    expires_at = int(time.time()) + days * 86400 if days > 0 else None

    try:
        with closing(db()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO keys (key, hwid, active, max_activations, activations,
                                       expires_at, resets_left, note, created_at)
                    VALUES (%s, NULL, 1, 1, 0, %s, 2, %s, %s)
                    """,
                    (key, expires_at, note, int(time.time())),
                )
                conn.commit()
    except psycopg2.errors.UniqueViolation:
        await message.answer(f"Ключ {key} уже существует в базе.")
        return
    except Exception as e:
        logging.exception("Ошибка при добавлении ключа")
        await message.answer(f"Не удалось добавить ключ: {e}")
        return

    expiry_text = "бессрочный" if days == 0 else f"на {days} дн."
    await message.answer(f"✅ Ключ добавлен: {key} ({expiry_text})")


@dp.message(Command("delkey"))
async def del_key(message: Message):
    if not is_admin(message):
        return

    if not DATABASE_URL:
        await message.answer("DATABASE_URL не настроена на этом сервисе.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: /delkey КЛЮЧ")
        return

    key = parts[1].strip().upper()

    try:
        with closing(db()) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE keys SET active = 0 WHERE key = %s", (key,))
                conn.commit()
                found = cur.rowcount > 0
    except Exception as e:
        logging.exception("Ошибка при отзыве ключа")
        await message.answer(f"Не удалось отозвать ключ: {e}")
        return

    if found:
        await message.answer(f"🚫 Ключ {key} отозван.")
    else:
        await message.answer(f"Ключ {key} не найден.")


@dp.message(F.web_app_data)
async def handle_webapp_data(message: Message):
    """Ловит данные, отправленные из мини-аппа через Telegram.WebApp.sendData(...)"""
    try:
        payload = json.loads(message.web_app_data.data)
    except (ValueError, AttributeError):
        await message.answer("Не удалось разобрать запрос, попробуйте ещё раз.")
        return

    if payload.get("action") != "buy":
        return

    user = message.from_user
    plan_label = payload.get("label", payload.get("plan", "неизвестный тариф"))
    price = payload.get("price", "?")

    # Подтверждение покупателю
    await message.answer(
        f"Заявка на «{plan_label}» ({price}₽) отправлена. "
        f"Продавец свяжется с вами здесь для оплаты и выдачи ключа."
    )

    # Заявка админу
    username = f"@{user.username}" if user.username else "(нет username)"
    admin_text = (
        "🛒 Новая заявка\n\n"
        f"Тариф: {plan_label}\n"
        f"Цена: {price}₽\n"
        f"Покупатель: {user.full_name} {username}\n"
        f"Telegram ID: {user.id}\n\n"
        f"Открыть чат: tg://user?id={user.id}"
    )
    await bot.send_message(ADMIN_CHAT_ID, admin_text)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
