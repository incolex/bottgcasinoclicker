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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_data (
            user_id        BIGINT PRIMARY KEY,
            streak_days    INTEGER DEFAULT 0,
            streak_last_ts INTEGER DEFAULT 0,
            quests_json    TEXT    DEFAULT '{}',
            quests_date    TEXT    DEFAULT '',
            updated_at     INTEGER DEFAULT 0
        )
    """)
    # Миграции
    for sql in [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS game_state TEXT DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS clan_id INTEGER DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE",
        "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS note TEXT DEFAULT ''",
        # transfers.user_id — старая колонка NOT NULL, мешает INSERT (from_id/to_id — новая схема)
        "ALTER TABLE transfers DROP COLUMN IF EXISTS user_id",
        # Убедимся что from_id и to_id есть
        "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS from_id BIGINT",
        "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS to_id BIGINT",
        # Анти-кликер
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS click_ban_until INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS click_ban_reason TEXT DEFAULT ''",
        # Бан за неактивность
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS inactive_banned BOOLEAN DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS inactive_ban_ts INTEGER DEFAULT 0",
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

    click_ban_until = int(row.get("click_ban_until") or 0)
    click_ban_reason = row.get("click_ban_reason") or ""
    # Если бан истёк — сбросить
    if click_ban_until > 0 and click_ban_until < int(time.time()):
        click_ban_until = 0
        click_ban_reason = ""
        try:
            conn2b = get_conn(); cur2b = conn2b.cursor()
            cur2b.execute("UPDATE users SET click_ban_until=0, click_ban_reason='' WHERE user_id=%s", (tg_id,))
            conn2b.commit(); cur2b.close(); conn2b.close()
        except Exception: pass

    return {
        "found":             True,
        "state":             state,
        "db_game_balance":   db_game_balance,
        "username":          row.get("username"),
        "user_id":           row.get("user_id"),
        "level":             row.get("level"),
        "xp":                row.get("xp"),
        "clan":              clan_data,
        "click_ban_until":   click_ban_until,
        "click_ban_reason":  click_ban_reason,
        "inactive_banned":   bool(row.get("inactive_banned")),
        "inactive_ban_ts":   int(row.get("inactive_ban_ts") or 0),
    }


class SaveStateRequest(BaseModel):
    tg_id: int
    state: dict
    coins: Optional[int] = None
    delta: Optional[int] = None
    exact: Optional[bool] = False  # True = клиент намеренно потратил (апгрейд/трейд), принять как точный баланс   # монеты накопленные с последнего сохранения


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
    state_to_save.pop("coins", None)   # coins хранятся в game_balance, не в JSON

    # Считаем новый баланс — безопасная логика без потерь:
    client_coins = int(req.coins) if req.coins is not None else None
    client_delta = int(req.delta) if req.delta is not None else 0

    if client_delta > 0:
        # БД — источник истины. Admin мог уменьшить баланс пока игрок играл.
        # Берём db_game_balance как базу; client_base только если db=0 (новый игрок)
        client_base = (client_coins - client_delta) if client_coins is not None else 0
        if db_game_balance == 0 and client_base > 0:
            base = client_base  # новый игрок, первый клик
        else:
            base = db_game_balance  # всегда доверяем БД
        new_balance = base + client_delta
    elif req.exact and client_coins is not None and client_coins >= 0:
        # Точное списание (апгрейд, трейд) — клиент уже вычел монеты, доверяем ему
        # Защита от обнуления: не даём уйти ниже 0
        new_balance = max(0, client_coins)
    elif client_coins is not None and client_coins >= 0:
        # Обычная синхронизация без delta — берём max чтобы не потерять /ocp пополнение
        if client_coins == 0 and db_game_balance > 0:
            new_balance = db_game_balance  # не обнуляем!
        elif db_game_balance > client_coins:
            new_balance = db_game_balance  # был внешний депозит, сохраняем
        else:
            new_balance = client_coins
    else:
        new_balance = db_game_balance

    # Никогда не уходим в минус
    new_balance = max(0, new_balance)

    cur.execute(
        "UPDATE users SET game_balance=%s, game_state=%s, updated_at=%s WHERE user_id=%s",
        (new_balance, json.dumps(state_to_save, ensure_ascii=False), int(time.time()), req.tg_id)
    )
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "db_game_balance": new_balance}


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
    cnt_row = cur.fetchone()
    cnt = cnt_row["count"] if cnt_row else 0
    cur.close(); conn.close()
    return {"rank": cnt + 1, "game_balance": bal}


@app.api_route("/api/stats", methods=["GET","HEAD"])
def global_stats():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(game_balance) FROM users WHERE is_banned=FALSE")
    row = cur.fetchone()
    cur.close(); conn.close()
    return {"total_players": row["count"] or 0, "total_coins": int(row["sum"] or 0)}


# Таймер сезона хранится в БД (таблица settings), не в памяти — переживает рестарты Render

def _get_season_ts(cur) -> int:
    try:
        cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("SELECT value FROM settings WHERE key='season_end_ts'")
        row = cur.fetchone()
        if not row:
            return 0
        val = row["value"] if isinstance(row, dict) else row[0]
        return int(val) if val else 0
    except Exception as e:
        logger.warning(f"_get_season_ts error: {e}")
        return 0

def _set_season_ts(cur, ts: int):
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute(
        "INSERT INTO settings(key,value) VALUES('season_end_ts',%s) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        (str(ts),)
    )

@app.get("/api/season")
def get_season_info():
    """Возвращает информацию о текущем сезоне: таймер и топ-3."""
    conn = get_conn(); cur = conn.cursor()
    end_ts = _get_season_ts(cur)
    cur.execute("""
        SELECT user_id, username, game_balance
        FROM users WHERE is_banned=FALSE
        ORDER BY game_balance DESC NULLS LAST LIMIT 3
    """)
    top3 = [{"rank": i+1, "username": r["username"] or f"id{r['user_id']}",
              "user_id": r["user_id"], "game_balance": int(r["game_balance"] or 0)}
            for i, r in enumerate(cur.fetchall())]
    try:
        cur.execute("SELECT COALESCE(MAX(season_num),0)+1 AS season_num FROM season_archive")
        season_num = cur.fetchone()["season_num"]
    except Exception:
        season_num = 1
    cur.close(); conn.close()
    # Считаем сколько дней назад начался сезон
    try:
        import datetime as _dt
        # Берём самую старую запись в users как начало игры/сезона
        conn2 = get_conn(); cur2 = conn2.cursor()
        cur2.execute("SELECT MIN(created_at) AS oldest FROM users")
        row2 = cur2.fetchone()
        cur2.close(); conn2.close()
        oldest = int(row2["oldest"] or 0) if row2 else 0
        started_ago = max(0, int((time.time() - oldest) / 86400)) if oldest > 0 else 0
    except Exception:
        started_ago = 0
    return {
        "season_num": season_num,
        "end_ts": end_ts,
        "top3": top3,
        "rewards": [30, 20, 10],
        "started_ago": started_ago,
    }


class SeasonSetRequest(BaseModel):
    end_ts: int
    secret: str = ""

@app.post("/api/season/set")
def set_season_timer(req: SeasonSetRequest):
    """Устанавливает таймер конца сезона (вызывается из бота)."""
    conn = get_conn(); cur = conn.cursor()
    _set_season_ts(cur, req.end_ts)
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "end_ts": req.end_ts}


class SeasonRewardRequest(BaseModel):
    user_id: int
    diamonds: int

@app.post("/api/season/reward")
def give_season_reward(req: SeasonRewardRequest):
    """Начислить алмазы победителю сезона."""
    if req.diamonds <= 0:
        raise HTTPException(400, "diamonds must be > 0")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT game_state FROM users WHERE user_id=%s", (req.user_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(404, "Пользователь не найден")
    state = {}
    if row["game_state"]:
        try: state = json.loads(row["game_state"])
        except: pass
    state["diamonds"] = int(state.get("diamonds") or 0) + req.diamonds
    cur.execute("UPDATE users SET game_state=%s WHERE user_id=%s",
                (json.dumps(state, ensure_ascii=False), req.user_id))
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "new_diamonds": state["diamonds"]}


# ══════════════════════════════════════════════════════════════
# АНТИ-КЛИКЕР — сохранить бан
# ══════════════════════════════════════════════════════════════

class ClickBanRequest(BaseModel):
    tg_id: int
    reason: str = ""
    ban_until: Optional[int] = None  # unix timestamp, если None — ставим +20 мин

@app.post("/api/game/click_ban")
def set_click_ban(req: ClickBanRequest):
    ban_until = req.ban_until if req.ban_until else int(time.time()) + 20 * 60
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=%s", (req.tg_id,))
    if not cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(404, "Пользователь не найден")
    cur.execute(
        "UPDATE users SET click_ban_until=%s, click_ban_reason=%s WHERE user_id=%s",
        (ban_until, req.reason[:500], req.tg_id)
    )
    conn.commit()
    # Лог
    logger.warning(f"[ANTI-CLICKER] tg_id={req.tg_id} ban_until={ban_until} reason={req.reason}")
    cur.close(); conn.close()
    return {"ok": True, "ban_until": ban_until, "ban_until_readable": time.strftime("%H:%M:%S %d.%m", time.localtime(ban_until))}

@app.get("/api/game/click_ban/{tg_id}")
def get_click_ban(tg_id: int):
    """Проверить бан кликера."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT click_ban_until, click_ban_reason FROM users WHERE user_id=%s", (tg_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        raise HTTPException(404, "Не найден")
    ban_until = int(row["click_ban_until"] or 0)
    active = ban_until > int(time.time())
    return {
        "active": active,
        "ban_until": ban_until if active else 0,
        "reason": row["click_ban_reason"] or "",
        "seconds_left": max(0, ban_until - int(time.time())) if active else 0,
    }

# ══════════════════════════════════════════════════════════════
# БАН ЗА НЕАКТИВНОСТЬ
# ══════════════════════════════════════════════════════════════

class InactiveBanCheckRequest(BaseModel):
    tg_id: int

@app.post("/api/game/check_inactive_ban")
def check_inactive_ban(req: InactiveBanCheckRequest):
    """Проверить и выставить бан за неактивность (нет streak 3+ дней)."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT inactive_banned, inactive_ban_ts FROM users WHERE user_id=%s", (req.tg_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return {"inactive_banned": False}
    cur.close(); conn.close()
    return {
        "inactive_banned": bool(row["inactive_banned"]),
        "inactive_ban_ts": int(row["inactive_ban_ts"] or 0),
    }

@app.post("/api/game/self_unban")
async def self_unban(req: InactiveBanCheckRequest):
    """Игрок сам снимает бан за неактивность (кнопка в игре)."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT inactive_banned, username FROM users WHERE user_id=%s", (req.tg_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(404, "Не найден")
    if not row["inactive_banned"]:
        cur.close(); conn.close()
        return {"ok": True, "already_active": True}
    cur.execute(
        "UPDATE users SET inactive_banned=FALSE, inactive_ban_ts=0 WHERE user_id=%s",
        (req.tg_id,)
    )
    conn.commit(); cur.close(); conn.close()
    logger.info(f"[INACTIVE-UNBAN] user {req.tg_id} ({row['username']}) self-unbanned")
    return {"ok": True}

@app.post("/api/game/apply_inactive_bans")
def apply_inactive_bans():
    """Забанить игроков без streak 3+ дней (вызывается по расписанию или вручную)."""
    import datetime as _dt
    conn = get_conn(); cur = conn.cursor()
    # Найти игроков у кого streak_last_ts старше 3 дней и они не забанены принудительно
    cutoff = int(time.time()) - 3 * 86400
    cur.execute("""
        SELECT u.user_id FROM users u
        LEFT JOIN daily_data d ON u.user_id = d.user_id
        WHERE u.is_banned = FALSE
          AND u.inactive_banned = FALSE
          AND (d.streak_last_ts IS NULL OR d.streak_last_ts < %s)
          AND u.created_at < %s
    """, (cutoff, cutoff))
    to_ban = [r["user_id"] for r in cur.fetchall()]
    if to_ban:
        cur.execute(
            "UPDATE users SET inactive_banned=TRUE, inactive_ban_ts=%s WHERE user_id=ANY(%s)",
            (int(time.time()), to_ban)
        )
        conn.commit()
    cur.close(); conn.close()
    logger.info(f"[INACTIVE-BAN] Applied to {len(to_ban)} users")
    return {"ok": True, "banned_count": len(to_ban), "user_ids": to_ban}

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
    else:
        # Если не лидер — всё равно проверить не остался ли клан пустым
        cur.execute("SELECT COUNT(*) AS cnt FROM clan_members WHERE clan_id=%s", (clan_id,))
        cnt_row = cur.fetchone()
        if not cnt_row or int(cnt_row["cnt"]) == 0:
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

# ══════════════════════════════════════════════════════════════
# STREAK & DAILY QUESTS — хранение в БД
# ══════════════════════════════════════════════════════════════

class StreakClaimRequest(BaseModel):
    tg_id: int

@app.get("/api/daily/{tg_id}")
def get_daily_data(tg_id: int):
    """Загрузить стрик и задания из БД."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM daily_data WHERE user_id=%s", (tg_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return {"ok": True, "streak_days": 0, "streak_last_ts": 0, "quests_json": "{}", "quests_date": ""}
    return {
        "ok": True,
        "streak_days":    int(row["streak_days"] or 0),
        "streak_last_ts": int(row["streak_last_ts"] or 0),
        "quests_json":    row["quests_json"] or "{}",
        "quests_date":    row["quests_date"] or "",
    }

class DailySaveRequest(BaseModel):
    tg_id: int
    streak_days: Optional[int] = None
    streak_last_ts: Optional[int] = None
    quests_json: Optional[str] = None
    quests_date: Optional[str] = None

@app.post("/api/daily/save")
def save_daily_data(req: DailySaveRequest):
    """Сохранить стрик и задания в БД (upsert)."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM daily_data WHERE user_id=%s", (req.tg_id,))
    exists = cur.fetchone()
    now = int(time.time())
    if exists:
        fields, vals = [], []
        if req.streak_days is not None:    fields.append("streak_days=%s");    vals.append(req.streak_days)
        if req.streak_last_ts is not None: fields.append("streak_last_ts=%s"); vals.append(req.streak_last_ts)
        if req.quests_json is not None:    fields.append("quests_json=%s");    vals.append(req.quests_json)
        if req.quests_date is not None:    fields.append("quests_date=%s");    vals.append(req.quests_date)
        if fields:
            fields.append("updated_at=%s"); vals.append(now); vals.append(req.tg_id)
            cur.execute(f"UPDATE daily_data SET {','.join(fields)} WHERE user_id=%s", vals)
    else:
        cur.execute(
            "INSERT INTO daily_data (user_id,streak_days,streak_last_ts,quests_json,quests_date,updated_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (req.tg_id,
             req.streak_days or 0, req.streak_last_ts or 0,
             req.quests_json or "{}", req.quests_date or "", now)
        )
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}

