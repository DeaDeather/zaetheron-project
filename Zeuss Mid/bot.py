# file: bot.py
"""
Zaetheron Industry — Telegram-бот.
Выдаёт ключи и отвечает на команды. Оплату принимает Telegram Payments:
счёт выставляет key_server.py (мини-апп → /buy), а сюда приходит подтверждение
оплаты, и бот сам выдаёт ключ покупателю.

Переменные окружения:
    BOT_TOKEN        — токен бота от @BotFather
    ADMIN_CHAT_ID    — твой Telegram ID (числом), куда падают уведомления
    WEBAPP_URL       — https-ссылка на мини-апп, например https://your-app.up.railway.app/app
    DATABASE_URL     — та же Postgres-БД, что использует key_server.py
    KEY_SECRET       — секрет шифрования ключей, тот же, что у key_server.py. После запуска не меняй!

Платёжный токен (PAYMENT_PROVIDER_TOKEN) задаётся только в key_server.py, боту он не нужен.

Файлы bot.py, keyvault.py и key_server.py должны лежать в одной папке.

Запуск локально (Windows):
    py -m pip install aiogram psycopg2-binary cryptography
    py bot.py

Админ-команды (доступны только ADMIN_CHAT_ID, только в личке):
    /addkey 1d|3d|7d [app|os] [TELEGRAM_ID] [заметка]
                                  — создать ключ ZTH-XXXX-XXXX-XXXX.
                                    app — приложение (по умолчанию), os — Custom OS.
                                    Ключ придумывает сервер, вручную его задать нельзя.
                                    Максимальный срок — 7 дней.
                                    Если указан TELEGRAM_ID, покупатель получит ключ в личку.
    /delkey КЛЮЧ                  — отозвать (деактивировать) ключ
    /resethwid КЛЮЧ               — сбросить привязку устройства к ключу
    /reply TELEGRAM_ID текст      — ответить пользователю на тикет поддержки от имени бота

Команды для всех пользователей:
    /start                        — открыть мини-апп
    /mykey                        — показать свой ключ, статус, срок действия
    /support ТЕКСТ                — отправить обращение в поддержку (видно только админу)
"""
import asyncio
import logging
import os
import re
import time
from contextlib import closing
from datetime import datetime

import psycopg2
import psycopg2.extras
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    PreCheckoutQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    FSInputFile,
)

import keyvault

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
WEBAPP_URL = os.environ["WEBAPP_URL"]
DATABASE_URL = os.environ.get("DATABASE_URL")  # нужна для ключей и платежей

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

ALLOWED_DAYS = tuple(keyvault.PLAN_DAYS.values())  # 1, 3, 7
KEY_CLEANUP_INTERVAL = 300  # раз в 5 минут удаляем истёкшие ключи из базы

BANNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zeus_bg_01.jpg")
WELCOME_TEXT = (
    "Zaetheron Industry\n\n"
    "Тарифы, статус ключа и скачивание — в мини-приложении ниже.\n\n"
    "Команды:\n"
    "/mykey — посмотреть свой ключ и его статус\n"
    "/support ТЕКСТ — написать в поддержку"
)


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
    if os.path.exists(BANNER_PATH):
        await message.answer_photo(
            FSInputFile(BANNER_PATH), caption=WELCOME_TEXT, reply_markup=webapp_keyboard()
        )
    else:
        await message.answer(WELCOME_TEXT, reply_markup=webapp_keyboard())


