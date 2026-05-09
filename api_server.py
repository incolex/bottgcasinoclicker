# api_server.py — FastAPI сервер для GOLDCLICK Bot
# Все данные хранятся в PostgreSQL, никакой локальной логики в HTML

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import psycopg2
import psycopg2.extras
import json
import time
import os
import httpx
import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="GOLDCLICK API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")


def get_conn():
    import re
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    # Убираем sslmode из URL и передаём отдельным параметром
    url = re.sub(r"[?&]sslmode=[^&]*", "", url)
    return psycopg2.connect(url, sslmode="require", cursor_factory=psycopg2.extras.RealDictCursor)


# ── Уведомление через Telegram Bot API ───────────────────────

async def notify_user(chat_id: int, text: str):
    if not BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            )
    except Exception as e:
        logger.warning(f"notify_user {chat_id}: {e}")


# ══════════════════════════════════════════════════════════════
# ИНИЦИАЛИЗАЦИЯ БД — создать таблицы если нет
# ══════════════════════════════════════════════════════════════

@app.on_event("startup")
def init_db():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      BIGINT PRIMARY KEY,
            username     TEXT,
            balance      INTEGER DEFAULT 1000,
            game_balance INTEGER DEFAULT 0,
            game_state   TEXT    DEFAULT NULL,
            xp           INTEGER DEFAULT 0,
            level        INTEGER DEFAULT 1,
            games_played INTEGER DEFAULT 0,
            wins         INTEGER DEFAULT 0,
            losses       INTEGER DEFAULT 0,
            daily_last   INTEGER DEFAULT 0,
            referrer_id  BIGINT  DEFAULT NULL,
            clan_id      INTEGER DEFAULT NULL,
            created_at   INTEGER DEFAULT 0,
            updated_at   INTEGER DEFAULT 0,
            is_banned    BOOLEAN DEFAULT FALSE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transfers (
            id          SERIAL PRIMARY KEY,
            from_id     BIGINT,
            to_id       BIGINT,
            direction   TEXT NOT NULL,
            amount      INTEGER NOT NULL,
            note        TEXT DEFAULT '',
            created_at  INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clans (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            emoji       TEXT DEFAULT '⚔️',
            description TEXT DEFAULT '',
            owner_id    BIGINT,
            created_at  INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clan_members (
            clan_id   INTEGER NOT NULL,
            user_id   BIGINT  NOT NULL,
            role      TEXT    DEFAULT 'member',
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
    # Миграции
    for sql in [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS game_state TEXT DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS clan_id INTEGER DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE",
        "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS note TEXT DEFAULT ''",
    ]:
        try:
            cur.execute(sql); conn.commit()
        except Exception:
            conn.rollback()
    conn.commit(); cur.close(); conn.close()
    logger.info("DB init OK")


# ══════════════════════════════════════════════════════════════
# ПОЛЬЗОВАТЕЛИ
# ══════════════════════════════════════════════════════════════

@app.get("/api/game/check/{tg_id}")
def check_user(tg_id: int, username: str = ""):
    if tg_id == 0:
        return {"registered": True, "username": "guest"}
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id, username FROM users WHERE user_id=%s", (tg_id,))
    row = cur.fetchone()
    if not row:
        uname = username or f"id{tg_id}"
        cur.execute(
            """INSERT INTO users
               (user_id,username,balance,game_balance,game_state,xp,level,
                games_played,wins,losses,daily_last,created_at,updated_at,is_banned)
               VALUES (%s,%s,1000,0,NULL,0,1,0,0,0,0,%s,%s,FALSE)""",
            (tg_id, uname, int(time.time()), int(time.time()))
        )
        conn.commit()
        cur.close(); conn.close()
        return {"registered": True, "username": uname, "new": True}
    else:
        if username:
            cur.execute("UPDATE users SET username=%s WHERE user_id=%s", (username, tg_id))
            conn.commit()
    cur.close(); conn.close()
    return {"registered": True, "username": row["username"]}


@app.get("/api/game/load/{tg_id}")
def load_game_state(tg_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=%s", (tg_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        raise HTTPException(404, "Сначала напиши /start боту!")
    if row.get("is_banned"):
        raise HTTPException(403, "Аккаунт заблокирован.")

    db_game_balance = int(row.get("game_balance") or 0)

    state = None
    if row.get("game_state"):
        try:
            state = json.loads(row["game_state"])
        except Exception:
            state = None

    # Всегда синхронизируем coins из БД
    if state:
        state["coins"] = db_game_balance
        state["tgId"]  = tg_id
        state["username"] = row.get("username") or f"id{tg_id}"
    
    # Загрузить клан
    clan_data = None
    if row.get("clan_id"):
        conn2 = get_conn(); cur2 = conn2.cursor()
        cur2.execute("SELECT c.id, c.name, c.emoji, cm.role FROM clans c JOIN clan_members cm ON c.id=cm.clan_id WHERE cm.user_id=%s", (tg_id,))
        crow = cur2.fetchone()
        if crow:
            cur2.execute("""
                SELECT cm.user_id, cm.role, u.username, u.game_balance
                FROM clan_members cm JOIN users u ON cm.user_id=u.user_id
                WHERE cm.clan_id=%s ORDER BY u.game_balance DESC
            """, (crow["id"],))
            members = [
                {"user_id": m["user_id"], "role": m["role"],
                 "name": m["username"] or f"id{m['user_id']}",
                 "emoji": "⚔️", "coins": m["game_balance"] or 0}
                for m in cur2.fetchall() if m["user_id"] != tg_id
            ]
            clan_data = {"name": crow["name"], "emoji": crow["emoji"],
                         "role": crow["role"], "members": members, "clan_id": crow["id"]}
        cur2.close(); conn2.close()

    if state:
        state["clan"] = clan_data

    return {
        "found":           True,
        "state":           state,
        "db_game_balance": db_game_balance,
        "username":        row.get("username"),
        "user_id":         row.get("user_id"),
        "level":           row.get("level"),
        "xp":              row.get("xp"),
        "clan":            clan_data,
    }


class SaveStateRequest(BaseModel):
    tg_id: int
    state: dict
    coins: Optional[int] = None


@app.post("/api/game/save")
def save_game_state(req: SaveStateRequest):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id, is_banned, game_balance FROM users WHERE user_id=%s", (req.tg_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(404, "Пользователь не найден.")
    if row["is_banned"]:
        cur.close(); conn.close()
        raise HTTPException(403, "Аккаунт заблокирован.")

    db_game_balance = int(row["game_balance"] or 0)

    state_to_save = dict(req.state)
    new_coins = state_to_save.pop("coins", None)

    # Всегда обновляем game_balance с клиента (клиент — источник истины для расходов).
    # Исключение: если клиент прислал None или отрицательное — берём из БД.
    if new_coins is not None and int(new_coins) >= 0:
        db_game_balance = int(new_coins)
        cur.execute(
            "UPDATE users SET game_balance=%s, game_state=%s, updated_at=%s WHERE user_id=%s",
            (db_game_balance, json.dumps(state_to_save, ensure_ascii=False), int(time.time()), req.tg_id)
        )
    else:
        cur.execute(
            "UPDATE users SET game_state=%s, updated_at=%s WHERE user_id=%s",
            (json.dumps(state_to_save, ensure_ascii=False), int(time.time()), req.tg_id)
        )

    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "db_game_balance": db_game_balance}


# ══════════════════════════════════════════════════════════════
# ТОП
# ══════════════════════════════════════════════════════════════

@app.get("/api/top")
def top_players(limit: int = 30):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        """SELECT u.user_id, u.username, u.game_balance, u.level, c.name AS clan_name
           FROM users u
           LEFT JOIN clan_members cm ON u.user_id = cm.user_id
           LEFT JOIN clans c ON cm.clan_id = c.id
           WHERE u.is_banned=FALSE
           ORDER BY u.game_balance DESC NULLS LAST
           LIMIT %s""",
        (min(limit, 100),)
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return {
        "top": [
            {
                "user_id":      r["user_id"],
                "username":     r["username"] or f"id{r['user_id']}",
                "coins":        int(r["game_balance"] or 0),
                "game_balance": int(r["game_balance"] or 0),
                "level":        r["level"] or 1,
                "clan_name":    r["clan_name"] or None,
            }
            for r in rows
        ]
    }


@app.get("/api/rank/{user_id}")
def get_rank(user_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(404, "Не найден")
    bal = int(row["game_balance"] or 0)
    cur.execute(
        "SELECT COUNT(*) FROM users WHERE is_banned=FALSE AND COALESCE(game_balance,0)>%s",
        (bal,)
    )
    cnt = cur.fetchone()["count"]
    cur.close(); conn.close()
    return {"rank": cnt + 1, "game_balance": bal}


@app.api_route("/api/stats", methods=["GET","HEAD"])
def global_stats():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(game_balance) FROM users WHERE is_banned=FALSE")
    row = cur.fetchone()
    cur.close(); conn.close()
    return {"total_players": row["count"] or 0, "total_coins": int(row["sum"] or 0)}


# ══════════════════════════════════════════════════════════════
# ДЕПОЗИТ (игра → бот и бот → игра)
# ══════════════════════════════════════════════════════════════

class DepositRequest(BaseModel):
    tg_id: int
    amount: int


@app.post("/api/deposit")
def deposit_to_game(req: DepositRequest):
    """Из бот-баланса → в game_balance."""
    if req.amount <= 0:
        raise HTTPException(400, "Сумма должна быть > 0")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT balance, game_balance FROM users WHERE user_id=%s FOR UPDATE", (req.tg_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(404, "Пользователь не найден")
    if int(row["balance"] or 0) < req.amount:
        cur.close(); conn.close()
        raise HTTPException(400, f"Недостаточно монет. Баланс бота: {row['balance']}")
    new_bot  = int(row["balance"]) - req.amount
    new_game = int(row["game_balance"] or 0) + req.amount
    cur.execute(
        "UPDATE users SET balance=%s, game_balance=%s, updated_at=%s WHERE user_id=%s",
        (new_bot, new_game, int(time.time()), req.tg_id)
    )
    cur.execute(
        "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) VALUES (%s,%s,'deposit',%s,'Пополнение игры',%s)",
        (req.tg_id, req.tg_id, req.amount, int(time.time()))
    )
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "new_game_balance": new_game, "new_bot_balance": new_bot}


@app.post("/api/withdraw")
def withdraw_from_game(req: DepositRequest):
    """Из game_balance → в бот-баланс."""
    if req.amount <= 0:
        raise HTTPException(400, "Сумма должна быть > 0")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT balance, game_balance FROM users WHERE user_id=%s FOR UPDATE", (req.tg_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(404, "Пользователь не найден")
    game_bal = int(row["game_balance"] or 0)
    if game_bal < req.amount:
        cur.close(); conn.close()
        raise HTTPException(400, f"Недостаточно монет в игре. Баланс игры: {game_bal}")
    new_game = game_bal - req.amount
    new_bot  = int(row["balance"] or 0) + req.amount
    cur.execute(
        "UPDATE users SET balance=%s, game_balance=%s, updated_at=%s WHERE user_id=%s",
        (new_bot, new_game, int(time.time()), req.tg_id)
    )
    cur.execute(
        "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) VALUES (%s,%s,'withdraw',%s,'Вывод из игры',%s)",
        (req.tg_id, req.tg_id, req.amount, int(time.time()))
    )
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "new_game_balance": new_game, "new_bot_balance": new_bot}


# ══════════════════════════════════════════════════════════════
# КАЗИНО-БОТ (отправить из игры в казино и уведомить)
# ══════════════════════════════════════════════════════════════

class CasinoDepositRequest(BaseModel):
    tg_id: int
    amount: int


@app.post("/api/casino_deposit")
async def casino_deposit(req: CasinoDepositRequest):
    """Списать монеты из game_balance и зачислить в balance (бот) для казино."""
    if req.amount <= 0:
        raise HTTPException(400, "Сумма должна быть > 0")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT game_balance, balance, username FROM users WHERE user_id=%s FOR UPDATE", (req.tg_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(404, "Пользователь не найден")
    game_bal = int(row["game_balance"] or 0)
    bot_bal  = int(row["balance"] or 0)
    if game_bal < req.amount:
        cur.close(); conn.close()
        raise HTTPException(400, f"Недостаточно монет в игре. Баланс: {game_bal}")
    new_game = game_bal - req.amount
    new_bot  = bot_bal  + req.amount   # зачисляем в бот-баланс чтобы казино-игры работали
    cur.execute(
        "UPDATE users SET game_balance=%s, balance=%s, updated_at=%s WHERE user_id=%s",
        (new_game, new_bot, int(time.time()), req.tg_id)
    )
    cur.execute(
        "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) VALUES (%s,%s,'casino',%s,'Казино через игру',%s)",
        (req.tg_id, req.tg_id, req.amount, int(time.time()))
    )
    conn.commit(); cur.close(); conn.close()

    # Уведомить пользователя в Telegram
    asyncio.ensure_future(notify_user(
        req.tg_id,
        f"🎰 <b>Казино-бот!</b>\n\n"
        f"Зачислено: <b>{req.amount} монет</b> для игр\n"
        f"🎮 Остаток в игре: <b>{new_game} монет</b>\n"
        f"💰 Баланс казино: <b>{new_bot} монет</b>"
    ))
    return {"ok": True, "new_balance": new_game, "new_bot_balance": new_bot}


# ══════════════════════════════════════════════════════════════
# ТРЕЙД
# ══════════════════════════════════════════════════════════════

class TradeRequest(BaseModel):
    from_id: int
    amount: int
    fee: int = 0
    to_id: Optional[int] = None
    to_username: Optional[str] = None


@app.post("/api/trade")
async def trade_coins(req: TradeRequest):
    if req.amount <= 0:
        raise HTTPException(400, "Сумма должна быть > 0")
    conn = get_conn(); cur = conn.cursor()

    # Найти получателя
    if req.to_id:
        cur.execute("SELECT user_id, username FROM users WHERE user_id=%s", (req.to_id,))
    elif req.to_username:
        clean = req.to_username.lstrip("@").strip()
        cur.execute("SELECT user_id, username FROM users WHERE LOWER(username)=LOWER(%s)", (clean,))
    else:
        cur.close(); conn.close()
        raise HTTPException(400, "Укажите to_id или to_username")

    to_row = cur.fetchone()
    if not to_row:
        cur.close(); conn.close()
        raise HTTPException(404, "Получатель не найден")
    to_id   = to_row["user_id"]
    to_name = to_row["username"] or f"id{to_id}"

    if to_id == req.from_id:
        cur.close(); conn.close()
        raise HTTPException(400, "Нельзя переводить самому себе")

    total = req.amount + req.fee

    # Отправитель — блокируем строку
    cur.execute("SELECT game_balance, username FROM users WHERE user_id=%s FOR UPDATE", (req.from_id,))
    from_row = cur.fetchone()
    if not from_row:
        cur.close(); conn.close()
        raise HTTPException(404, "Отправитель не найден")
    from_game_bal = int(from_row["game_balance"] or 0)
    from_name     = from_row["username"] or f"id{req.from_id}"

    if from_game_bal < total:
        cur.close(); conn.close()
        raise HTTPException(400, f"Недостаточно монет в игре. Баланс: {from_game_bal}")

    # Провести транзакцию
    cur.execute(
        "UPDATE users SET game_balance=game_balance-%s, updated_at=%s WHERE user_id=%s",
        (total, int(time.time()), req.from_id)
    )
    cur.execute(
        "UPDATE users SET game_balance=game_balance+%s, updated_at=%s WHERE user_id=%s",
        (req.amount, int(time.time()), to_id)
    )
    cur.execute(
        "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) VALUES (%s,%s,'trade',%s,'Трейд',%s)",
        (req.from_id, to_id, req.amount, int(time.time()))
    )
    conn.commit()

    # Новый баланс отправителя
    cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (req.from_id,))
    new_bal = int(cur.fetchone()["game_balance"] or 0)
    cur.close(); conn.close()

    # Уведомить получателя
    asyncio.ensure_future(notify_user(
        to_id,
        f"💰 <b>Пополнение игрового баланса!</b>\n\n"
        f"От: <b>@{from_name}</b>\n"
        f"Сумма: <b>+{req.amount} монет</b>\n\n"
        f"🎮 Монеты зачислены в ваш игровой баланс."
    ))

    return {"ok": True, "new_balance": new_bal, "to_username": to_name}


# ── История переводов ────────────────────────────────────────

@app.get("/api/history/{tg_id}")
def get_history(tg_id: int, limit: int = 30):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT t.id, t.from_id, t.to_id, t.direction, t.amount, t.note, t.created_at,
               u1.username AS from_username, u2.username AS to_username
        FROM transfers t
        LEFT JOIN users u1 ON t.from_id=u1.user_id
        LEFT JOIN users u2 ON t.to_id=u2.user_id
        WHERE t.from_id=%s OR t.to_id=%s
        ORDER BY t.created_at DESC LIMIT %s
    """, (tg_id, tg_id, min(limit, 100)))
    rows = cur.fetchall()
    cur.close(); conn.close()
    result = []
    for r in rows:
        is_sent = r["from_id"] == tg_id
        result.append({
            "id":        r["id"],
            "type":      "sent" if is_sent else "received",
            "direction": r["direction"],
            "amount":    r["amount"],
            "note":      r["note"] or "",
            "date":      r["created_at"],
            "counterpart": (r["to_username"] or f"id{r['to_id']}") if is_sent else (r["from_username"] or f"id{r['from_id']}"),
            "isBotTransfer": r["direction"] in ("casino", "deposit", "withdraw"),
        })
    return {"ok": True, "history": result}


# ══════════════════════════════════════════════════════════════
# КЛАНЫ
# ══════════════════════════════════════════════════════════════

@app.get("/api/clans/top")
def clans_top(limit: int = 20):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.name, c.emoji,
               COUNT(cm.user_id) AS member_count,
               COALESCE(SUM(u.game_balance),0) AS total_balance
        FROM clans c
        LEFT JOIN clan_members cm ON c.id=cm.clan_id
        LEFT JOIN users u ON cm.user_id=u.user_id
        GROUP BY c.id, c.name, c.emoji
        ORDER BY total_balance DESC
        LIMIT %s
    """, (min(limit, 50),))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return {"ok": True, "clans": [dict(r) for r in rows]}


