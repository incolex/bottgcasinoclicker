"""
database.py — SQLite-база для GOLDCLICK Bot
"""

import sqlite3
import time
import random
import config


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()

    # Основная таблица пользователей
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
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
            referrer_id  INTEGER DEFAULT NULL,
            created_at   INTEGER DEFAULT 0,
            updated_at   INTEGER DEFAULT 0
        )
    """)

    # Миграция: добавить новые колонки если их нет (для уже существующих БД)
    for col, definition in [
        ("game_state", "TEXT DEFAULT NULL"),
        ("updated_at", "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
        except Exception:
            pass  # колонка уже есть

    # История переводов
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transfers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            direction   TEXT NOT NULL,
            amount      INTEGER NOT NULL,
            created_at  INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()


# ── Пользователи ──────────────────────────────────────────────

def ensure_user(user_id: int, username: str, referrer_id: int = None):
    """Создаёт пользователя если не существует."""
    conn = get_conn()
    row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        conn.execute(
            """INSERT INTO users
               (user_id, username, balance, game_balance, game_state, xp, level,
                games_played, wins, losses, daily_last, referrer_id, created_at, updated_at)
               VALUES (?, ?, ?, 0, NULL, 0, 1, 0, 0, 0, 0, ?, ?, ?)""",
            (user_id, username, config.STARTING_BALANCE, referrer_id,
             int(time.time()), int(time.time()))
        )
        if referrer_id:
            conn.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (config.REFERRAL_BONUS, referrer_id)
            )
        conn.commit()
    else:
        conn.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
        conn.commit()
    conn.close()


def get_user(user_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def update_balance(user_id: int, delta: int):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET balance = MAX(0, balance + ?) WHERE user_id=?",
        (delta, user_id)
    )
    conn.commit()
    conn.close()


# ── XP и уровни ──────────────────────────────────────────────

def add_xp(user_id: int, xp: int):
    conn = get_conn()
    row = conn.execute("SELECT xp, level FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        conn.close()
        return False, 1

    new_xp = row["xp"] + xp
    new_level = row["level"]
    leveled = False

    while new_xp >= new_level * config.LEVEL_BASE_XP:
        new_xp -= new_level * config.LEVEL_BASE_XP
        new_level += 1
        leveled = True

    conn.execute(
        "UPDATE users SET xp=?, level=? WHERE user_id=?",
        (new_xp, new_level, user_id)
    )
    conn.commit()
    conn.close()
    return leveled, new_level


# ── Статистика игр ────────────────────────────────────────────

def record_game(user_id: int, won: bool):
    conn = get_conn()
    if won:
        conn.execute(
            "UPDATE users SET games_played=games_played+1, wins=wins+1 WHERE user_id=?",
            (user_id,)
        )
    else:
        conn.execute(
            "UPDATE users SET games_played=games_played+1, losses=losses+1 WHERE user_id=?",
            (user_id,)
        )
    conn.commit()
    conn.close()


# ── Ежедневный бонус ─────────────────────────────────────────

def claim_daily(user_id: int):
    conn = get_conn()
    row = conn.execute("SELECT daily_last FROM users WHERE user_id=?", (user_id,)).fetchone()
    now = int(time.time())
    last = row["daily_last"] if row else 0
    diff = now - last

    if diff < config.DAILY_COOLDOWN:
        conn.close()
        return False, config.DAILY_COOLDOWN - diff

    amount = random.randint(config.DAILY_MIN, config.DAILY_MAX)
    conn.execute(
        "UPDATE users SET balance=balance+?, daily_last=? WHERE user_id=?",
        (amount, now, user_id)
    )
    conn.commit()
    conn.close()
    return True, amount


# ── Перевод монет ─────────────────────────────────────────────

def deposit_to_game(user_id: int, amount: int):
    conn = get_conn()
    row = conn.execute("SELECT balance, game_balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        conn.close()
        return False, "Пользователь не найден."
    if row["balance"] < amount:
        conn.close()
        return False, f"Недостаточно монет. Баланс: {row['balance']}"
    new_bal  = row["balance"] - amount
    new_game = (row["game_balance"] or 0) + amount
    conn.execute(
        "UPDATE users SET balance=?, game_balance=?, updated_at=? WHERE user_id=?",
        (new_bal, new_game, int(time.time()), user_id)
    )
    conn.execute(
        "INSERT INTO transfers (user_id, direction, amount, created_at) VALUES (?, 'deposit', ?, ?)",
        (user_id, amount, int(time.time()))
    )
    conn.commit()
    conn.close()
    return True, new_game


def withdraw_from_game(user_id: int, amount: int):
    conn = get_conn()
    row = conn.execute("SELECT balance, game_balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        conn.close()
        return False, "Пользователь не найден."
    game_bal = row["game_balance"] or 0
    if game_bal < amount:
        conn.close()
        return False, f"Недостаточно средств в игре. Баланс: {game_bal}"
    new_game = game_bal - amount
    new_bal  = row["balance"] + amount
    conn.execute(
        "UPDATE users SET balance=?, game_balance=?, updated_at=? WHERE user_id=?",
        (new_bal, new_game, int(time.time()), user_id)
    )
    conn.execute(
        "INSERT INTO transfers (user_id, direction, amount, created_at) VALUES (?, 'withdraw', ?, ?)",
        (user_id, amount, int(time.time()))
    )
    conn.commit()
    conn.close()
    return True, new_bal


# ── История переводов ─────────────────────────────────────────

def get_transfer_history(user_id: int, limit: int = 10):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM transfers WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit)
    ).fetchall()
    conn.close()
    return rows


# ── Топ игроков ───────────────────────────────────────────────

def get_top_users(limit: int = 10):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM users ORDER BY balance DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows
