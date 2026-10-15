"""
Ключи ZTH-XXXX-XXXX-XXXX: генерация, шифрование и поиск по хэшу.
Общий модуль для bot.py и key_server.py. Положи его в ту же папку, что и они.

Переменная окружения (одна и та же у бота и у сервера):
    KEY_SECRET — длинная случайная строка. Ей шифруются ключи и считаются хэши.
                 Если её поменять, все существующие ключи перестанут работать.
"""
import base64
import hashlib
import hmac
import os
import secrets
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

KEY_PREFIX = "ZTH"
KEY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # без похожих символов: 0/O, 1/I/L
KEY_BODY_LEN = 12  # 32^12 ≈ 1.15e18 вариантов

_SECRET = os.environ.get("KEY_SECRET", "")


def _secret_bytes() -> bytes:
    if not _SECRET:
        raise RuntimeError("Не задана переменная окружения KEY_SECRET")
    return _SECRET.encode()


def normalize(raw: str) -> str:
    return raw.strip().upper()


def generate_plain_key() -> str:
    """Случайный ключ вида ZTH-XXXX-XXXX-XXXX (криптостойкий генератор)."""
    body = "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_BODY_LEN))
    return f"{KEY_PREFIX}-{body[0:4]}-{body[4:8]}-{body[8:12]}"


def lookup_hash(raw_key: str) -> str:
    """Хэш ключа (HMAC с секретом). По нему ищем строку в базе.
    Из хэша сам ключ восстановить нельзя."""
    msg = b"zth-lookup:" + normalize(raw_key).encode()
    return hmac.new(_secret_bytes(), msg, hashlib.sha256).hexdigest()


def _fernet() -> Fernet:
    digest = hashlib.sha256(b"zth-fernet:" + _secret_bytes()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_key(plain: str) -> str:
    """Шифрует ключ для хранения в базе. Нужен, чтобы покупатель мог увидеть ключ в /mykey."""
    return _fernet().encrypt(normalize(plain).encode()).decode()


def decrypt_key(token: str) -> Optional[str]:
    """Расшифровывает ключ. Возвращает None, если токен битый или секрет не тот."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


# ---------- Товары и цены (общие для бота и сервера) ----------
# Цены в рублях. Меняешь здесь — меняется и в боте, и в key_server.py.
# В index.html цены дублируются для показа (CONFIG.products.*.prices): поменяй и там.
PRODUCTS = {
    "app": {"title": "Zaetheron для Windows", "prices": {"1d": 399, "3d": 699, "7d": 1299}},
    "os": {"title": "Zaetheron Custom OS", "prices": {"1d": 539, "3d": 944, "7d": 1754}},
}
PLAN_DAYS = {"1d": 1, "3d": 3, "7d": 7}
PLAN_LABELS = {"1d": "1 день", "3d": "3 дня", "7d": "7 дней"}
