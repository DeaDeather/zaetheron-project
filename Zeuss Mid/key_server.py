"""
Zeus Midnight — сервер лицензионных ключей.
БД: PostgreSQL (Railway Postgres — данные сохраняются между рестартами).

Переменные окружения:
    DATABASE_URL       — автоматически задаётся Railway при подключении Postgres
    ZEUS_ADMIN_TOKEN   — токен для /deactivate (поменяй в Railway Variables)

Запуск локально:
    pip install fastapi uvicorn psycopg2-binary
    DATABASE_URL=postgresql://... python key_server.py
"""
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

import psycopg2
import psycopg2.extras
import requests
from contextlib import closing
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

DATABASE_URL  = os.environ["DATABASE_URL"]  # Railway подставляет автоматически
ADMIN_TOKEN   = os.environ.get("ZEUS_ADMIN_TOKEN", "change-me-now")
DOWNLOAD_URL  = os.environ.get(
    "DOWNLOAD_URL",
    "https://drive.google.com/file/d/1sMyDNsyQUdOPkn2Pns8I13f58IQ7tgS8/view?usp=drive_link",
)

# Нужны для отправки заявок на покупку прямо из мини-аппа, без sendData()
BOT_TOKEN       = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID   = os.environ.get("ADMIN_CHAT_ID")
SELLER_USERNAME = os.environ.get("SELLER_USERNAME", "hopeyng")

app = FastAPI(title="Zeus Midnight License Server")


@app.get("/app")
def webapp():
    """Отдаёт Telegram Mini App (webapp/index.html)."""
    return FileResponse("webapp/index.html")


def db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def verify_init_data(init_data: str):
    """Проверяет подпись Telegram WebApp initData. Возвращает словарь пользователя или None,
    если подпись неверна (значит запрос не из настоящего Telegram)."""
    if not BOT_TOKEN or not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None
    auth_date = int(pairs.get("auth_date", "0"))
    if time.time() - auth_date > 86400:  # старше суток — на всякий случай отклоняем
        return None
    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except ValueError:
        return None


def tg_send_message(chat_id, text, reply_markup=None):
    if not BOT_TOKEN:
        return
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
    except Exception:
        pass  # не роняем запрос покупателя из-за проблем с уведомлением


def init_db():
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS keys (
                    key TEXT PRIMARY KEY,
                    hwid TEXT,
                    active INTEGER DEFAULT 1,
                    max_activations INTEGER DEFAULT 1,
                    activations INTEGER DEFAULT 0,
                    expires_at BIGINT,
                    resets_left INTEGER DEFAULT 2,
                    note TEXT,
                    created_at BIGINT
                )
            """)
            # На случай, если таблица уже существовала без этой колонки
            cur.execute("ALTER TABLE keys ADD COLUMN IF NOT EXISTS telegram_id BIGINT")
        conn.commit()

init_db()


class ActivateReq(BaseModel):
    key: str
    hwid: str

class ValidateReq(BaseModel):
    key: str
    hwid: str

class DeactivateReq(BaseModel):
    key: str
    admin_token: str

class ResetHwidReq(BaseModel):
    key: str
    admin_token: str

class CheckReq(BaseModel):
    key: str

class BuyReq(BaseModel):
    plan: str
    label: str
    price: str
    init_data: str


def _norm(key: str) -> str:
    return key.strip().upper()


def _row_status(row, hwid):
    if row is None:
        return False, "not_found"
    if not row["active"]:
        return False, "revoked"
    if row["expires_at"] and row["expires_at"] < time.time():
        return False, "expired"
    if row["hwid"] and row["hwid"] != hwid:
        return False, "hwid_mismatch"
    return True, "ok"


@app.post("/activate")
def activate(req: ActivateReq):
    key = _norm(req.key)
    with closing(db()) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM keys WHERE key = %s", (key,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "Ключ не найден")
            if not row["active"]:
                raise HTTPException(403, "Ключ отозван")
            if row["expires_at"] and row["expires_at"] < time.time():
                raise HTTPException(403, "Срок действия ключа истёк")

            if row["hwid"] == req.hwid:
                return {"ok": True, "status": "already_active"}

            if row["hwid"] is None:
                if row["activations"] >= row["max_activations"]:
                    raise HTTPException(403, "Превышен лимит активаций")
                cur.execute(
                    "UPDATE keys SET hwid = %s, activations = activations + 1 WHERE key = %s",
                    (req.hwid, key),
                )
                conn.commit()
                return {"ok": True, "status": "activated"}

            raise HTTPException(409, "Ключ уже активирован на другом устройстве")


@app.post("/validate")
def validate(req: ValidateReq):
    key = _norm(req.key)
    with closing(db()) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM keys WHERE key = %s", (key,))
            row = cur.fetchone()
            ok, reason = _row_status(row, req.hwid)
            if not ok:
                raise HTTPException(403, reason)
            return {"ok": True, "expires_at": row["expires_at"]}


@app.post("/deactivate")
def deactivate(req: DeactivateReq):
    if req.admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "Неверный admin_token")
    key = _norm(req.key)
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE keys SET hwid = NULL, activations = 0 WHERE key = %s", (key,))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(404, "Ключ не найден")
        return {"ok": True}


@app.post("/buy")
def buy(req: BuyReq):
    """Мини-апп стучится сюда напрямую при нажатии «Купить» —
    работает независимо от того, как открыт мини-апп (inline-кнопка, меню и т.д.)."""
    user = verify_init_data(req.init_data)
    if not user:
        raise HTTPException(401, "invalid_init_data")

    user_id = user.get("id")
    username = user.get("username")
    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()

    tg_send_message(
        user_id,
        f"Заявка на «{req.label}» ({req.price}₽) принята.\n"
        f"Для оплаты и получения ключа напишите продавцу:",
        reply_markup={
            "inline_keyboard": [[
                {"text": f"Написать @{SELLER_USERNAME}", "url": f"https://t.me/{SELLER_USERNAME}"}
            ]]
        },
    )

    if ADMIN_CHAT_ID:
        uname = f"@{username}" if username else "(нет username)"
        tg_send_message(
            ADMIN_CHAT_ID,
            "🛒 Новая заявка\n\n"
            f"Тариф: {req.label}\n"
            f"Цена: {req.price}₽\n"
            f"Покупатель: {full_name} {uname}\n"
            f"Telegram ID: {user_id}\n\n"
            f"Создать ключ: /addkey КЛЮЧ ДНИ {user_id}",
        )

    return {"ok": True}


@app.post("/check")
def check(req: CheckReq):
    """Используется мини-аппом: показывает статус ключа и ссылку на скачивание."""
    key = _norm(req.key)
    with closing(db()) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM keys WHERE key = %s", (key,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "not_found")
            if not row["active"]:
                raise HTTPException(403, "revoked")
            if row["expires_at"] and row["expires_at"] < time.time():
                raise HTTPException(403, "expired")
            return {
                "ok": True,
                "expires_at": row["expires_at"],
                "hwid_bound": row["hwid"] is not None,
                "resets_left": row["resets_left"],
                "download_url": DOWNLOAD_URL,
            }


@app.post("/reset_hwid")
def reset_hwid(req: ResetHwidReq):
    if req.admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "Неверный admin_token")
    key = _norm(req.key)
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE keys SET hwid = NULL, activations = 0 WHERE key = %s", (key,))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(404, "Ключ не найден")
        return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
