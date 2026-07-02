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
    /addkey КЛЮЧ ДНИ [TELEGRAM_ID] [заметка]
                                    — добавить ключ в базу, например:
                                    /addkey ZEUS-ABCD-1234 30 123456789 заказ от @username
                                    ДНИ = 0 означает "бессрочно"
                                    TELEGRAM_ID — необязателен: если указан,
                                    бот сам напишет покупателю, что ключ создан,
                                    и покупатель сможет смотреть его через /mykey.
    /delkey КЛЮЧ                 — отозвать (деактивировать) ключ
    /resethwid КЛЮЧ               — сбросить привязку устройства к ключу
    /reply TELEGRAM_ID текст      — ответить пользователю на тикет поддержки от имени бота

Команды для всех пользователей:
    /mykey                        — показать свой ключ, статус, срок действия
    /support ТЕКСТ                — отправить обращение в поддержку (видно только админу)
"""
import asyncio
import json
import logging
import os
import time
from contextlib import closing
from datetime import datetime

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
SELLER_USERNAME = os.environ.get("SELLER_USERNAME", "hopeyng")  # без @, для ссылки на оплату

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
        "Тарифы, статус ключа и скачивание клиента — в мини-приложении ниже.\n\n"
        "Команды:\n"
        "/mykey — посмотреть свой ключ и его статус\n"
        "/support ТЕКСТ — написать в поддержку",
        reply_markup=webapp_keyboard(),
    )


@dp.message(Command("addkey"))
async def add_key(message: Message):
    if not is_admin(message):
        return  # чужие команды тихо игнорируем

    if not DATABASE_URL:
        await message.answer("DATABASE_URL не настроена на этом сервисе, добавить ключ не могу.")
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "Формат: /addkey КЛЮЧ ДНИ [TELEGRAM_ID] [заметка]\n"
            "Пример: /addkey ZEUS-ABCD-1234 30 123456789 заказ от @username\n"
            "ДНИ = 0 означает «бессрочно»\n"
            "TELEGRAM_ID необязателен — если указан, покупателю придёт уведомление "
            "и он сможет смотреть ключ через /mykey"
        )
        return

    key = parts[1].strip().upper()
    raw_days = parts[2]
    rest = parts[3:]

    buyer_id = None
    note = None
    if rest:
        if rest[0].isdigit():
            buyer_id = int(rest[0])
            note = " ".join(rest[1:]) if len(rest) > 1 else None
        else:
            note = " ".join(rest)

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
                                       expires_at, resets_left, note, created_at, telegram_id)
                    VALUES (%s, NULL, 1, 1, 0, %s, 2, %s, %s, %s)
                    """,
                    (key, expires_at, note, int(time.time()), buyer_id),
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
    result_text = f"✅ Ключ добавлен: {key} ({expiry_text})"

    if buyer_id:
        try:
            await bot.send_message(
                buyer_id,
                f"🔑 Ваш ключ создан!\n\n"
                f"Ключ: {key}\n"
                f"Срок действия: {expiry_text}\n\n"
                f"Проверить и активировать можно в мини-приложении (/start) "
                f"или командой /mykey.",
            )
            result_text += "\n📨 Покупатель уведомлён."
        except Exception as e:
            logging.warning("Не удалось уведомить покупателя %s: %s", buyer_id, e)
            result_text += f"\n⚠️ Не удалось уведомить покупателя ({buyer_id}): {e}"

    await message.answer(result_text)


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


