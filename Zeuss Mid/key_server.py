"""
Zeus Midnight — сервер лицензионных ключей.
БД: PostgreSQL (Railway Postgres — данные сохраняются между рестартами).

Переменные окружения:
    DATABASE_URL       — автоматически задаётся Railway при подключении Postgres
    ZEUS_ADMIN_TOKEN   — токен для /deactivate (поменяй в Railway Variables)

Запуск локально:
    pip install fastapi uvicorn psycopg2-binary cryptography
    DATABASE_URL=postgresql://... python key_server.py
"""
import hashlib
import hmac
import json
import os
import time
import uuid
import plistlib
import datetime
from urllib.parse import parse_qsl

import psycopg2
import psycopg2.extras
import requests
from contextlib import closing
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7

DATABASE_URL  = os.environ["DATABASE_URL"]
ADMIN_TOKEN   = os.environ.get("ZEUS_ADMIN_TOKEN", "change-me-now")
DOWNLOAD_URL  = os.environ.get(
    "DOWNLOAD_URL",
    "https://drive.google.com/file/d/1sMyDNsyQUdOPkn2Pns8I13f58IQ7tgS8/view?usp=drive_link",
)

SIGN_CERT_PEM = os.environ.get("ZAETHERON_SIGN_CERT_PEM")
SIGN_KEY_PEM = os.environ.get("ZAETHERON_SIGN_KEY_PEM")


def _generate_self_signed_signing_identity():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Zaetheron Industry Signing"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Zaetheron"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    return cert, key


if SIGN_CERT_PEM and SIGN_KEY_PEM:
    _sign_cert = x509.load_pem_x509_certificate(SIGN_CERT_PEM.encode())
    _sign_key = serialization.load_pem_private_key(SIGN_KEY_PEM.encode(), password=None)
else:
    _sign_cert, _sign_key = _generate_self_signed_signing_identity()


def sign_mobileconfig(plist_bytes: bytes) -> bytes:
    return (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(plist_bytes)
        .add_signer(_sign_cert, _sign_key, hashes.SHA256())
        .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
    )

COOLDOWN_SECONDS = 5 * 60

PRIVATE_DNS_SERVER_NAME = "dns.quad9.net"
PRIVATE_DNS_SERVERS = [
    "9.9.9.9",
    "149.112.112.112",
    "2620:fe::fe",
    "2620:fe::9",
]


def build_dns_mobileconfig(servers: list[str], server_name: str) -> bytes:
    payload_uuid = str(uuid.uuid4())
    profile_uuid = str(uuid.uuid4())
    profile = {
        "PayloadContent": [
            {
                "PayloadType": "com.apple.dnsSettings.managed",
                "PayloadUUID": payload_uuid,
                "PayloadIdentifier": f"com.zaetheron.dns.{payload_uuid}",
                "PayloadDisplayName": "Zaetheron OPTIM",
                "PayloadDescription": "Configures device to use Quad9 Encrypted DNS over TLS",
                "PayloadVersion": 1,
                "ProhibitDisablement": False,
                "DNSSettings": {
                    "DNSProtocol": "TLS",
                    "ServerAddresses": servers,
                    "ServerName": server_name,
                },
            }
        ],
        "PayloadDisplayName": "Zaetheron OPTIM",
        "PayloadDescription": "Системный DNS на приватные резолверы. Не туннелирует трафик, не является VPN.",
        "PayloadIdentifier": f"com.zaetheron.profile.{profile_uuid}",
        "PayloadOrganization": "Zaetheron",
        "PayloadRemovalDisallowed": False,
        "PayloadType": "Configuration",
        "PayloadUUID": profile_uuid,
        "PayloadVersion": 1,
    }
    return plistlib.dumps(profile, fmt=plistlib.FMT_XML)


def build_basic_mobileconfig() -> bytes:
    profile_uuid = str(uuid.uuid4())
    payload_uuid = str(uuid.uuid4())
    profile = {
        "PayloadContent": [
            {
                "PayloadType": "com.apple.dnsSettings.managed",
                "PayloadUUID": payload_uuid,
                "PayloadIdentifier": f"com.zaetheron.basic.{payload_uuid}",
                "PayloadDisplayName": "Zaetheron BASIC",
                "PayloadDescription": "Базовый профиль Zaetheron",
                "PayloadVersion": 1,
                "ProhibitDisablement": False,
                "DNSSettings": {
                    "DNSProtocol": "TLS",
                    "ServerAddresses": PRIVATE_DNS_SERVERS,
                    "ServerName": PRIVATE_DNS_SERVER_NAME,
                },
            }
        ],
        "PayloadDisplayName": "Zaetheron BASIC",
        "PayloadDescription": "Базовая конфигурация Zaetheron",
        "PayloadIdentifier": f"com.zaetheron.basic.profile.{profile_uuid}",
        "PayloadOrganization": "Zaetheron",
        "PayloadRemovalDisallowed": False,
        "PayloadType": "Configuration",
        "PayloadUUID": profile_uuid,
        "PayloadVersion": 1,
    }
    return plistlib.dumps(profile, fmt=plistlib.FMT_XML)


BOT_TOKEN       = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID   = os.environ.get("ADMIN_CHAT_ID")
SELLER_USERNAME = os.environ.get("SELLER_USERNAME", "hopeyng")

app = FastAPI(title="Zeus Midnight License Server")