@app.get("/api/clans/members")
def clan_members(name: str = Query(...)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id FROM clans WHERE LOWER(name)=LOWER(%s)", (name,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(404, f"Клан «{name}» не найден")
    clan_id = row["id"]
    cur.execute("""
        SELECT cm.user_id, cm.role, u.username, u.game_balance
        FROM clan_members cm JOIN users u ON cm.user_id=u.user_id
        WHERE cm.clan_id=%s ORDER BY u.game_balance DESC
    """, (clan_id,))
    members = [{"user_id": r["user_id"], "role": r["role"],
                "username": r["username"] or f"id{r['user_id']}",
                "game_balance": int(r["game_balance"] or 0)} for r in cur.fetchall()]
    cur.close(); conn.close()
    return {"ok": True, "clan_id": clan_id, "members": members}


class ClanCreateRequest(BaseModel):
    tg_id: int
    name: str
    emoji: str = "⚔️"


@app.post("/api/clans/create")
def clan_create(req: ClanCreateRequest):
    name = req.name.strip()
    if not name or len(name) < 2 or len(name) > 20:
        raise HTTPException(400, "Длина названия: 2–20 символов")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id FROM clans WHERE LOWER(name)=LOWER(%s)", (name,))
    if cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(400, "Клан с таким именем уже существует")
    cur.execute("SELECT clan_id FROM users WHERE user_id=%s", (req.tg_id,))
    u = cur.fetchone()
    if u and u["clan_id"]:
        cur.close(); conn.close()
        raise HTTPException(400, "Вы уже состоите в клане")
    cur.execute(
        "INSERT INTO clans (name,emoji,owner_id,created_at) VALUES (%s,%s,%s,%s) RETURNING id",
        (name, req.emoji, req.tg_id, int(time.time()))
    )
    clan_id = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO clan_members (clan_id,user_id,role,joined_at) VALUES (%s,%s,'leader',%s)",
        (clan_id, req.tg_id, int(time.time()))
    )
    cur.execute("UPDATE users SET clan_id=%s WHERE user_id=%s", (clan_id, req.tg_id))
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "clan_id": clan_id, "name": name}


class ClanJoinRequest(BaseModel):
    tg_id: int
    name: str


@app.post("/api/clans/join")
def clan_join(req: ClanJoinRequest):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, name, emoji FROM clans WHERE LOWER(name)=LOWER(%s)", (req.name.strip(),))
    clan = cur.fetchone()
    if not clan:
        cur.close(); conn.close()
        raise HTTPException(404, f"Клан «{req.name}» не найден")
    cur.execute("SELECT clan_id FROM users WHERE user_id=%s", (req.tg_id,))
    u = cur.fetchone()
    if u and u["clan_id"]:
        cur.close(); conn.close()
        raise HTTPException(400, "Вы уже состоите в клане")
    clan_id = clan["id"]
    cur.execute(
        "INSERT INTO clan_members (clan_id,user_id,role,joined_at) VALUES (%s,%s,'member',%s) ON CONFLICT DO NOTHING",
        (clan_id, req.tg_id, int(time.time()))
    )
    cur.execute("UPDATE users SET clan_id=%s WHERE user_id=%s", (clan_id, req.tg_id))
    conn.commit()
    # Вернуть участников
    cur.execute("""
        SELECT cm.user_id, cm.role, u.username, u.game_balance
        FROM clan_members cm JOIN users u ON cm.user_id=u.user_id
        WHERE cm.clan_id=%s ORDER BY u.game_balance DESC
    """, (clan_id,))
    members = [{"user_id": r["user_id"], "role": r["role"],
                "name": r["username"] or f"id{r['user_id']}",
                "emoji": "⚔️", "coins": int(r["game_balance"] or 0)} for r in cur.fetchall()]
    cur.close(); conn.close()
    return {"ok": True, "clan_id": clan_id, "name": clan["name"], "emoji": clan["emoji"], "members": members}