def ensure_schema():
    """Запускается один раз при старте.
    key_history — никогда не чистим, она гарантирует уникальность ключей.
    keys.key_enc и keys.product — колонки, которых раньше не было.
    payments — журнал оплат: по нему не выдаём ключ за один платёж дважды."""
    migrated = 0
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS key_history (
                    key_hash  TEXT PRIMARY KEY,
                    issued_at BIGINT NOT NULL
                )
                """
            )
            cur.execute("ALTER TABLE keys ADD COLUMN IF NOT EXISTS key_enc TEXT")
            cur.execute("ALTER TABLE keys ADD COLUMN IF NOT EXISTS product TEXT DEFAULT 'app'")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    charge_id   TEXT PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    product     TEXT NOT NULL,
                    plan        TEXT NOT NULL,
                    amount      INTEGER NOT NULL,
                    currency    TEXT NOT NULL,
                    created_at  BIGINT NOT NULL
                )
                """
            )
            # строки, где ключ ещё лежит открытым текстом: key_enc пустой
            cur.execute("SELECT key FROM keys WHERE key_enc IS NULL")
            legacy_keys = [r[0] for r in cur.fetchall()]
            for plain in legacy_keys:
                key_hash = keyvault.lookup_hash(plain)
                cur.execute(
                    "UPDATE keys SET key = %s, key_enc = %s WHERE key = %s",
                    (key_hash, keyvault.encrypt_key(plain), plain),
                )
                cur.execute(
                    "INSERT INTO key_history (key_hash, issued_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (key_hash, int(time.time())),
                )
                migrated += 1
        conn.commit()
    if migrated:
        logging.info("Зашифровано старых ключей: %s", migrated)


def _insert_new_key(cur, days: int, buyer_id, note, product: str, now: int):
    """Внутри открытой транзакции: придумывает ключ, пишет его хэш в key_history
    и строку в keys. Возвращает (ключ, expires_at).

    Хэш в key_history не удаляется никогда, поэтому ключ не повторится,
    даже если его строка в keys уже удалена после истечения срока."""
    expires_at = now + days * 86400
    for _ in range(20):
        plain = keyvault.generate_plain_key()
        key_hash = keyvault.lookup_hash(plain)
        cur.execute(
            "INSERT INTO key_history (key_hash, issued_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (key_hash, now),
        )
        if cur.rowcount == 0:
            continue  # такой ключ уже выдавали раньше, берём другой
        cur.execute(
            """
            INSERT INTO keys (key, key_enc, hwid, active, max_activations, activations,
                              expires_at, resets_left, note, created_at, telegram_id, product)
            VALUES (%s, %s, NULL, 1, 1, 0, %s, 2, %s, %s, %s, %s)
            """,
            (key_hash, keyvault.encrypt_key(plain), expires_at, note, now, buyer_id, product),
        )
        return plain, expires_at
    raise RuntimeError("Не удалось подобрать свободный ключ, попробуй ещё раз")


