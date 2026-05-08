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
    if not url:
        raise Exception("DATABASE_URL не задан!")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url += sep + "sslmode=require"
    return psycopg2.connect(url)


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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS clans (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            emoji       TEXT DEFAULT '🏰',
            description TEXT DEFAULT '',
            owner_id    BIGINT,
            created_at  INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS clan_members (
            clan_id   INTEGER NOT NULL,
            user_id   BIGINT NOT NULL,
            role      TEXT DEFAULT 'member',
            joined_at INTEGER DEFAULT 0,
            PRIMARY KEY (clan_id, user_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS mines_sessions (
            user_id        BIGINT PRIMARY KEY,
            bet            INTEGER NOT NULL,
            mines          INTEGER NOT NULL,
            mine_positions TEXT NOT NULL,
            opened_cells   TEXT NOT NULL DEFAULT '',
            created_at     INTEGER DEFAULT 0
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
        "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS from_id BIGINT",
        "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS to_id BIGINT",
        "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS note TEXT",
        # Для клана в users
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS clan_id INTEGER DEFAULT NULL",
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
        # Реферальный бонус — обоим по 25 000
        if referrer_id:
            cur.execute(
                "SELECT user_id FROM users WHERE user_id=%s", (referrer_id,)
            )
            if cur.fetchone():
                # Новому пользователю добавляем бонус сверх стартового
                cur.execute(
                    "UPDATE users SET balance=balance+%s WHERE user_id=%s",
                    (config.REFERRAL_BONUS, user_id)
                )
                # Рефереру тоже
                cur.execute(
                    "UPDATE users SET balance=balance+%s WHERE user_id=%s",
                    (config.REFERRAL_BONUS, referrer_id)
                )
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
    cur.execute(
        "UPDATE users SET balance=GREATEST(0,balance+%s) WHERE user_id=%s",
        (delta, user_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def get_all_user_ids() -> list:
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
        cur.close(); conn.close()
        return False, 1
    new_xp, new_level, leveled = row[0] + xp, row[1], False
    while new_xp >= new_level * config.LEVEL_BASE_XP:
        new_xp -= new_level * config.LEVEL_BASE_XP
        new_level += 1
        leveled = True
    cur.execute(
        "UPDATE users SET xp=%s,level=%s WHERE user_id=%s",
        (new_xp, new_level, user_id)
    )
    conn.commit()
    cur.close(); conn.close()
    return leveled, new_level


# ── Статистика игр ────────────────────────────────────────────

def record_game(user_id: int, won: bool):
    conn = get_conn()
    cur = conn.cursor()
    if won:
        cur.execute(
            "UPDATE users SET games_played=games_played+1,wins=wins+1 WHERE user_id=%s",
            (user_id,)
        )
    else:
        cur.execute(
            "UPDATE users SET games_played=games_played+1,losses=losses+1 WHERE user_id=%s",
            (user_id,)
        )
    conn.commit()
    cur.close(); conn.close()


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
        cur.close(); conn.close()
        return False, config.DAILY_COOLDOWN - diff
    amount = random.randint(config.DAILY_MIN, config.DAILY_MAX)
    cur.execute(
        "UPDATE users SET balance=balance+%s,daily_last=%s WHERE user_id=%s",
        (amount, now, user_id)
    )
    conn.commit()
    cur.close(); conn.close()
    return True, amount


# ── Перевод монет (бот ↔ игра) ───────────────────────────────

def deposit_to_game(user_id: int, amount: int):
    """Перевод из бот-баланса в игровой баланс."""
    if amount <= 0:
        return False, "Сумма должна быть положительной."
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT balance, game_balance FROM users WHERE user_id=%s FOR UPDATE", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return False, "Пользователь не найден."
    bot_bal, game_bal = row[0], row[1] or 0
    if bot_bal < amount:
        cur.close(); conn.close()
        return False, f"Недостаточно монет. Баланс бота: {bot_bal}"
    new_bot  = bot_bal - amount
    new_game = game_bal + amount
    cur.execute(
        "UPDATE users SET balance=%s, game_balance=%s, updated_at=%s WHERE user_id=%s",
        (new_bot, new_game, int(time.time()), user_id)
    )
    cur.execute(
        "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) "
        "VALUES (%s,NULL,'deposit',%s,'Пополнение игры',%s)",
        (user_id, amount, int(time.time()))
    )
    conn.commit()
    cur.close(); conn.close()
    return True, new_game


def withdraw_from_game(user_id: int, amount: int):
    """Перевод из игрового баланса в бот-баланс."""
    if amount <= 0:
        return False, "Сумма должна быть положительной."
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT balance, game_balance FROM users WHERE user_id=%s FOR UPDATE", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return False, "Пользователь не найден."
    bot_bal, game_bal = row[0], row[1] or 0
    if game_bal < amount:
        cur.close(); conn.close()
        return False, f"Недостаточно средств в игре. Баланс игры: {game_bal}"
    new_bot  = bot_bal + amount
    new_game = game_bal - amount
    cur.execute(
        "UPDATE users SET balance=%s, game_balance=%s, updated_at=%s WHERE user_id=%s",
        (new_bot, new_game, int(time.time()), user_id)
    )
    cur.execute(
        "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) "
        "VALUES (%s,NULL,'withdraw',%s,'Вывод из игры',%s)",
        (user_id, amount, int(time.time()))
    )
    conn.commit()
    cur.close(); conn.close()
    return True, new_bot


# ── Трейд между игроками ──────────────────────────────────────

def trade_coins(from_id: int, to_id: int, amount: int, fee: int = 0):
    """
    Перевод монет из игрового баланса от одного игрока другому.
    Реально списывает amount+fee у from_id, зачисляет amount к to_id.
    """
    if amount <= 0:
        return False, "Сумма должна быть положительной."
    total = amount + fee
    conn = get_conn()
    cur = conn.cursor()
    # Блокируем строки для транзакции
    cur.execute("SELECT game_balance FROM users WHERE user_id=%s FOR UPDATE", (from_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return False, "Отправитель не найден."
    from_game_bal = row[0] or 0
    if from_game_bal < total:
        cur.close(); conn.close()
        return False, f"Недостаточно монет в игре. Баланс: {from_game_bal}"

    cur.execute("SELECT user_id, game_balance FROM users WHERE user_id=%s FOR UPDATE", (to_id,))
    row2 = cur.fetchone()
    if not row2:
        cur.close(); conn.close()
        return False, "Получатель не найден."

    # Списать у отправителя
    cur.execute(
        "UPDATE users SET game_balance=game_balance-%s, updated_at=%s WHERE user_id=%s",
        (total, int(time.time()), from_id)
    )
    # Зачислить получателю
    cur.execute(
        "UPDATE users SET game_balance=game_balance+%s, updated_at=%s WHERE user_id=%s",
        (amount, int(time.time()), to_id)
    )
    # История
    cur.execute(
        "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) "
        "VALUES (%s,%s,'trade',%s,'Трейд между игроками',%s)",
        (from_id, to_id, amount, int(time.time()))
    )
    conn.commit()
    cur.close(); conn.close()
    return True, "ok"


def add_trade_history(from_id: int, to_id: int, amount: int, direction: str = "trade"):
    """Запись в историю переводов (бот-баланс)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) "
        "VALUES (%s,%s,%s,%s,'Перевод через бот',%s)",
        (from_id, to_id, direction, amount, int(time.time()))
    )
    conn.commit()
    cur.close(); conn.close()


def get_trade_history(user_id: int, limit: int = 20):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT t.*, u1.username AS from_username, u2.username AS to_username
           FROM transfers t
           LEFT JOIN users u1 ON t.from_id = u1.user_id
           LEFT JOIN users u2 ON t.to_id   = u2.user_id
           WHERE t.from_id=%s OR t.to_id=%s
           ORDER BY t.created_at DESC LIMIT %s""",
        (user_id, user_id, limit)
    )
    rows = [_row(cur, r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return rows


# ── Топ игроков (по игровому балансу) ─────────────────────────

def get_top_users(limit: int = 30):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM users WHERE is_banned=FALSE ORDER BY game_balance DESC LIMIT %s",
        (limit,)
    )
    rows = [_row(cur, r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return rows


# ── Топ кланов ────────────────────────────────────────────────

def get_top_clans(limit: int = 20):
    """Топ кланов по суммарному game_balance участников."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.name, c.emoji, c.description,
               COUNT(cm.user_id) AS member_count,
               COALESCE(SUM(u.game_balance), 0) AS total_balance
        FROM clans c
        LEFT JOIN clan_members cm ON c.id = cm.clan_id
        LEFT JOIN users u ON cm.user_id = u.user_id
        GROUP BY c.id, c.name, c.emoji, c.description
        ORDER BY total_balance DESC
        LIMIT %s
    """, (limit,))
    rows = [_row(cur, r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return rows


def get_clan_by_name(name: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM clans WHERE LOWER(name)=LOWER(%s)", (name,))
    row = _row(cur, cur.fetchone())
    cur.close(); conn.close()
    return row


def get_all_clans():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.name, c.emoji, c.description,
               COUNT(cm.user_id) AS member_count,
               COALESCE(SUM(u.game_balance), 0) AS total_balance
        FROM clans c
        LEFT JOIN clan_members cm ON c.id = cm.clan_id
        LEFT JOIN users u ON cm.user_id = u.user_id
        GROUP BY c.id, c.name, c.emoji, c.description
        ORDER BY total_balance DESC
    """)
    rows = [_row(cur, r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return rows


# ── Мины: сессии в БД ─────────────────────────────────────────

def mines_save_session(user_id: int, bet: int, mines: int,
                       mine_positions: list, opened_cells: list):
    """Сохранить или обновить сессию игры в мины."""
    pos_str    = ",".join(map(str, mine_positions))
    opened_str = ",".join(map(str, opened_cells))
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO mines_sessions (user_id, bet, mines, mine_positions, opened_cells, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE
            SET bet=%s, mines=%s, mine_positions=%s, opened_cells=%s, created_at=%s
    """, (
        user_id, bet, mines, pos_str, opened_str, int(time.time()),
        bet, mines, pos_str, opened_str, int(time.time())
    ))
    conn.commit()
    cur.close(); conn.close()


def mines_load_session(user_id: int):
    """Загрузить сессию игры в мины из БД. Возвращает dict или None."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM mines_sessions WHERE user_id=%s", (user_id,))
    row = _row(cur, cur.fetchone())
    cur.close(); conn.close()
    if not row:
        return None
    # Парсим списки
    row["mine_positions"] = [int(x) for x in row["mine_positions"].split(",") if x]
    row["opened_cells"]   = [int(x) for x in row["opened_cells"].split(",")   if x]
    return row


def mines_delete_session(user_id: int):
    """Удалить сессию мин из БД (конец игры)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM mines_sessions WHERE user_id=%s", (user_id,))
    conn.commit()
    cur.close(); conn.close()


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
