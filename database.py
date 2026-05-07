"""
database.py — PostgreSQL база для GOLDCLICK Bot
"""

import psycopg2
import time
import random
import os
import config


def get_conn():
    url = os.environ.get("DATABASE_URL", "")
    if url:
        conn = psycopg2.connect(url)
    else:
        raise Exception("DATABASE_URL не задан!")
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      BIGINT PRIMARY KEY,
            username     TEXT,
            balance      INTEGER DEFAULT 0,
            game_balance INTEGER DEFAULT 0,
            game_state   TEXT    DEFAULT NULL,
            xp           INTEGER DEFAULT 0,
            level        INTEGER DEFAULT 1,
            games_played INTEGER DEFAULT 0,
            wins         INTEGER DEFAULT 0,
            losses       INTEGER DEFAULT 0,
            daily_last   INTEGER DEFAULT 0,
            referrer_id  BIGINT  DEFAULT NULL,
            created_at   INTEGER DEFAULT 0,
            updated_at   INTEGER DEFAULT 0,
            is_banned    BOOLEAN DEFAULT FALSE
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transfers (
            id          SERIAL PRIMARY KEY,
            from_id     BIGINT NOT NULL,
            to_id       BIGINT,
            direction   TEXT NOT NULL,
            amount      INTEGER NOT NULL,
            note        TEXT DEFAULT NULL,
            created_at  INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    cur.close()
    conn.close()

    # ── Миграции ──────────────────────────────────────────────
    conn = get_conn()
    cur = conn.cursor()

    migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE",
        """CREATE TABLE IF NOT EXISTS transfers_new AS SELECT
              id, user_id AS from_id, NULL::BIGINT AS to_id,
              direction, amount, NULL::TEXT AS note, created_at
           FROM transfers WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns
              WHERE table_name='transfers' AND column_name='from_id')""",
        "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS from_id BIGINT",
        "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS to_id BIGINT",
        "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS note TEXT",
    ]

    for sql in migrations:
        try:
            cur.execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()

    cur.close()
    conn.close()


def _row(cur, row):
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


# ── Пользователи ──────────────────────────────────────────────

def ensure_user(user_id: int, username: str, referrer_id: int = None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=%s", (user_id,))
    if not cur.fetchone():
        cur.execute(
            """INSERT INTO users (user_id,username,balance,game_balance,game_state,xp,level,
               games_played,wins,losses,daily_last,referrer_id,created_at,updated_at,is_banned)
               VALUES (%s,%s,%s,0,NULL,0,1,0,0,0,0,%s,%s,%s,FALSE)""",
            (user_id, username, config.STARTING_BALANCE, referrer_id,
             int(time.time()), int(time.time()))
        )
        if referrer_id:
            cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s",
                        (config.REFERRAL_BONUS, referrer_id))
        conn.commit()
    else:
        cur.execute("UPDATE users SET username=%s WHERE user_id=%s", (username, user_id))
        conn.commit()
    cur.close()
    conn.close()


def get_user(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
    result = _row(cur, cur.fetchone())
    cur.close()
    conn.close()
    return result


def update_balance(user_id: int, delta: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance=GREATEST(0,balance+%s) WHERE user_id=%s", (delta, user_id))
    conn.commit()
    cur.close()
    conn.close()


def get_all_user_ids() -> list:
    """Возвращает список всех незабаненных user_id для рассылки."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE is_banned=FALSE")
    ids = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return ids


# ── XP и уровни ──────────────────────────────────────────────

def add_xp(user_id: int, xp: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT xp,level FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return False, 1
    new_xp, new_level, leveled = row[0] + xp, row[1], False
    while new_xp >= new_level * config.LEVEL_BASE_XP:
        new_xp -= new_level * config.LEVEL_BASE_XP
        new_level += 1
        leveled = True
    cur.execute("UPDATE users SET xp=%s,level=%s WHERE user_id=%s", (new_xp, new_level, user_id))
    conn.commit()
    cur.close()
    conn.close()
    return leveled, new_level


# ── Статистика игр ────────────────────────────────────────────

def record_game(user_id: int, won: bool):
    conn = get_conn()
    cur = conn.cursor()
    if won:
        cur.execute("UPDATE users SET games_played=games_played+1,wins=wins+1 WHERE user_id=%s", (user_id,))
    else:
        cur.execute("UPDATE users SET games_played=games_played+1,losses=losses+1 WHERE user_id=%s", (user_id,))
    conn.commit()
    cur.close()
    conn.close()


# ── Ежедневный бонус ─────────────────────────────────────────

def claim_daily(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT daily_last FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    now = int(time.time())
    last = row[0] if row else 0
    diff = now - last
    if diff < config.DAILY_COOLDOWN:
        cur.close()
        conn.close()
        return False, config.DAILY_COOLDOWN - diff
    amount = random.randint(config.DAILY_MIN, config.DAILY_MAX)
    cur.execute("UPDATE users SET balance=balance+%s,daily_last=%s WHERE user_id=%s", (amount, now, user_id))
    conn.commit()
    cur.close()
    conn.close()
    return True, amount


# ── Перевод монет (бот ↔ игра) ───────────────────────────────

def deposit_to_game(user_id: int, amount: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT balance,game_balance FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return False, "Пользователь не найден."
    if row[0] < amount:
        cur.close(); conn.close()
        return False, f"Недостаточно монет. Баланс: {row[0]}"
    cur.execute("UPDATE users SET balance=%s,game_balance=%s,updated_at=%s WHERE user_id=%s",
                (row[0] - amount, (row[1] or 0) + amount, int(time.time()), user_id))
    cur.execute(
        "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) VALUES (%s,NULL,'deposit',%s,'Пополнение игры',%s)",
        (user_id, amount, int(time.time()))
    )
    conn.commit()
    cur.close(); conn.close()
    return True, (row[1] or 0) + amount


def withdraw_from_game(user_id: int, amount: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT balance,game_balance FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return False, "Пользователь не найден."
    game_bal = row[1] or 0
    if game_bal < amount:
        cur.close(); conn.close()
        return False, f"Недостаточно средств. Баланс: {game_bal}"
    cur.execute("UPDATE users SET balance=%s,game_balance=%s,updated_at=%s WHERE user_id=%s",
                (row[0] + amount, game_bal - amount, int(time.time()), user_id))
    cur.execute(
        "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) VALUES (%s,NULL,'withdraw',%s,'Вывод из игры',%s)",
        (user_id, amount, int(time.time()))
    )
    conn.commit()
    cur.close(); conn.close()
    return True, row[0] + amount


# ── Трейд между игроками ──────────────────────────────────────

def trade_coins(from_id: int, to_id: int, amount: int, fee: int):
    """Перевод монет из игрового баланса от одного игрока другому."""
    conn = get_conn()
    cur = conn.cursor()
    total = amount + fee

    cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (from_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return False, "Отправитель не найден."
    if (row[0] or 0) < total:
        cur.close(); conn.close()
        return False, f"Недостаточно монет. Баланс: {row[0] or 0}"

    cur.execute("SELECT user_id FROM users WHERE user_id=%s", (to_id,))
    if not cur.fetchone():
        cur.close(); conn.close()
        return False, "Получатель не найден."

    cur.execute("UPDATE users SET game_balance=game_balance-%s WHERE user_id=%s", (total, from_id))
    cur.execute("UPDATE users SET game_balance=game_balance+%s WHERE user_id=%s", (amount, to_id))
    now = int(time.time())
    cur.execute(
        "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) VALUES (%s,%s,'trade',%s,'Трейд',%s)",
        (from_id, to_id, amount, now)
    )
    conn.commit()
    cur.close(); conn.close()
    return True, "ok"


def get_trade_history(user_id: int, limit: int = 20):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT t.*, u1.username AS from_username, u2.username AS to_username
           FROM transfers t
           LEFT JOIN users u1 ON t.from_id = u1.user_id
           LEFT JOIN users u2 ON t.to_id = u2.user_id
           WHERE t.from_id=%s OR t.to_id=%s
           ORDER BY t.created_at DESC LIMIT %s""",
        (user_id, user_id, limit)
    )
    rows = [_row(cur, r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return rows


# ── Топ игроков ───────────────────────────────────────────────

def get_top_users(limit: int = 30):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM users WHERE is_banned=FALSE ORDER BY balance DESC LIMIT %s",
        (limit,)
    )
    rows = [_row(cur, r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return rows


# ── Баны ─────────────────────────────────────────────────────

def is_banned(user_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT is_banned FROM users WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
    except Exception:
        row = None
    cur.close(); conn.close()
    if row is None:
        return False
    return bool(row[0])


def set_ban(user_id: int, banned: bool):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_banned=%s WHERE user_id=%s", (banned, user_id))
    conn.commit()
    cur.close(); conn.close()