def issue_key(days: int, buyer_id, note, product: str = "app"):
    """Выдаёт ключ по команде /addkey. Возвращает (ключ, expires_at)."""
    now = int(time.time())
    with closing(db()) as conn:
        try:
            with conn.cursor() as cur:
                result = _insert_new_key(cur, days, buyer_id, note, product, now)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def fulfill_payment(charge_id, buyer_id, product, plan, amount, currency):
    """Записывает платёж и выдаёт ключ в одной транзакции.
    Повторный вызов с тем же charge_id ничего не выдаёт.
    Возвращает (ключ, expires_at) или None, если платёж уже обработан."""
    now = int(time.time())
    days = keyvault.PLAN_DAYS[plan]
    with closing(db()) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO payments (charge_id, telegram_id, product, plan, amount, currency, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (charge_id) DO NOTHING
                    """,
                    (charge_id, buyer_id, product, plan, amount, currency, now),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return None
                result = _insert_new_key(cur, days, buyer_id, f"оплата {charge_id}", product, now)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


@dp.message(Command("addkey"))
async def add_key(message: Message):
    if not is_admin(message):
        return  # чужие команды тихо игнорируем

    if message.chat.type != "private":
        await message.answer("Выполняй /addkey только в личке с ботом, не в группе.")
        return

    if not DATABASE_URL:
        await message.answer("DATABASE_URL не настроена на этом сервисе, создать ключ не могу.")
        return

    usage = (
        "Формат: /addkey 1d|3d|7d [app|os] [TELEGRAM_ID] [заметка]\n"
        "app — приложение (по умолчанию), os — Custom OS.\n"
        "Ключ придумывает сервер. Максимальный срок — 7 дней.\n"
        "TELEGRAM_ID необязателен: если указан, покупатель получит ключ в личку."
    )
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(usage)
        return

    match = re.fullmatch(r"(\d+)d", parts[1].strip().lower())
    if match is None:
        await message.answer(usage)
        return
    days = int(match.group(1))
    if days not in ALLOWED_DAYS:
        await message.answer("Доступные сроки: 1d, 3d, 7d. Больше 7 дней не выдаю.")
        return

    rest = parts[2:]
    product = "app"
    if rest and rest[0].lower() in keyvault.PRODUCTS:
        product = rest.pop(0).lower()

    buyer_id = None
    note = None
    if rest:
        if rest[0].isdigit():
            buyer_id = int(rest[0])
            note = " ".join(rest[1:]) or None
        else:
            note = " ".join(rest)

    title = keyvault.PRODUCTS[product]["title"]
    try:
        key, expires_at = await asyncio.to_thread(issue_key, days, buyer_id, note, product)
    except Exception as e:
        logging.exception("Ошибка при создании ключа")
        await message.answer(f"Не удалось создать ключ: {e}")
        return

    expires_text = datetime.fromtimestamp(expires_at).strftime("%d.%m.%Y %H:%M")
    result_text = (
        f"✅ Ключ создан: {key}\n"
        f"Продукт: {title}\n"
        f"Срок: {days} дн.\n"
        f"Удалится из базы после {expires_text}"
    )

    if buyer_id:
        try:
            await bot.send_message(
                buyer_id,
                "🔑 Ваш ключ создан!\n\n"
                f"Продукт: {title}\n"
                f"Ключ: {key}\n"
                f"Срок действия: {days} дн.\n\n"
                "Активировать можно в мини-приложении (/start) или командой /mykey.",
            )
            result_text += "\n📨 Покупатель уведомлён."
        except Exception as e:
            logging.warning("Не удалось уведомить покупателя %s: %s", buyer_id, e)
            result_text += f"\n⚠️ Не удалось уведомить покупателя ({buyer_id}): {e}"

    await message.answer(result_text)


def delete_expired() -> int:
    if not DATABASE_URL:
        return 0
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM keys WHERE expires_at IS NOT NULL AND expires_at < %s",
                (int(time.time()),),
            )
            conn.commit()
            return cur.rowcount


async def cleanup_expired_loop():
    """Периодически удаляет истёкшие ключи из базы. key_history при этом не трогаем."""
    while True:
        try:
            deleted = await asyncio.to_thread(delete_expired)
            if deleted:
                logging.info("Удалено истёкших ключей: %s", deleted)
        except Exception:
            logging.exception("Ошибка очистки истёкших ключей")
        await asyncio.sleep(KEY_CLEANUP_INTERVAL)


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

    key = keyvault.normalize(parts[1])
    key_hash = keyvault.lookup_hash(key)

    try:
        with closing(db()) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE keys SET active = 0 WHERE key = %s", (key_hash,))
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
    except Exception:
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

    plain_key = keyvault.decrypt_key(row["key_enc"]) if row["key_enc"] else None
    if plain_key is None:
        logging.error("Не удалось расшифровать ключ пользователя %s", user_id)
        await message.answer("Не удалось показать ключ. Напишите в поддержку — /support")
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
    product_title = keyvault.PRODUCTS.get(row.get("product") or "app", {}).get("title", "—")

    await message.answer(
        f"🔑 Ваш ключ: {plain_key}\n"
        f"Продукт: {product_title}\n"
        f"Статус: {status}\n"
        f"Действует до: {expires_text}\n"
        f"Устройство: {hwid_text}\n"
        f"Осталось сбросов привязки: {row['resets_left']}\n\n"
        "Для сброса привязки устройства обратитесь в поддержку — /support"
    )


@dp.message(Command("resethwid"))
async def reset_hwid_cmd(message: Message):
    """Сброс привязки устройства к ключу — доступно только админу.
    У каждого ключа ограниченный лимит сбросов (resets_left, по умолчанию 2 — задаётся в /addkey)."""
    if not is_admin(message):
        return

    if not DATABASE_URL:
        await message.answer("DATABASE_URL не настроена на этом сервисе.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: /resethwid КЛЮЧ")
        return

    key = keyvault.normalize(parts[1])
    key_hash = keyvault.lookup_hash(key)

    try:
        with closing(db()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT resets_left FROM keys WHERE key = %s FOR UPDATE", (key_hash,))
                row = cur.fetchone()
                if row is None:
                    await message.answer(f"Ключ {key} не найден.")
                    return
                resets_left = row[0]
                if resets_left <= 0:
                    await message.answer(f"❌ У ключа {key} закончились сбросы привязки (лимит исчерпан).")
                    return
                cur.execute(
                    "UPDATE keys SET hwid = NULL, activations = 0, resets_left = resets_left - 1 WHERE key = %s",
                    (key_hash,),
                )
                conn.commit()
    except Exception as e:
        logging.exception("Ошибка при сбросе привязки")
        await message.answer(f"Не удалось сбросить привязку: {e}")
        return

    await message.answer(
        f"✅ Привязка устройства для ключа {key} сброшена. Осталось сбросов: {resets_left - 1}."
    )


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


# ---------- Оплата через Telegram Payments ----------

def check_invoice_payload(payload: str, amount: int, currency: str):
    """Проверяет счёт до списания денег. Возвращает текст ошибки или None."""
    product, _, plan = payload.partition(":")
    if product not in keyvault.PRODUCTS or plan not in keyvault.PLAN_DAYS:
        return "Такого тарифа больше нет. Оформите заказ заново в мини-приложении"
    if currency != "RUB" or amount != keyvault.PRODUCTS[product]["prices"][plan] * 100:
        return "Цена изменилась. Оформите заказ заново в мини-приложении"
    return None


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    """Telegram спрашивает перед списанием: цена и товар совпадают с прайсом?"""
    error = check_invoice_payload(query.invoice_payload, query.total_amount, query.currency)
    await bot.answer_pre_checkout_query(query.id, ok=error is None, error_message=error)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    """Оплата прошла: выдаём ключ тому, кто платил."""
    payment = message.successful_payment
    buyer = message.from_user
    buyer_id = buyer.id
    product, _, plan = payment.invoice_payload.partition(":")
    charge_id = payment.telegram_payment_charge_id
    title = keyvault.PRODUCTS.get(product, {}).get("title", product)
    uname = f"@{buyer.username}" if buyer.username else "(нет username)"

    try:
        result = await asyncio.to_thread(
            fulfill_payment, charge_id, buyer_id, product, plan, payment.total_amount, payment.currency
        )
    except Exception as e:
        logging.exception("Ошибка выдачи ключа после оплаты %s", charge_id)
        await message.answer(
            "Оплата прошла, но ключ не выдался автоматически. Напишите /support — всё будет выдано."
        )
        await bot.send_message(
            ADMIN_CHAT_ID,
            "⚠️ Оплата прошла, ключ не выдан автоматически\n\n"
            f"Покупатель: {buyer.full_name} {uname}\n"
            f"Telegram ID: {buyer_id}\n"
            f"Товар: {title}, {plan}\n"
            f"Чек: {charge_id}\n"
            f"Ошибка: {e}\n\n"
            f"Выдать вручную: /addkey {plan} {product} {buyer_id}",
        )
        return

    if result is None:
        logging.warning("Повторное уведомление об оплате %s, ключ уже выдан", charge_id)
        return

    key, expires_at = result
    days = keyvault.PLAN_DAYS[plan]
    expires_text = datetime.fromtimestamp(expires_at).strftime("%d.%m.%Y %H:%M")
    await message.answer(
        "✅ Оплата получена!\n\n"
        f"Товар: {title}\n"
        f"Ключ: {key}\n"
        f"Срок: {days} дн., до {expires_text}\n\n"
        "Активировать можно в мини-приложении или командой /mykey.",
        reply_markup=webapp_keyboard(),
    )
    await bot.send_message(
        ADMIN_CHAT_ID,
        "💰 Оплата получена\n\n"
        f"Товар: {title}, {days} дн.\n"
        f"Покупатель: {buyer.full_name} {uname}\n"
        f"Telegram ID: {buyer_id}\n"
        f"Сумма: {payment.total_amount // 100} ₽\n"
        f"Ключ: {key}",
    )


async def main():
    if DATABASE_URL:
        if not os.environ.get("KEY_SECRET"):
            raise SystemExit(
                "Не задана переменная KEY_SECRET. Без неё ключи не зашифровать. "
                "Задай её и запусти бота снова."
            )
        await asyncio.to_thread(ensure_schema)
        asyncio.create_task(cleanup_expired_loop())
    # снимаем вебхук, иначе getUpdates отдаёт TelegramConflictError
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
