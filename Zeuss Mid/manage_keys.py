"""
Управление ключами Zeus Midnight.
Работает с PostgreSQL — той же БД, что и key_server.py на Railway.

Настройка: задай переменную окружения DATABASE_URL перед запуском:
    Windows:  set DATABASE_URL=postgresql://user:pass@host:5432/dbname
    Linux/Mac: export DATABASE_URL=postgresql://user:pass@host:5432/dbname

DATABASE_URL берётся из Railway → твой Postgres сервис → Variables → DATABASE_URL

Примеры:
    python manage_keys.py add
    python manage_keys.py add --count 50 --days 365
    python manage_keys.py add --key "CANATE" --note "Владелец"
    python manage_keys.py add --note "заказ #102" --activations 2
    python manage_keys.py list
    python manage_keys.py revoke ZEUS1-XXXXX-XXXXX-XXXXX
    python manage_keys.py unbind ZEUS1-XXXXX-XXXXX-XXXXX
    python manage_keys.py extend ZEUS1-XXXXX-XXXXX-XXXXX --days 30
"""
import argparse
import os
import secrets
import string
import time

import psycopg2
import psycopg2.extras
from contextlib import closing

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit(
        "Ошибка: переменная DATABASE_URL не задана.\n"
        "Скопируй её из Railway → Postgres сервис → Variables → DATABASE_URL\n"
        "и задай перед запуском:\n"
        "  Windows:   set DATABASE_URL=postgresql://...\n"
        "  Linux/Mac: export DATABASE_URL=postgresql://..."
    )


def db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def ensure_table():
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
        conn.commit()


ensure_table()


def gen_key():
    alphabet = string.ascii_uppercase + string.digits
    groups = ["".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4)]
    return "-".join(groups)


def cmd_add(args):
    with closing(db()) as conn:
        keys_to_create = [args.key.upper()] if args.key else [gen_key() for _ in range(args.count)]
        with conn.cursor() as cur:
            for key in keys_to_create:
                expires_at = int(time.time() + args.days * 86400) if args.days else None
                cur.execute(
                    "INSERT INTO keys (key, max_activations, expires_at, resets_left, note, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (key, args.activations, expires_at, args.resets, args.note, int(time.time())),
                )
        conn.commit()
    print(f"Создано ключей: {len(keys_to_create)}")
    for k in keys_to_create:
        print(" ", k)


def cmd_delete(args):
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM keys WHERE key = %s", (args.key.upper(),))
            conn.commit()
            print("Удалён" if cur.rowcount else "Ключ не найден")


def cmd_delete_all(args):
    confirm = input("Удалить ВСЕ ключи? Введите YES: ")
    if confirm.strip() != "YES":
        print("Отменено")
        return
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM keys")
            conn.commit()
            print(f"Удалено ключей: {cur.rowcount}")


def cmd_list(args):
    with closing(db()) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM keys ORDER BY created_at DESC")
            rows = cur.fetchall()
    for r in rows:
        status = "revoked" if not r["active"] else (
            "expired" if r["expires_at"] and r["expires_at"] < time.time() else "active")
        bound = (r["hwid"][:12] + "…") if r["hwid"] else "—"
        exp = time.strftime("%Y-%m-%d", time.localtime(r["expires_at"])) if r["expires_at"] else "∞"
        print(f"{r['key']}  [{status:8}]  hwid={bound:14}  "
              f"act={r['activations']}/{r['max_activations']}  "
              f"resets={r['resets_left']}  expires={exp}  "
              f"note={r['note'] or ''}")


def cmd_revoke(args):
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE keys SET active = 0 WHERE key = %s", (args.key.upper(),))
            conn.commit()
            print("Отозван" if cur.rowcount else "Ключ не найден")


def cmd_unbind(args):
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE keys SET hwid = NULL, activations = 0 WHERE key = %s", (args.key.upper(),))
            conn.commit()
            print("Привязка снята" if cur.rowcount else "Ключ не найден")


def cmd_extend(args):
    with closing(db()) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT expires_at FROM keys WHERE key = %s", (args.key.upper(),))
            row = cur.fetchone()
            if not row:
                print("Ключ не найден")
                return
            base = row["expires_at"] if row["expires_at"] and row["expires_at"] > time.time() else time.time()
            new_exp = int(base + args.days * 86400)
            cur.execute("UPDATE keys SET expires_at = %s, active = 1 WHERE key = %s",
                        (new_exp, args.key.upper()))
            conn.commit()
    print(f"Новый срок действия: {time.strftime('%Y-%m-%d', time.localtime(new_exp))}")


def cmd_give_resets(args):
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE keys SET resets_left = resets_left + %s WHERE key = %s",
                (args.amount, args.key.upper()),
            )
            conn.commit()
            print("Выдано" if cur.rowcount else "Ключ не найден")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("add")
    pa.add_argument("--key", default="")
    pa.add_argument("--count", type=int, default=1)
    pa.add_argument("--days", type=int, default=0, help="0 = бессрочный")
    pa.add_argument("--activations", type=int, default=1, help="сколько разных ПК можно активировать")
    pa.add_argument("--resets", type=int, default=2, help="сколько раз юзер сам может сбросить HWID")
    pa.add_argument("--note", default="")
    pa.set_defaults(func=cmd_add)

    sub.add_parser("list").set_defaults(func=cmd_list)

    pd = sub.add_parser("delete"); pd.add_argument("key"); pd.set_defaults(func=cmd_delete)
    sub.add_parser("delete-all").set_defaults(func=cmd_delete_all)

    pr = sub.add_parser("revoke"); pr.add_argument("key"); pr.set_defaults(func=cmd_revoke)
    pu = sub.add_parser("unbind"); pu.add_argument("key"); pu.set_defaults(func=cmd_unbind)
    pe = sub.add_parser("extend"); pe.add_argument("key"); pe.add_argument("--days", type=int, required=True)
    pe.set_defaults(func=cmd_extend)

    pg = sub.add_parser("give-resets"); pg.add_argument("key"); pg.add_argument("--amount", type=int, default=1)
    pg.set_defaults(func=cmd_give_resets)

    args = p.parse_args()
    args.func(args)