@dp.message(Command("mykey"))
async def my_key(message: Message):
    """Пользователь смотрит свой ключ прямо в чате с ботом."""
    if not DATABASE_URL:
        await message.answer("Сервис временно недоступен, попробуйте позже.")
        return

    user_id = message.from_user.id

    try:
        with closing(db()) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM keys WHERE telegram_id = %s ORDER BY created_at DESC LIMIT 1",
                    (user_id,),
                )
                row = cur.fetchone()
    except Exception as e:
        logging.exception("Ошибка при получении ключа пользователя")
        await message.answer("Не удалось получить данные, попробуйте позже.")
        return

    if row is None:
        await message.answer(
            "У вас пока нет привязанного ключа.\n"
            "Приобрести доступ можно в мини-приложении ниже.",
            reply_markup=webapp_keyboard(),
        )
        return

    if not row["active"]:
        status = "🚫 отозван"
    elif row["expires_at"] and row["expires_at"] < time.time():
        status = "⌛ истёк"
    else:
        status = "✅ активен"

    expires_text = (
        "бессрочно" if not row["expires_at"]
        else datetime.fromtimestamp(row["expires_at"]).strftime("%d.%m.%Y")
    )
    hwid_text = "привязано" if row["hwid"] else "не привязано"

    await message.answer(
        f"🔑 Ваш ключ: {row['key']}\n"
        f"Статус: {status}\n"
        f"Действует до: {expires_text}\n"
        f"Устройство: {hwid_text}\n"
        f"Осталось сбросов привязки: {row['resets_left']}\n\n"
        f"Для сброса привязки устройства обратитесь в поддержку — /support"
    )


@dp.message(Command("resethwid"))
async def reset_hwid_cmd(message: Message):
    """Сброс привязки устройства к ключу — доступно только админу."""
    if not is_admin(message):
        return

    if not DATABASE_URL:
        await message.answer("DATABASE_URL не настроена на этом сервисе.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: /resethwid КЛЮЧ")
        return

    key = parts[1].strip().upper()

    try:
        with closing(db()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE keys SET hwid = NULL, activations = 0 WHERE key = %s",
                    (key,),
                )
                conn.commit()
                found = cur.rowcount > 0
    except Exception as e:
        logging.exception("Ошибка при сбросе привязки")
        await message.answer(f"Не удалось сбросить привязку: {e}")
        return

    if found:
        await message.answer(f"✅ Привязка устройства для ключа {key} сброшена.")
    else:
        await message.answer(f"Ключ {key} не найден.")


@dp.message(Command("support"))
async def support(message: Message):
    """Обращение в поддержку — видит только админ, никуда больше не уходит."""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Опишите вопрос: /support ваш текст")
        return

    text = parts[1]
    user = message.from_user
    username = f"@{user.username}" if user.username else "(нет username)"

    await bot.send_message(
        ADMIN_CHAT_ID,
        "🆘 Тикет поддержки\n\n"
        f"От: {user.full_name} {username}\n"
        f"Telegram ID: {user.id}\n\n"
        f"{text}\n\n"
        f"Ответить через бота: /reply {user.id} текст ответа",
    )
    await message.answer("Обращение отправлено в поддержку, вам ответят в этом же чате.")


@dp.message(Command("reply"))
async def reply_to_user(message: Message):
    """Админ отвечает на тикет — сообщение уходит от имени бота."""
    if not is_admin(message):
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Формат: /reply TELEGRAM_ID текст ответа")
        return

    _, raw_id, text = parts
    try:
        target_id = int(raw_id)
    except ValueError:
        await message.answer("TELEGRAM_ID должен быть числом.")
        return

    try:
        await bot.send_message(target_id, f"💬 Ответ поддержки:\n\n{text}")
    except Exception as e:
        await message.answer(f"Не удалось отправить сообщение: {e}")
        return

    await message.answer(f"✅ Ответ отправлен пользователю {target_id}.")


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

    seller_kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=f"Написать @{SELLER_USERNAME}", url=f"https://t.me/{SELLER_USERNAME}")
        ]]
    )

    # Подтверждение покупателю — с прямой ссылкой на продавца
    await message.answer(
        f"Заявка на «{plan_label}» ({price}₽) принята.\n"
        f"Для оплаты и получения ключа напишите продавцу:",
        reply_markup=seller_kb,
    )

    # Заявка админу (для учёта — видно только вам)
    username = f"@{user.username}" if user.username else "(нет username)"
    admin_text = (
        "🛒 Новая заявка\n\n"
        f"Тариф: {plan_label}\n"
        f"Цена: {price}₽\n"
        f"Покупатель: {user.full_name} {username}\n"
        f"Telegram ID: {user.id}\n\n"
        f"Открыть чат: tg://user?id={user.id}\n"
        f"Создать ключ: /addkey КЛЮЧ ДНИ {user.id}"
    )
    await bot.send_message(ADMIN_CHAT_ID, admin_text)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