@app.post("/api/streak/claim")
async def claim_streak(req: StreakClaimRequest):
    """Забрать ежедневный стрик с проверкой на сервере."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM daily_data WHERE user_id=%s", (req.tg_id,))
    row = cur.fetchone()
    now = int(time.time())

    # Текущая дата (UTC) как строка
    import datetime as _dt
    today = _dt.date.today().isoformat()  # "2025-01-15"

    streak_days    = int(row["streak_days"] or 0) if row else 0
    streak_last_ts = int(row["streak_last_ts"] or 0) if row else 0
    quests_json    = row["quests_json"] if row else "{}"
    quests_date    = row["quests_date"] if row else ""

    # Проверка: уже получено сегодня?
    last_date = _dt.date.fromtimestamp(streak_last_ts).isoformat() if streak_last_ts > 0 else ""
    if last_date == today:
        cur.close(); conn.close()
        raise HTTPException(400, "Бонус уже получен сегодня")

    # Серия: если пропущено более 1 дня — сброс
    days_since = (now - streak_last_ts) / 86400 if streak_last_ts > 0 else 999
    if streak_last_ts == 0 or days_since > 2:
        new_days = 1
    else:
        new_days = streak_days + 1

    # Награда на основе ПРЕДЫДУЩИХ дней
    bonus = (streak_days) * 50
    min_r = 1000 + bonus
    max_r = 17500 + bonus
    import random as _rnd
    reward = _rnd.randint(min_r, max_r)

    # Обновить стрик в БД
    if row:
        cur.execute(
            "UPDATE daily_data SET streak_days=%s,streak_last_ts=%s,updated_at=%s WHERE user_id=%s",
            (new_days, now, now, req.tg_id)
        )
    else:
        cur.execute(
            "INSERT INTO daily_data (user_id,streak_days,streak_last_ts,quests_json,quests_date,updated_at) VALUES (%s,%s,%s,'{}','',%s)",
            (req.tg_id, new_days, now, now)
        )

    # Начислить монеты
    cur.execute("UPDATE users SET game_balance=game_balance+%s WHERE user_id=%s", (reward, req.tg_id))
    cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (req.tg_id,))
    new_bal_row = cur.fetchone()
    new_balance = int(new_bal_row["game_balance"]) if new_bal_row else 0
    conn.commit(); cur.close(); conn.close()

    return {
        "ok": True,
        "reward": reward,
        "streak_days": new_days,
        "streak_last_ts": now,
        "new_balance": new_balance,
    }

@app.post("/api/daily/quest_claim")
def claim_quest(req: dict):
    """Забрать награду за ежедневное задание."""
    tg_id   = int(req.get("tg_id", 0))
    quest_id = str(req.get("quest_id", ""))
    reward   = int(req.get("reward", 0))
    if not tg_id or not quest_id or reward <= 0:
        raise HTTPException(400, "Bad request")

    import datetime as _dt
    today = _dt.date.today().isoformat()

    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT quests_json, quests_date FROM daily_data WHERE user_id=%s", (tg_id,))
    row = cur.fetchone()

    quests = {}
    if row:
        qdate = row["quests_date"] or ""
        if qdate == today:
            try: quests = json.loads(row["quests_json"] or "{}")
            except: quests = {}
        # else: новый день, сбрасываем

    if quests.get(quest_id):
        cur.close(); conn.close()
        raise HTTPException(400, "Задание уже выполнено")

    quests[quest_id] = True
    now = int(time.time())
    quests_json = json.dumps(quests)

    if row:
        cur.execute(
            "UPDATE daily_data SET quests_json=%s,quests_date=%s,updated_at=%s WHERE user_id=%s",
            (quests_json, today, now, tg_id)
        )
    else:
        cur.execute(
            "INSERT INTO daily_data (user_id,streak_days,streak_last_ts,quests_json,quests_date,updated_at) VALUES (%s,0,0,%s,%s,%s)",
            (tg_id, quests_json, today, now)
        )

    cur.execute("UPDATE users SET game_balance=game_balance+%s WHERE user_id=%s", (reward, tg_id))
    cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (tg_id,))
    nb = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "new_balance": int(nb["game_balance"]) if nb else 0}

# ── Топ с полем streak ────────────────────────────────────────
@app.get("/api/top/extended")
def top_extended(limit: int = 30):
    """Топ игроков с серией дней."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        """SELECT u.user_id, u.username, u.game_balance, u.level,
                  c.name AS clan_name,
                  COALESCE(d.streak_days, 0) AS streak_days
           FROM users u
           LEFT JOIN clan_members cm ON u.user_id = cm.user_id
           LEFT JOIN clans c ON cm.clan_id = c.id
           LEFT JOIN daily_data d ON u.user_id = d.user_id
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
                "streak_days":  int(r["streak_days"] or 0),
            }
            for r in rows
        ]
    }

# ══════════════════════════════════════════════════════════════
# ADMIN INACTIVE UNBAN (вызывается из OCP бота)
# ══════════════════════════════════════════════════════════════

class AdminUnbanInactiveRequest(BaseModel):
    tg_id: int  # кого разбанить

@app.post("/api/game/admin_unban_inactive")
def admin_unban_inactive(req: AdminUnbanInactiveRequest):
    """Снять бан за неактивность через OCP."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        "UPDATE users SET inactive_banned=FALSE, inactive_ban_ts=0 WHERE user_id=%s",
        (req.tg_id,)
    )
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}