class ClanLeaveRequest(BaseModel):
    tg_id: int


@app.post("/api/clans/leave")
def clan_leave(req: ClanLeaveRequest):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT clan_id, role FROM clan_members WHERE user_id=%s", (req.tg_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(400, "Вы не состоите в клане")
    clan_id, role = row["clan_id"], row["role"]
    cur.execute("DELETE FROM clan_members WHERE user_id=%s", (req.tg_id,))
    cur.execute("UPDATE users SET clan_id=NULL WHERE user_id=%s", (req.tg_id,))
    if role == "leader":
        cur.execute("SELECT user_id FROM clan_members WHERE clan_id=%s LIMIT 1", (clan_id,))
        nxt = cur.fetchone()
        if nxt:
            cur.execute("UPDATE clan_members SET role='leader' WHERE user_id=%s", (nxt["user_id"],))
            cur.execute("UPDATE clans SET owner_id=%s WHERE id=%s", (nxt["user_id"], clan_id))
        else:
            cur.execute("DELETE FROM clans WHERE id=%s", (clan_id,))
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════
# ПРОФИЛЬ
# ══════════════════════════════════════════════════════════════

@app.get("/api/user/{user_id}")
def get_user(user_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        raise HTTPException(404, "Пользователь не найден")
    r = dict(row)
    r.pop("game_state", None)  # не отдавать весь state
    return r


class ClanKickRequest(BaseModel):
    tg_id: int      # кто кикает (должен быть лидером)
    kick_id: int    # кого кикать


@app.post("/api/clans/kick")
def clan_kick(req: ClanKickRequest):
    conn = get_conn(); cur = conn.cursor()
    # Проверить что tg_id — лидер
    cur.execute("SELECT role, clan_id FROM clan_members WHERE user_id=%s", (req.tg_id,))
    r = cur.fetchone()
    if not r or r["role"] != "leader":
        cur.close(); conn.close()
        raise HTTPException(403, "Только лидер может исключать участников")
    clan_id = r["clan_id"]
    # Проверить что kick_id в том же клане
    cur.execute("SELECT role FROM clan_members WHERE user_id=%s AND clan_id=%s", (req.kick_id, clan_id))
    kr = cur.fetchone()
    if not kr:
        cur.close(); conn.close()
        raise HTTPException(404, "Участник не найден в клане")
    if kr["role"] == "leader":
        cur.close(); conn.close()
        raise HTTPException(400, "Нельзя исключить лидера")
    cur.execute("DELETE FROM clan_members WHERE user_id=%s AND clan_id=%s", (req.kick_id, clan_id))
    cur.execute("UPDATE users SET clan_id=NULL WHERE user_id=%s", (req.kick_id,))
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}