app.mount("/static", StaticFiles(directory="webapp/static"), name="static")


@app.get("/app")
def webapp():
    return FileResponse("webapp/index.html")


@app.get("/manifest.json")
def manifest():
    return FileResponse("webapp/manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse("webapp/sw.js", media_type="application/javascript")


def db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def verify_init_data(init_data: str):
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
    if time.time() - auth_date > 86400:
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
        pass


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
            cur.execute("ALTER TABLE keys ADD COLUMN IF NOT EXISTS telegram_id BIGINT")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS request_cooldowns (
                    user_id BIGINT NOT NULL,
                    action TEXT NOT NULL,
                    last_ts BIGINT NOT NULL,
                    PRIMARY KEY (user_id, action)
                )
            """)
        conn.commit()

init_db()


def check_cooldown(conn, cur, user_id: int, action: str):
    now = int(time.time())
    cur.execute(
        "SELECT last_ts FROM request_cooldowns WHERE user_id = %s AND action = %s",
        (user_id, action),
    )
    row = cur.fetchone()
    last_ts = row[0] if row else 0
    elapsed = now - last_ts
    if elapsed < COOLDOWN_SECONDS:
        return False, COOLDOWN_SECONDS - elapsed
    cur.execute(
        """
        INSERT INTO request_cooldowns (user_id, action, last_ts)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, action) DO UPDATE SET last_ts = EXCLUDED.last_ts
        """,
        (user_id, action, now),
    )
    return True, 0


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

class ReportReq(BaseModel):
    message: str
    init_data: str
    key: str | None = None


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
    user = verify_init_data(req.init_data)
    if not user:
        raise HTTPException(401, "invalid_init_data")

    user_id = user.get("id")

    with closing(db()) as conn:
        with conn.cursor() as cur:
            allowed, retry_after = check_cooldown(conn, cur, user_id, "buy")
            conn.commit()
    if not allowed:
        raise HTTPException(429, f"Слишком часто. Попробуйте через {retry_after} сек.")

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


@app.post("/report")
def report(req: ReportReq):
    user = verify_init_data(req.init_data)
    if not user:
        raise HTTPException(401, "invalid_init_data")

    user_id = user.get("id")
    text = req.message.strip()
    if not text:
        raise HTTPException(400, "empty_message")

    with closing(db()) as conn:
        with conn.cursor() as cur:
            allowed, retry_after = check_cooldown(conn, cur, user_id, "report")
            conn.commit()
    if not allowed:
        raise HTTPException(429, f"Слишком часто. Попробуйте через {retry_after} сек.")

    username = user.get("username")
    uname = f"@{username}" if username else "(нет username)"
    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()

    if ADMIN_CHAT_ID:
        tg_send_message(
            ADMIN_CHAT_ID,
            "🆘 Репорт из мини-аппа\n\n"
            f"От: {full_name} {uname}\n"
            f"Telegram ID: {user_id}\n"
            f"Ключ: {req.key or '—'}\n\n"
            f"{text}\n\n"
            f"Ответить через бота: /reply {user_id} текст ответа",
        )

    tg_send_message(user_id, "Обращение отправлено в поддержку, вам ответят в этом же чате с ботом.")
    return {"ok": True}


@app.post("/check")
def check(req: CheckReq):
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


@app.get("/optimize.mobileconfig")
def optimize_mobileconfig(key: str):
    key = _norm(key)
    with closing(db()) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM keys WHERE key = %s", (key,))
            row = cur.fetchone()
            if row is None or not row["active"]:
                raise HTTPException(403, "invalid_key")
            if row["expires_at"] and row["expires_at"] < time.time():
                raise HTTPException(403, "expired")

    config_bytes = build_dns_mobileconfig(PRIVATE_DNS_SERVERS, PRIVATE_DNS_SERVER_NAME)
    signed_bytes = sign_mobileconfig(config_bytes)
    return Response(
        content=signed_bytes,
        media_type="application/x-apple-aspen-config",
        headers={"Content-Disposition": "attachment; filename=ZaetheronOPTIM.mobileconfig"},
    )


@app.get("/basic.mobileconfig")
def basic_mobileconfig(key: str):
    key = _norm(key)
    with closing(db()) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM keys WHERE key = %s", (key,))
            row = cur.fetchone()
            if row is None or not row["active"]:
                raise HTTPException(403, "invalid_key")
            if row["expires_at"] and row["expires_at"] < time.time():
                raise HTTPException(403, "expired")

    config_bytes = build_basic_mobileconfig()
    signed_bytes = sign_mobileconfig(config_bytes)
    return Response(
        content=signed_bytes,
        media_type="application/x-apple-aspen-config",
        headers={"Content-Disposition": "attachment; filename=ZaetheronBASIC.mobileconfig"},
    )


@app.post("/reset_hwid")
def reset_hwid(req: ResetHwidReq):
    if req.admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "Неверный admin_token")
    key = _norm(req.key)
    with closing(db()) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT resets_left FROM keys WHERE key = %s FOR UPDATE", (key,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "Ключ не найден")
            if row["resets_left"] <= 0:
                raise HTTPException(403, "Сбросы привязки для этого ключа закончились")
            cur.execute(
                "UPDATE keys SET hwid = NULL, activations = 0, resets_left = resets_left - 1 WHERE key = %s",
                (key,),
            )
            conn.commit()
        return {"ok": True, "resets_left": row["resets_left"] - 1}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
