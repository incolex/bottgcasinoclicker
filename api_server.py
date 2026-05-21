# api_server.py — FastAPI сервер для GOLDCLICK Bot
# Все данные хранятся в PostgreSQL, никакой локальной логики в HTML

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import psycopg2
import psycopg2.extras
import psycopg2.pool
import json
import time
import os
import httpx
import asyncio
import logging
import datetime
import random
import math
import hmac
import hashlib
from urllib.parse import parse_qsl
from collections import defaultdict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # e.g. https://your-app.onrender.com
_bot_app = None  # global PTB Application when running in webhook mode


async def _cleanup_empty_clans_job():
    """Периодически удаляет кланы без участников."""
    while True:
        await asyncio.sleep(300)  # каждые 5 минут
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("""
                DELETE FROM clans WHERE id NOT IN (
                    SELECT DISTINCT clan_id FROM clan_members WHERE clan_id IS NOT NULL
                )
            """)
            deleted = cur.rowcount
            if deleted > 0:
                cur.execute("UPDATE users SET clan_id=NULL WHERE clan_id NOT IN (SELECT id FROM clans)")
                logger.info(f"[CLEANUP] Deleted {deleted} empty clans")
            conn.commit()
            cur.close()
            release_conn(conn)
        except Exception as e:
            logger.warning(f"cleanup_empty_clans error: {e}")


@asynccontextmanager
async def lifespan(app_: "FastAPI"):
    init_db_tables()
    # Start empty-clan cleanup job
    asyncio.ensure_future(_cleanup_empty_clans_job())
    # Webhook mode: set Telegram webhook and initialize PTB app
    if WEBHOOK_URL and BOT_TOKEN:
        try:
            import main as bot_main
            global _bot_app
            _bot_app = bot_main.build_application()
            await _bot_app.initialize()
            await _bot_app.start()
            webhook_addr = f"{WEBHOOK_URL.rstrip('/')}/tg-webhook"
            await _bot_app.bot.set_webhook(
                url=webhook_addr,
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query"],
            )
            logger.info(f"[WEBHOOK] Set: {webhook_addr}")
        except Exception as e:
            logger.error(f"[WEBHOOK] Setup failed: {e}")
    yield
    if _bot_app:
        try:
            await _bot_app.stop()
            await _bot_app.shutdown()
        except Exception:
            pass

app = FastAPI(title="GOLDCLICK API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# ══════════════════════════════════════════════════════════════
# FIX #7: Connection Pool — вместо нового соединения на каждый запрос
# ══════════════════════════════════════════════════════════════

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def _build_dsn() -> str:
    import re
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    url = re.sub(r"[?&]sslmode=[^&]*", "", url)
    return url


def init_pool():
    global _pool
    dsn = _build_dsn()
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=20,
        dsn=dsn,
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    logger.info("Connection pool initialized")


def get_conn():
    if _pool is None:
        # Fallback: прямое соединение если пул ещё не инициализирован
        import re
        url = _build_dsn()
        return psycopg2.connect(url, sslmode="require", cursor_factory=psycopg2.extras.RealDictCursor)
    return _pool.getconn()


def release_conn(conn):
    if _pool is not None:
        try:
            _pool.putconn(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
    else:
        try:
            conn.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# FIX #1: Telegram initData verification
# ══════════════════════════════════════════════════════════════

def verify_tg_init_data(init_data: str) -> Optional[dict]:
    """
    Проверяет подпись Telegram WebApp initData.
    Возвращает dict с данными пользователя или None при ошибке.
    """
    if not BOT_TOKEN or not init_data:
        return None
    try:
        vals = dict(parse_qsl(init_data, strict_parsing=True))
        check_hash = vals.pop("hash", None)
        if not check_hash:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(vals.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, check_hash):
            return None
        # Извлекаем user из JSON-поля
        user_str = vals.get("user", "{}")
        user = json.loads(user_str)
        return user
    except Exception as e:
        logger.warning(f"verify_tg_init_data error: {e}")
        return None


def get_verified_tg_id(init_data: Optional[str], claimed_tg_id: int) -> Optional[int]:
    """
    Проверяет initData и возвращает tg_id если он совпадает с заявленным.
    Если BOT_TOKEN не задан (dev режим) — пропускает проверку.
    Возвращает None если проверка провалена.
    """
    if not BOT_TOKEN:
        # Dev-режим: BOT_TOKEN не задан, пропускаем проверку
        return claimed_tg_id
    if not init_data:
        # Нет initData — отклоняем для продакшна
        return None
    user = verify_tg_init_data(init_data)
    if user is None:
        return None
    real_id = int(user.get("id", 0))
    if real_id != claimed_tg_id:
        return None
    return real_id


# ══════════════════════════════════════════════════════════════
# FIX #4: Rate limiting для /api/game/save
# ══════════════════════════════════════════════════════════════

_save_rate_limits: dict = defaultdict(list)  # {tg_id: [timestamps]}
_MAX_SAVES_PER_MINUTE = 40
# Суммарный delta за минуту на пользователя
_delta_rate_limits: dict = defaultdict(list)  # {tg_id: [(ts, delta)]}
_MAX_DELTA_PER_MINUTE = 999_999_999_999  # без ограничения


def check_save_rate_limit(tg_id: int, delta: int) -> bool:
    """Возвращает True если запрос разрешён, False если превышен лимит."""
    now = time.time()
    window_start = now - 60

    # Чистим старые записи
    _save_rate_limits[tg_id] = [t for t in _save_rate_limits[tg_id] if t > window_start]
    _delta_rate_limits[tg_id] = [(t, d) for t, d in _delta_rate_limits[tg_id] if t > window_start]

    # Проверяем количество запросов
    if len(_save_rate_limits[tg_id]) >= _MAX_SAVES_PER_MINUTE:
        logger.warning(f"[RATE_LIMIT] tg_id={tg_id} too many saves: {len(_save_rate_limits[tg_id])}/min")
        return False

    # Проверяем суммарный delta
    total_delta = sum(d for _, d in _delta_rate_limits[tg_id]) + delta
    if total_delta > _MAX_DELTA_PER_MINUTE:
        logger.warning(f"[RATE_LIMIT] tg_id={tg_id} delta overflow: {total_delta}/min")
        return False

    # Записываем
    _save_rate_limits[tg_id].append(now)
    if delta > 0:
        _delta_rate_limits[tg_id].append((now, delta))
    return True


# ══════════════════════════════════════════════════════════════
# FIX #11: Server-side upgrade stats recalculation
# ══════════════════════════════════════════════════════════════

DEFS_SERVER = [
    {"id": "c1", "type": "click", "val": 1,    "base": 8,     "mult": 1.25, "max": 10, "cat": "std"},
    {"id": "c2", "type": "click", "val": 2,    "base": 40,    "mult": 1.28, "max": 10, "cat": "std"},
    {"id": "c3", "type": "click", "val": 4,    "base": 150,   "mult": 1.30, "max": 10, "cat": "std"},
    {"id": "c4", "type": "click", "val": 8,    "base": 600,   "mult": 1.32, "max": 10, "cat": "std"},
    {"id": "p1", "type": "ps",    "val": 0.005,"base": 15,    "mult": 1.25, "max": 10, "cat": "std"},
    {"id": "p2", "type": "ps",    "val": 0.012,"base": 60,    "mult": 1.28, "max": 10, "cat": "std"},
    {"id": "p3", "type": "ps",    "val": 0.025,"base": 200,   "mult": 1.30, "max": 10, "cat": "std"},
    {"id": "p4", "type": "ps",    "val": 0.04, "base": 800,   "mult": 1.32, "max": 10, "cat": "std"},
    {"id": "p5", "type": "ps",    "val": 0.06, "base": 3000,  "mult": 1.35, "max": 10, "cat": "std"},
    {"id": "e1", "type": "click", "val": 228,  "base": 8000,  "mult": 1,    "max": 1,  "cat": "eli"},
    {"id": "e2", "type": "ps",    "val": 0.48, "base": 20000, "mult": 1,    "max": 1,  "cat": "eli"},
    {"id": "leg1","type": "click","val": 1,    "base": 3,     "mult": 999,  "max": 1,  "cat": "leg"},
    {"id": "leg2","type": "both", "val": 1.05, "base": 8,     "mult": 999,  "max": 1,  "cat": "leg"},
]
DEFS_MAP = {d["id"]: d for d in DEFS_SERVER}


def recalc_stats_from_upgrades(upgrades: list) -> tuple:
    """Пересчитывает clickPower и perSecond из уровней апгрейдов."""
    cp, ps = 1.0, 0.0
    if not upgrades:
        return 1, 0.0
    # Сначала additive
    for u in upgrades:
        d = DEFS_MAP.get(u.get("id"))
        if not d or not u.get("level"):
            continue
        lvl = int(u["level"])
        if d["type"] == "click":
            cp += d["val"] * lvl
        elif d["type"] == "ps":
            ps += d["val"] * lvl
    # Потом multipliers
    for u in upgrades:
        d = DEFS_MAP.get(u.get("id"))
        if d and d["type"] == "both" and u.get("level"):
            for _ in range(int(u["level"])):
                cp *= d["val"]
                ps *= d["val"]
    return max(1, round(cp)), max(0.0, round(ps, 4))


# ══════════════════════════════════════════════════════════════
# ИНИЦИАЛИЗАЦИЯ БД — создать таблицы если нет
# ══════════════════════════════════════════════════════════════

def init_db_tables():
    init_pool()
    conn = get_conn()
    cur = conn.cursor()
    try:
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
                is_banned    BOOLEAN NOT NULL DEFAULT FALSE
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS season_archive (
                id         SERIAL PRIMARY KEY,
                season_num INTEGER NOT NULL,
                ended_at   INTEGER DEFAULT 0,
                top_json   TEXT    DEFAULT '[]'
            )
        """)
        conn.commit()

        # Миграции — каждая в отдельной транзакции чтобы одна ошибка не мешала остальным
        migrations = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS game_state TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS clan_id INTEGER DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE",
            "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS note TEXT DEFAULT ''",
            "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS from_id BIGINT",
            "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS to_id BIGINT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS click_ban_until INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS click_ban_reason TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS inactive_banned BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS inactive_ban_ts INTEGER DEFAULT 0",
            # FIX #9: нормализуем is_banned чтобы индекс работал
            "UPDATE users SET is_banned=FALSE WHERE is_banned IS NULL",
            "ALTER TABLE users ALTER COLUMN is_banned SET NOT NULL",
            "ALTER TABLE users ALTER COLUMN is_banned SET DEFAULT FALSE",
        ]
        for sql in migrations:
            try:
                cur.execute(sql)
                conn.commit()
            except Exception:
                conn.rollback()

        cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()

        # FIX #9: правильные индексы (без WHERE — чтобы условие OR тоже работало)
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_users_game_balance ON users(game_balance DESC NULLS LAST)",
            "CREATE INDEX IF NOT EXISTS idx_users_username_lower ON users(LOWER(username))",
            "CREATE INDEX IF NOT EXISTS idx_clan_members_user ON clan_members(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_daily_data_user ON daily_data(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_transfers_from ON transfers(from_id)",
            "CREATE INDEX IF NOT EXISTS idx_transfers_to ON transfers(to_id)",
            "CREATE INDEX IF NOT EXISTS idx_transfers_created ON transfers(created_at DESC)",
        ]
        for sql in indexes:
            try:
                cur.execute(sql)
                conn.commit()
            except Exception:
                conn.rollback()

        logger.info("DB init OK")
    except Exception as e:
        conn.rollback()
        logger.error(f"DB init error: {e}")
        raise
    finally:
        cur.close()
        release_conn(conn)


# ══════════════════════════════════════════════════════════════
# Уведомление через Telegram Bot API
# ══════════════════════════════════════════════════════════════

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
# ПОЛЬЗОВАТЕЛИ
# ══════════════════════════════════════════════════════════════

@app.get("/api/game/check/{tg_id}")
def check_user(tg_id: int, username: str = ""):
    if tg_id == 0:
        return {"registered": True, "username": "guest"}
    conn = get_conn()
    cur = conn.cursor()
    try:
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
            return {"registered": True, "username": uname, "new": True}
        else:
            if username:
                cur.execute("UPDATE users SET username=%s WHERE user_id=%s", (username, tg_id))
                conn.commit()
        return {"registered": True, "username": row["username"]}
    except Exception as e:
        conn.rollback()
        logger.error(f"check_user error: {e}")
        raise HTTPException(500, "Ошибка сервера")
    finally:
        cur.close()
        release_conn(conn)


@app.get("/api/game/load/{tg_id}")
def load_game_state(tg_id: int):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM users WHERE user_id=%s", (tg_id,))
        row = cur.fetchone()

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

        if state:
            state["coins"] = db_game_balance
            state["tgId"] = tg_id
            state["username"] = row.get("username") or f"id{tg_id}"

        # FIX #10: клан загружается через то же соединение (без conn2)
        clan_data = None
        if row.get("clan_id"):
            cur.execute(
                "SELECT c.id, c.name, c.emoji, cm.role FROM clans c "
                "JOIN clan_members cm ON c.id=cm.clan_id WHERE cm.user_id=%s",
                (tg_id,)
            )
            crow = cur.fetchone()
            if crow:
                cur.execute("""
                    SELECT cm.user_id, cm.role, u.username, u.game_balance
                    FROM clan_members cm JOIN users u ON cm.user_id=u.user_id
                    WHERE cm.clan_id=%s ORDER BY u.game_balance DESC
                """, (crow["id"],))
                members = [
                    {
                        "user_id": m["user_id"],
                        "role": m["role"],
                        "name": m["username"] or f"id{m['user_id']}",
                        "emoji": "⚔️",
                        "coins": m["game_balance"] or 0,
                    }
                    for m in cur.fetchall() if m["user_id"] != tg_id
                ]
                clan_data = {
                    "name": crow["name"],
                    "emoji": crow["emoji"],
                    "role": crow["role"],
                    "members": members,
                    "clan_id": crow["id"],
                }

        if state:
            state["clan"] = clan_data

        # Бан кликера — проверяем и сбрасываем если истёк
        click_ban_until = int(row.get("click_ban_until") or 0)
        click_ban_reason = row.get("click_ban_reason") or ""
        if click_ban_until > 0 and click_ban_until < int(time.time()):
            click_ban_until = 0
            click_ban_reason = ""
            cur.execute(
                "UPDATE users SET click_ban_until=0, click_ban_reason='' WHERE user_id=%s",
                (tg_id,)
            )
            conn.commit()

        # Извлекаем claimedAchs из state
        claimed_achs = {}
        if state and isinstance(state.get("claimedAchs"), dict):
            claimed_achs = state["claimedAchs"]

        return {
            "found": True,
            "state": state,
            "db_game_balance": db_game_balance,
            "username": row.get("username"),
            "user_id": row.get("user_id"),
            "level": row.get("level"),
            "xp": row.get("xp"),
            "clan": clan_data,
            "click_ban_until": click_ban_until,
            "click_ban_reason": click_ban_reason,
            "inactive_banned": bool(row.get("inactive_banned")),
            "inactive_ban_ts": int(row.get("inactive_ban_ts") or 0),
            "claimed_achs": claimed_achs,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"load_game_state error: {e}")
        raise HTTPException(500, "Ошибка сервера")
    finally:
        cur.close()
        release_conn(conn)


class SaveStateRequest(BaseModel):
    tg_id: int
    state: dict
    coins: Optional[int] = None
    delta: Optional[int] = None
    exact: Optional[bool] = False
    init_data: Optional[str] = None  # FIX #1: поле для Telegram initData


_MAX_DELTA_PER_SAVE = 9_000_000_000_000_000_000   # BIGINT safe
_MAX_BALANCE       = 9_000_000_000_000_000_000   # BIGINT max (~9 quintillion)


@app.post("/api/game/save")
def save_game_state(req: SaveStateRequest):
    # FIX #1: Верификация Telegram initData (если BOT_TOKEN задан)
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        logger.warning(f"[AUTH] save rejected for tg_id={req.tg_id}: invalid initData")
        raise HTTPException(403, "Неверная подпись Telegram")

    client_delta = int(req.delta) if req.delta is not None else 0
    if client_delta < 0:
        client_delta = 0

    # FIX #4: Rate limiting
    if not check_save_rate_limit(req.tg_id, client_delta):
        raise HTTPException(429, "Слишком много запросов, подождите")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT user_id, is_banned, game_balance FROM users WHERE user_id=%s", (req.tg_id,))
        row = cur.fetchone()

        if not row:
            if req.tg_id and req.tg_id != 0:
                uname = req.state.get("username") or f"id{req.tg_id}"
                try:
                    cur.execute(
                        """INSERT INTO users
                           (user_id,username,balance,game_balance,xp,level,
                            games_played,wins,losses,daily_last,created_at,updated_at,is_banned)
                           VALUES (%s,%s,0,%s,0,1,0,0,0,0,%s,%s,FALSE) ON CONFLICT DO NOTHING""",
                        (req.tg_id, uname, max(0, int(req.coins or 0)),
                         int(time.time()), int(time.time()))
                    )
                    conn.commit()
                    cur.execute("SELECT user_id, is_banned, game_balance FROM users WHERE user_id=%s", (req.tg_id,))
                    row = cur.fetchone()
                    if row:
                        logger.info(f"Auto-registered user {req.tg_id} ({uname}) during save")
                except Exception as _ae:
                    logger.warning(f"Auto-register in save failed for {req.tg_id}: {_ae}")
                    conn.rollback()
            if not row:
                raise HTTPException(404, "Пользователь не найден. Напишите /start боту.")

        if row["is_banned"]:
            raise HTTPException(403, "Аккаунт заблокирован.")

        db_game_balance = int(row["game_balance"] or 0)
        client_coins = int(req.coins) if req.coins is not None else None

        # ── Вычисляем новый баланс ──────────────────────────────
        if client_delta > 0:
            # FIX #2: атомарный UPDATE по delta — нет race condition с казино/трейдом
            cur.execute(
                """UPDATE users
                   SET game_balance = GREATEST(game_balance + %s, 0),
                       updated_at = %s
                   WHERE user_id = %s
                   RETURNING game_balance""",
                (client_delta, int(time.time()), req.tg_id)
            )
            result = cur.fetchone()
            new_balance = int(result["game_balance"]) if result else db_game_balance + client_delta
        elif req.exact and client_coins is not None and client_coins >= 0:
            # FIX #5: точное списание (апгрейд, покупка) — используем разницу от db
            # Это безопасно: cost = db_bal - client_coins, применяем к db
            cost = db_game_balance - client_coins
            if cost < 0:
                # OCP добавил монет пока клиент не видел — не уменьшаем баланс
                new_balance = db_game_balance
                logger.info(f"[EXACT] uid={req.tg_id} cost<0 (OCP added coins), keeping db={db_game_balance}")
            else:
                new_balance = max(0, db_game_balance - cost)
            cur.execute(
                "UPDATE users SET game_balance=%s, updated_at=%s WHERE user_id=%s",
                (new_balance, int(time.time()), req.tg_id)
            )
        else:
            # Обычный save без delta и без exact: баланс из БД
            new_balance = db_game_balance

        # ── Читаем текущий game_state из БД ──────────────────────
        cur.execute("SELECT game_state FROM users WHERE user_id=%s", (req.tg_id,))
        gs_row = cur.fetchone()
        db_state = {}
        if gs_row and gs_row.get("game_state"):
            try:
                db_state = json.loads(gs_row["game_state"])
            except Exception:
                db_state = {}

        # ── Формируем state для сохранения ───────────────────────
        state_to_save = dict(req.state)
        state_to_save.pop("coins", None)

        # FIX: Validate _lastSeen — prevent offline income manipulation
        server_now = int(time.time())
        client_last_seen = int(state_to_save.get("_lastSeen", 0))
        if client_last_seen > server_now + 60 or client_last_seen < server_now - 7 * 86400:
            state_to_save["_lastSeen"] = server_now

        # FIX #11: пересчитываем clickPower и perSecond из апгрейдов
        if state_to_save.get("upgrades"):
            cp, ps = recalc_stats_from_upgrades(state_to_save["upgrades"])
            state_to_save["clickPower"] = cp
            state_to_save["perSecond"] = ps

        # FIX алмазов: берём MAX(db, client) — алмазы не пропадают
        client_diamonds = int(state_to_save.get("diamonds") or 0)
        if not req.exact:
            db_diamonds = int(db_state.get("diamonds") or 0)
            if db_diamonds > client_diamonds:
                state_to_save["diamonds"] = db_diamonds

        # FIX ownedSkins/ownedBgs: union — купленное не пропадает
        db_owned_skins = db_state.get("ownedSkins") or []
        db_owned_bgs = db_state.get("ownedBgs") or []
        client_owned_skins = state_to_save.get("ownedSkins") or []
        client_owned_bgs = state_to_save.get("ownedBgs") or []
        merged_skins = list(set(db_owned_skins) | set(s for s in client_owned_skins if isinstance(s, str)))
        merged_bgs = list(set(db_owned_bgs) | set(b for b in client_owned_bgs if isinstance(b, str)))
        if merged_skins:
            state_to_save["ownedSkins"] = merged_skins
        if merged_bgs:
            state_to_save["ownedBgs"] = merged_bgs

        # FIX totalClicks: не уменьшаем никогда
        db_total_clicks = int(db_state.get("totalClicks") or 0)
        client_total_clicks = int(state_to_save.get("totalClicks") or 0)
        if db_total_clicks > client_total_clicks:
            state_to_save["totalClicks"] = db_total_clicks

        # FIX ачивок: мержим из БД — не даём затереть уже полученные
        client_achs = state_to_save.get("claimedAchs") or {}
        db_achs = db_state.get("claimedAchs") or {}
        state_to_save["claimedAchs"] = {**client_achs, **db_achs}

        # FIX престижа: prestige и prestigeMultiplier никогда не уменьшаются
        db_prestige = int(db_state.get("prestige") or 0)
        client_prestige = int(state_to_save.get("prestige") or 0)
        if db_prestige > client_prestige:
            state_to_save["prestige"] = db_prestige
            logger.info(f"[PRESTIGE GUARD] uid={req.tg_id} client={client_prestige} restored to db={db_prestige}")
        actual_prestige = max(db_prestige, client_prestige)
        db_mult = float(db_state.get("prestigeMultiplier") or 1.0)
        client_mult = float(state_to_save.get("prestigeMultiplier") or 1.0)
        correct_mult = round(1 + actual_prestige * 0.15, 4) if actual_prestige > 0 else 1.0
        # Берём максимум между db, клиентом и пересчитанным значением
        state_to_save["prestigeMultiplier"] = max(db_mult, client_mult, correct_mult)

        # Сохраняем state (если обновлялся game_balance через delta — state отдельным запросом)
        cur.execute(
            "UPDATE users SET game_state=%s, updated_at=%s WHERE user_id=%s",
            (json.dumps(state_to_save, ensure_ascii=False), int(time.time()), req.tg_id)
        )
        conn.commit()

        # Читаем актуальный баланс (после возможного атомарного UPDATE)
        cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (req.tg_id,))
        final_row = cur.fetchone()
        final_balance = int(final_row["game_balance"]) if final_row else new_balance

        return {"ok": True, "db_game_balance": final_balance}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"save_game_state error tg_id={req.tg_id}: {e}")
        raise HTTPException(500, "Ошибка сохранения")
    finally:
        cur.close()
        release_conn(conn)


# ══════════════════════════════════════════════════════════════
# ТОП
# ══════════════════════════════════════════════════════════════

@app.get("/api/top")
def top_players(limit: int = 30):
    conn = get_conn()
    cur = conn.cursor()
    try:
        # FIX #8: убираем коррелированный подзапрос — используем LEFT JOIN
        cur.execute(
            """SELECT u.user_id, u.username, u.game_balance, u.level,
                      c.name AS clan_name
               FROM users u
               LEFT JOIN clan_members cm ON u.user_id = cm.user_id
               LEFT JOIN clans c ON cm.clan_id = c.id
               WHERE u.is_banned = FALSE
                 AND (u.inactive_banned IS NULL OR u.inactive_banned = FALSE)
                 AND COALESCE(u.game_balance, 0) > 0
               ORDER BY u.game_balance DESC NULLS LAST
               LIMIT %s""",
            (min(limit, 100),)
        )
        rows = cur.fetchall()
        return {
            "top": [
                {
                    "user_id": r["user_id"],
                    "username": r["username"] or f"id{r['user_id']}",
                    "coins": int(r["game_balance"] or 0),
                    "game_balance": int(r["game_balance"] or 0),
                    "level": r["level"] or 1,
                    "clan_name": r["clan_name"] or None,
                }
                for r in rows
            ]
        }
    finally:
        cur.close()
        release_conn(conn)


@app.get("/api/rank/{user_id}")
def get_rank(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Не найден")
        bal = int(row["game_balance"] or 0)
        cur.execute(
            "SELECT COUNT(*) FROM users WHERE is_banned = FALSE "
            "AND (inactive_banned IS NULL OR inactive_banned = FALSE) "
            "AND COALESCE(game_balance,0)>%s",
            (bal,)
        )
        cnt_row = cur.fetchone()
        cnt = cnt_row["count"] if cnt_row else 0
        return {"rank": cnt + 1, "game_balance": bal}
    finally:
        cur.close()
        release_conn(conn)


@app.api_route("/api/stats", methods=["GET", "HEAD"])
def global_stats():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*), SUM(game_balance) FROM users WHERE is_banned=FALSE")
        row = cur.fetchone()
        return {"total_players": row["count"] or 0, "total_coins": int(row["sum"] or 0)}
    finally:
        cur.close()
        release_conn(conn)


def _get_season_ts(cur) -> int:
    try:
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
    cur.execute(
        "INSERT INTO settings(key,value) VALUES('season_end_ts',%s) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        (str(ts),)
    )


@app.get("/api/season")
def get_season_info():
    conn = get_conn()
    cur = conn.cursor()
    try:
        end_ts = _get_season_ts(cur)
        cur.execute("""
            SELECT user_id, username, game_balance
            FROM users WHERE is_banned=FALSE
            ORDER BY game_balance DESC NULLS LAST LIMIT 3
        """)
        top3 = [
            {
                "rank": i + 1,
                "username": r["username"] or f"id{r['user_id']}",
                "user_id": r["user_id"],
                "game_balance": int(r["game_balance"] or 0),
            }
            for i, r in enumerate(cur.fetchall())
        ]

        season_num = 1
        try:
            cur.execute("SELECT COALESCE(MAX(season_num),0)+1 AS season_num FROM season_archive")
            season_num = cur.fetchone()["season_num"]
        except Exception:
            conn.rollback()

        started_ago = 0
        try:
            cur.execute("SELECT MIN(created_at) AS oldest FROM users")
            row2 = cur.fetchone()
            oldest = int(row2["oldest"] or 0) if row2 else 0
            started_ago = max(0, int((time.time() - oldest) / 86400)) if oldest > 0 else 0
        except Exception:
            pass

        # FIX #3: возвращаем last_season_reset_ts чтобы клиент мог проверить свежесть backup
        last_reset_ts = 0
        try:
            cur.execute("SELECT value FROM settings WHERE key='last_season_reset_ts'")
            rr = cur.fetchone()
            if rr and rr.get("value"):
                last_reset_ts = int(rr["value"])
        except Exception:
            conn.rollback()

        return {
            "season_num": season_num,
            "end_ts": end_ts,
            "top3": top3,
            "rewards": [30, 20, 10],
            "started_ago": started_ago,
            "last_season_reset_ts": last_reset_ts,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"get_season_info error: {e}")
        raise HTTPException(500, "Ошибка сервера")
    finally:
        cur.close()
        release_conn(conn)


class SeasonSetRequest(BaseModel):
    end_ts: int
    secret: str = ""


@app.post("/api/season/set")
def set_season_timer(req: SeasonSetRequest):
    conn = get_conn()
    cur = conn.cursor()
    try:
        _set_season_ts(cur, req.end_ts)
        conn.commit()
        return {"ok": True, "end_ts": req.end_ts}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


class SeasonRewardRequest(BaseModel):
    user_id: int
    diamonds: int


@app.post("/api/season/reward")
def give_season_reward(req: SeasonRewardRequest):
    if req.diamonds <= 0:
        raise HTTPException(400, "diamonds must be > 0")
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT game_state FROM users WHERE user_id=%s", (req.user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Пользователь не найден")
        state = {}
        if row["game_state"]:
            try:
                state = json.loads(row["game_state"])
            except Exception:
                pass
        state["diamonds"] = int(state.get("diamonds") or 0) + req.diamonds
        cur.execute(
            "UPDATE users SET game_state=%s WHERE user_id=%s",
            (json.dumps(state, ensure_ascii=False), req.user_id)
        )
        conn.commit()
        return {"ok": True, "new_diamonds": state["diamonds"]}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


# ══════════════════════════════════════════════════════════════
# МИНЫ — полная реализация
# ══════════════════════════════════════════════════════════════

MINES_GRID_SIZE = 25
MINES_RTP = 0.97


def _mines_multiplier(mines: int, opened: int) -> float:
    safe_cells = MINES_GRID_SIZE - mines
    if opened <= 0 or opened > safe_cells:
        return 1.0
    prob = 1.0
    for i in range(opened):
        prob *= (safe_cells - i) / (MINES_GRID_SIZE - i)
    if prob <= 0:
        return 1.0
    return round(MINES_RTP / prob, 4)


class MinesStartRequest(BaseModel):
    tg_id: int
    bet: int
    mines: int
    init_data: Optional[str] = None


@app.post("/api/mines/start")
def mines_start(req: MinesStartRequest):
    # FIX #1: auth
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    if req.bet <= 0:
        raise HTTPException(400, "Ставка должна быть > 0")
    if not (1 <= req.mines <= 24):
        raise HTTPException(400, "Количество мин: 1–24")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT game_balance, is_banned FROM users WHERE user_id=%s FOR UPDATE",
            (req.tg_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Пользователь не найден")
        if row["is_banned"]:
            raise HTTPException(403, "Аккаунт заблокирован")

        balance = int(row["game_balance"] or 0)
        if balance < req.bet:
            raise HTTPException(400, f"Недостаточно монет. Баланс: {balance}")

        # Удаляем старую сессию если была
        cur.execute("DELETE FROM mines_sessions WHERE user_id=%s", (req.tg_id,))

        mine_positions = random.sample(range(MINES_GRID_SIZE), req.mines)
        new_balance = balance - req.bet
        cur.execute(
            "UPDATE users SET game_balance=%s, updated_at=%s WHERE user_id=%s",
            (new_balance, int(time.time()), req.tg_id)
        )
        cur.execute(
            """INSERT INTO mines_sessions (user_id, bet, mines, mine_positions, opened_cells, created_at)
               VALUES (%s, %s, %s, %s, '', %s)""",
            (req.tg_id, req.bet, req.mines, json.dumps(mine_positions), int(time.time()))
        )
        conn.commit()

        return {
            "ok": True,
            "new_balance": new_balance,
            "multiplier": 1.0,
            "next_multiplier": _mines_multiplier(req.mines, 1),
            "opened_cells": [],
            "mines_count": req.mines,
            "safe_count": MINES_GRID_SIZE - req.mines,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"mines_start error: {e}")
        raise HTTPException(500, "Ошибка сервера")
    finally:
        cur.close()
        release_conn(conn)


class MinesOpenRequest(BaseModel):
    tg_id: int
    cell: int
    init_data: Optional[str] = None


@app.post("/api/mines/open")
def mines_open(req: MinesOpenRequest):
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    if not (0 <= req.cell < MINES_GRID_SIZE):
        raise HTTPException(400, f"Клетка должна быть от 0 до {MINES_GRID_SIZE - 1}")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM mines_sessions WHERE user_id=%s", (req.tg_id,))
        session = cur.fetchone()
        if not session:
            raise HTTPException(404, "Активная игра не найдена. Начните новую игру.")

        bet = int(session["bet"])
        mines_count = int(session["mines"])
        mine_positions: List[int] = json.loads(session["mine_positions"])
        opened_raw = session["opened_cells"] or ""
        opened_cells: List[int] = json.loads(opened_raw) if opened_raw.strip().startswith("[") else []

        if req.cell in opened_cells:
            raise HTTPException(400, "Клетка уже открыта")

        is_mine = req.cell in mine_positions

        if is_mine:
            cur.execute("DELETE FROM mines_sessions WHERE user_id=%s", (req.tg_id,))
            cur.execute(
                "UPDATE users SET games_played=games_played+1, losses=losses+1 WHERE user_id=%s",
                (req.tg_id,)
            )
            conn.commit()
            cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (req.tg_id,))
            bal_row = cur.fetchone()
            return {
                "ok": True,
                "hit_mine": True,
                "cell": req.cell,
                "mine_positions": mine_positions,
                "opened_cells": opened_cells,
                "win": 0,
                "new_balance": int(bal_row["game_balance"]) if bal_row else 0,
                "multiplier": 0.0,
            }

        opened_cells.append(req.cell)
        safe_opened = len(opened_cells)
        safe_total = MINES_GRID_SIZE - mines_count
        current_multiplier = _mines_multiplier(mines_count, safe_opened)
        potential_win = int(bet * current_multiplier)

        if safe_opened >= safe_total:
            cur.execute("DELETE FROM mines_sessions WHERE user_id=%s", (req.tg_id,))
            cur.execute(
                "UPDATE users SET game_balance=game_balance+%s, games_played=games_played+1, wins=wins+1, updated_at=%s WHERE user_id=%s",
                (potential_win, int(time.time()), req.tg_id)
            )
            conn.commit()
            cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (req.tg_id,))
            bal_row = cur.fetchone()
            return {
                "ok": True,
                "hit_mine": False,
                "cell": req.cell,
                "opened_cells": opened_cells,
                "win": potential_win,
                "new_balance": int(bal_row["game_balance"]) if bal_row else 0,
                "multiplier": current_multiplier,
                "next_multiplier": current_multiplier,
                "auto_cashout": True,
                "mine_positions": mine_positions,
            }

        next_multiplier = _mines_multiplier(mines_count, safe_opened + 1)
        cur.execute(
            "UPDATE mines_sessions SET opened_cells=%s WHERE user_id=%s",
            (json.dumps(opened_cells), req.tg_id)
        )
        conn.commit()

        return {
            "ok": True,
            "hit_mine": False,
            "cell": req.cell,
            "opened_cells": opened_cells,
            "win": potential_win,
            "new_balance": None,
            "multiplier": current_multiplier,
            "next_multiplier": next_multiplier,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"mines_open error: {e}")
        raise HTTPException(500, "Ошибка сервера")
    finally:
        cur.close()
        release_conn(conn)


class MinesCashoutRequest(BaseModel):
    tg_id: int
    init_data: Optional[str] = None


@app.post("/api/mines/cashout")
def mines_cashout(req: MinesCashoutRequest):
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM mines_sessions WHERE user_id=%s", (req.tg_id,))
        session = cur.fetchone()
        if not session:
            raise HTTPException(404, "Активная игра не найдена")

        bet = int(session["bet"])
        mines_count = int(session["mines"])
        mine_positions: List[int] = json.loads(session["mine_positions"])
        opened_raw = session["opened_cells"] or ""
        opened_cells: List[int] = json.loads(opened_raw) if opened_raw.strip().startswith("[") else []
        safe_opened = len(opened_cells)

        if safe_opened == 0:
            multiplier = 1.0
            win = bet
        else:
            multiplier = _mines_multiplier(mines_count, safe_opened)
            win = int(bet * multiplier)

        cur.execute("DELETE FROM mines_sessions WHERE user_id=%s", (req.tg_id,))
        cur.execute(
            "UPDATE users SET game_balance=game_balance+%s, games_played=games_played+1, wins=wins+1, updated_at=%s WHERE user_id=%s",
            (win, int(time.time()), req.tg_id)
        )
        conn.commit()
        cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (req.tg_id,))
        bal_row = cur.fetchone()

        return {
            "ok": True,
            "win": win,
            "multiplier": multiplier,
            "opened_cells": opened_cells,
            "mine_positions": mine_positions,
            "new_balance": int(bal_row["game_balance"]) if bal_row else 0,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"mines_cashout error: {e}")
        raise HTTPException(500, "Ошибка сервера")
    finally:
        cur.close()
        release_conn(conn)


@app.get("/api/mines/session/{tg_id}")
def mines_get_session(tg_id: int):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM mines_sessions WHERE user_id=%s", (tg_id,))
        session = cur.fetchone()
        if not session:
            return {"active": False}

        bet = int(session["bet"])
        mines_count = int(session["mines"])
        opened_raw = session["opened_cells"] or ""
        opened_cells: List[int] = json.loads(opened_raw) if opened_raw.strip().startswith("[") else []
        safe_opened = len(opened_cells)
        current_multiplier = _mines_multiplier(mines_count, safe_opened) if safe_opened > 0 else 1.0
        next_multiplier = _mines_multiplier(mines_count, safe_opened + 1)

        return {
            "active": True,
            "bet": bet,
            "mines": mines_count,
            "opened_cells": opened_cells,
            "multiplier": current_multiplier,
            "next_multiplier": next_multiplier,
            "potential_win": int(bet * current_multiplier) if safe_opened > 0 else bet,
        }
    finally:
        cur.close()
        release_conn(conn)


# ══════════════════════════════════════════════════════════════
# АНТИ-КЛИКЕР
# ══════════════════════════════════════════════════════════════

class ClickBanRequest(BaseModel):
    tg_id: int
    reason: str = ""
    ban_until: Optional[int] = None


@app.post("/api/game/click_ban")
def set_click_ban(req: ClickBanRequest):
    ban_until = req.ban_until if req.ban_until else int(time.time()) + 20 * 60
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT user_id FROM users WHERE user_id=%s", (req.tg_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Пользователь не найден")
        cur.execute(
            "UPDATE users SET click_ban_until=%s, click_ban_reason=%s WHERE user_id=%s",
            (ban_until, req.reason[:500], req.tg_id)
        )
        conn.commit()
        logger.warning(f"[ANTI-CLICKER] tg_id={req.tg_id} ban_until={ban_until} reason={req.reason}")
        return {
            "ok": True,
            "ban_until": ban_until,
            "ban_until_readable": time.strftime("%H:%M:%S %d.%m", time.localtime(ban_until)),
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


@app.get("/api/game/click_ban/{tg_id}")
def get_click_ban(tg_id: int):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT click_ban_until, click_ban_reason FROM users WHERE user_id=%s", (tg_id,))
        row = cur.fetchone()
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
    finally:
        cur.close()
        release_conn(conn)


# ══════════════════════════════════════════════════════════════
# БАН ЗА НЕАКТИВНОСТЬ
# ══════════════════════════════════════════════════════════════

class InactiveBanCheckRequest(BaseModel):
    tg_id: int


@app.post("/api/game/check_inactive_ban")
def check_inactive_ban(req: InactiveBanCheckRequest):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT inactive_banned, inactive_ban_ts FROM users WHERE user_id=%s", (req.tg_id,))
        row = cur.fetchone()
        if not row:
            return {"inactive_banned": False}
        return {
            "inactive_banned": bool(row["inactive_banned"]),
            "inactive_ban_ts": int(row["inactive_ban_ts"] or 0),
        }
    finally:
        cur.close()
        release_conn(conn)


@app.post("/api/game/self_unban")
async def self_unban(req: InactiveBanCheckRequest):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT inactive_banned, username FROM users WHERE user_id=%s", (req.tg_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Не найден")
        if not row["inactive_banned"]:
            return {"ok": True, "already_active": True}
        cur.execute(
            "UPDATE users SET inactive_banned=FALSE, inactive_ban_ts=0 WHERE user_id=%s",
            (req.tg_id,)
        )
        conn.commit()
        logger.info(f"[INACTIVE-UNBAN] user {req.tg_id} ({row['username']}) self-unbanned")
        return {"ok": True, "unbanned": True}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


@app.post("/api/game/apply_inactive_bans")
def apply_inactive_bans():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cutoff = int(time.time()) - 3 * 86400
        cur.execute("""
            SELECT u.user_id FROM users u
            LEFT JOIN daily_data d ON u.user_id = d.user_id
            WHERE u.is_banned = FALSE
              AND (u.inactive_banned IS NULL OR u.inactive_banned = FALSE)
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
        return {"ok": True, "banned_count": len(to_ban), "user_ids": to_ban}
    except Exception as e:
        conn.rollback()
        logger.error(f"apply_inactive_bans error: {e}")
        raise HTTPException(500, "Ошибка при применении банов")
    finally:
        cur.close()
        release_conn(conn)


# ══════════════════════════════════════════════════════════════
# ДЕПОЗИТ / ВЫВОД (игра ↔ бот)
# ══════════════════════════════════════════════════════════════

class DepositRequest(BaseModel):
    tg_id: int
    amount: int
    init_data: Optional[str] = None


@app.post("/api/deposit")
def deposit_to_game(req: DepositRequest):
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    if req.amount <= 0:
        raise HTTPException(400, "Сумма должна быть > 0")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT balance, game_balance FROM users WHERE user_id=%s FOR UPDATE",
            (req.tg_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Пользователь не найден")
        if int(row["balance"] or 0) < req.amount:
            raise HTTPException(400, f"Недостаточно монет. Баланс бота: {row['balance']}")
        new_bot = int(row["balance"]) - req.amount
        new_game = int(row["game_balance"] or 0) + req.amount
        cur.execute(
            "UPDATE users SET balance=%s, game_balance=%s, updated_at=%s WHERE user_id=%s",
            (new_bot, new_game, int(time.time()), req.tg_id)
        )
        cur.execute(
            "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) VALUES (%s,%s,'deposit',%s,'Пополнение игры',%s)",
            (req.tg_id, req.tg_id, req.amount, int(time.time()))
        )
        conn.commit()
        return {"ok": True, "new_game_balance": new_game, "new_bot_balance": new_bot}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


@app.post("/api/withdraw")
def withdraw_from_game(req: DepositRequest):
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    if req.amount <= 0:
        raise HTTPException(400, "Сумма должна быть > 0")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT balance, game_balance FROM users WHERE user_id=%s FOR UPDATE",
            (req.tg_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Пользователь не найден")
        game_bal = int(row["game_balance"] or 0)
        if game_bal < req.amount:
            raise HTTPException(400, f"Недостаточно монет в игре. Баланс игры: {game_bal}")
        new_game = game_bal - req.amount
        new_bot = int(row["balance"] or 0) + req.amount
        cur.execute(
            "UPDATE users SET balance=%s, game_balance=%s, updated_at=%s WHERE user_id=%s",
            (new_bot, new_game, int(time.time()), req.tg_id)
        )
        cur.execute(
            "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) VALUES (%s,%s,'withdraw',%s,'Вывод из игры',%s)",
            (req.tg_id, req.tg_id, req.amount, int(time.time()))
        )
        conn.commit()
        return {"ok": True, "new_game_balance": new_game, "new_bot_balance": new_bot}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


# ══════════════════════════════════════════════════════════════
# КАЗИНО-БОТ
# ══════════════════════════════════════════════════════════════

class CasinoDepositRequest(BaseModel):
    tg_id: int
    amount: int
    init_data: Optional[str] = None


@app.post("/api/casino_deposit")
async def casino_deposit(req: CasinoDepositRequest):
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    if req.amount <= 0:
        raise HTTPException(400, "Сумма должна быть > 0")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT game_balance, balance, username FROM users WHERE user_id=%s FOR UPDATE",
            (req.tg_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Пользователь не найден")
        game_bal = int(row["game_balance"] or 0)
        bot_bal = int(row["balance"] or 0)
        if game_bal < req.amount:
            raise HTTPException(400, f"Недостаточно монет в игре. Баланс: {game_bal}")
        new_game = game_bal - req.amount
        new_bot = bot_bal + req.amount
        cur.execute(
            "UPDATE users SET game_balance=%s, balance=%s, updated_at=%s WHERE user_id=%s",
            (new_game, new_bot, int(time.time()), req.tg_id)
        )
        cur.execute(
            "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) VALUES (%s,%s,'casino',%s,'Казино через игру',%s)",
            (req.tg_id, req.tg_id, req.amount, int(time.time()))
        )
        conn.commit()

        asyncio.ensure_future(notify_user(
            req.tg_id,
            f"🎰 <b>Казино-бот!</b>\n\n"
            f"Зачислено: <b>{req.amount} монет</b> для игр\n"
            f"🎮 Остаток в игре: <b>{new_game} монет</b>\n"
            f"💰 Баланс казино: <b>{new_bot} монет</b>"
        ))
        return {"ok": True, "new_balance": new_game, "new_bot_balance": new_bot}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


# ══════════════════════════════════════════════════════════════
# ТРЕЙД
# ══════════════════════════════════════════════════════════════

class TradeRequest(BaseModel):
    from_id: int
    amount: int
    fee: int = 0
    to_id: Optional[int] = None
    to_username: Optional[str] = None
    init_data: Optional[str] = None


@app.post("/api/trade")
async def trade_coins(req: TradeRequest):
    # FIX #1: auth — проверяем что from_id соответствует initData
    verified_id = get_verified_tg_id(req.init_data, req.from_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    if req.amount <= 0:
        raise HTTPException(400, "Сумма должна быть > 0")

    conn = get_conn()
    cur = conn.cursor()
    try:
        if req.to_id:
            cur.execute("SELECT user_id, username FROM users WHERE user_id=%s", (req.to_id,))
        elif req.to_username:
            clean = req.to_username.lstrip("@").strip()
            # FIX #9: используем индекс LOWER(username)
            cur.execute("SELECT user_id, username FROM users WHERE LOWER(username)=LOWER(%s)", (clean,))
        else:
            raise HTTPException(400, "Укажите to_id или to_username")

        to_row = cur.fetchone()
        if not to_row:
            raise HTTPException(404, "Получатель не найден")
        to_id = to_row["user_id"]
        to_name = to_row["username"] or f"id{to_id}"

        if to_id == req.from_id:
            raise HTTPException(400, "Нельзя переводить самому себе")

        total = req.amount + req.fee

        # FIX #2: блокируем обе строки чтобы избежать race condition
        lock_ids = sorted([req.from_id, to_id])
        cur.execute("SELECT user_id FROM users WHERE user_id=ANY(%s) FOR UPDATE", (lock_ids,))

        cur.execute("SELECT game_balance, username FROM users WHERE user_id=%s", (req.from_id,))
        from_row = cur.fetchone()
        if not from_row:
            raise HTTPException(404, "Отправитель не найден")
        from_game_bal = int(from_row["game_balance"] or 0)
        from_name = from_row["username"] or f"id{req.from_id}"

        if from_game_bal < total:
            raise HTTPException(400, f"Недостаточно монет в игре. Баланс: {from_game_bal}")

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

        cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (req.from_id,))
        new_bal = int(cur.fetchone()["game_balance"] or 0)

        asyncio.ensure_future(notify_user(
            to_id,
            f"💰 <b>Пополнение игрового баланса!</b>\n\n"
            f"От: <b>@{from_name}</b>\n"
            f"Сумма: <b>+{req.amount} монет</b>\n\n"
            f"🎮 Монеты зачислены в ваш игровой баланс."
        ))

        return {"ok": True, "new_balance": new_bal, "to_username": to_name}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"trade_coins error: {e}")
        raise HTTPException(500, "Ошибка трейда")
    finally:
        cur.close()
        release_conn(conn)


# ── История переводов ─────────────────────────────────────────

@app.get("/api/history/{tg_id}")
def get_history(tg_id: int, limit: int = 30):
    conn = get_conn()
    cur = conn.cursor()
    try:
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
        result = []
        for r in rows:
            is_sent = r["from_id"] == tg_id
            result.append({
                "id": r["id"],
                "type": "sent" if is_sent else "received",
                "direction": r["direction"],
                "amount": r["amount"],
                "note": r["note"] or "",
                "date": r["created_at"],
                "counterpart": (r["to_username"] or f"id{r['to_id']}") if is_sent else (r["from_username"] or f"id{r['from_id']}"),
                "isBotTransfer": r["direction"] in ("casino", "deposit", "withdraw"),
            })
        return {"ok": True, "history": result}
    finally:
        cur.close()
        release_conn(conn)


# ══════════════════════════════════════════════════════════════
# КЛАНЫ
# ══════════════════════════════════════════════════════════════

@app.get("/api/clans/top")
def clans_top(limit: int = 20):
    conn = get_conn()
    cur = conn.cursor()
    try:
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
        return {"ok": True, "clans": [dict(r) for r in rows]}
    finally:
        cur.close()
        release_conn(conn)


@app.get("/api/clans/members")
def clan_members(name: str = Query(...)):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM clans WHERE LOWER(name)=LOWER(%s)", (name,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"Клан «{name}» не найден")
        clan_id = row["id"]
        cur.execute("""
            SELECT cm.user_id, cm.role, u.username, u.game_balance
            FROM clan_members cm JOIN users u ON cm.user_id=u.user_id
            WHERE cm.clan_id=%s ORDER BY u.game_balance DESC
        """, (clan_id,))
        members = [
            {
                "user_id": r["user_id"],
                "role": r["role"],
                "username": r["username"] or f"id{r['user_id']}",
                "game_balance": int(r["game_balance"] or 0),
            }
            for r in cur.fetchall()
        ]
        return {"ok": True, "clan_id": clan_id, "members": members}
    except HTTPException:
        raise
    finally:
        cur.close()
        release_conn(conn)


class ClanCreateRequest(BaseModel):
    tg_id: int
    name: str
    emoji: str = "⚔️"
    init_data: Optional[str] = None


@app.post("/api/clans/create")
def clan_create(req: ClanCreateRequest):
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    name = req.name.strip()
    if not name or len(name) < 2 or len(name) > 20:
        raise HTTPException(400, "Длина названия: 2–20 символов")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM clans WHERE LOWER(name)=LOWER(%s)", (name,))
        if cur.fetchone():
            raise HTTPException(400, "Клан с таким именем уже существует")
        cur.execute("SELECT clan_id FROM users WHERE user_id=%s", (req.tg_id,))
        u = cur.fetchone()
        if u and u["clan_id"]:
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
        conn.commit()
        return {"ok": True, "clan_id": clan_id, "name": name}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


class ClanJoinRequest(BaseModel):
    tg_id: int
    name: str
    init_data: Optional[str] = None


@app.post("/api/clans/join")
def clan_join(req: ClanJoinRequest):
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, name, emoji FROM clans WHERE LOWER(name)=LOWER(%s)", (req.name.strip(),))
        clan = cur.fetchone()
        if not clan:
            raise HTTPException(404, f"Клан «{req.name}» не найден")
        cur.execute("SELECT clan_id FROM users WHERE user_id=%s", (req.tg_id,))
        u = cur.fetchone()
        if u and u["clan_id"]:
            raise HTTPException(400, "Вы уже состоите в клане")
        clan_id = clan["id"]
        cur.execute(
            "INSERT INTO clan_members (clan_id,user_id,role,joined_at) VALUES (%s,%s,'member',%s) ON CONFLICT DO NOTHING",
            (clan_id, req.tg_id, int(time.time()))
        )
        cur.execute("UPDATE users SET clan_id=%s WHERE user_id=%s", (clan_id, req.tg_id))
        conn.commit()
        cur.execute("""
            SELECT cm.user_id, cm.role, u.username, u.game_balance
            FROM clan_members cm JOIN users u ON cm.user_id=u.user_id
            WHERE cm.clan_id=%s ORDER BY u.game_balance DESC
        """, (clan_id,))
        members = [
            {
                "user_id": r["user_id"],
                "role": r["role"],
                "name": r["username"] or f"id{r['user_id']}",
                "emoji": "⚔️",
                "coins": int(r["game_balance"] or 0),
            }
            for r in cur.fetchall()
        ]
        return {"ok": True, "clan_id": clan_id, "name": clan["name"], "emoji": clan["emoji"], "members": members}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


class ClanLeaveRequest(BaseModel):
    tg_id: int
    init_data: Optional[str] = None


@app.post("/api/clans/leave")
def clan_leave(req: ClanLeaveRequest):
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT clan_id, role FROM clan_members WHERE user_id=%s", (req.tg_id,))
        row = cur.fetchone()
        if not row:
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
            cur.execute("SELECT COUNT(*) AS cnt FROM clan_members WHERE clan_id=%s", (clan_id,))
            cnt_row = cur.fetchone()
            if not cnt_row or int(cnt_row["cnt"]) == 0:
                cur.execute("DELETE FROM clans WHERE id=%s", (clan_id,))
        conn.commit()
        return {"ok": True}
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


class ClanKickRequest(BaseModel):
    tg_id: int
    kick_id: int
    init_data: Optional[str] = None


@app.post("/api/clans/kick")
def clan_kick(req: ClanKickRequest):
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT role, clan_id FROM clan_members WHERE user_id=%s", (req.tg_id,))
        r = cur.fetchone()
        if not r or r["role"] != "leader":
            raise HTTPException(403, "Только лидер может исключать участников")
        clan_id = r["clan_id"]
        cur.execute("SELECT role FROM clan_members WHERE user_id=%s AND clan_id=%s", (req.kick_id, clan_id))
        kr = cur.fetchone()
        if not kr:
            raise HTTPException(404, "Участник не найден в клане")
        if kr["role"] == "leader":
            raise HTTPException(400, "Нельзя исключить лидера")
        cur.execute("DELETE FROM clan_members WHERE user_id=%s AND clan_id=%s", (req.kick_id, clan_id))
        cur.execute("UPDATE users SET clan_id=NULL WHERE user_id=%s", (req.kick_id,))
        # Удаляем клан если больше никого нет
        cur.execute("SELECT COUNT(*) AS cnt FROM clan_members WHERE clan_id=%s", (clan_id,))
        cnt_row = cur.fetchone()
        if not cnt_row or int(cnt_row["cnt"]) == 0:
            cur.execute("DELETE FROM clans WHERE id=%s", (clan_id,))
        conn.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


# ══════════════════════════════════════════════════════════════
# ПРОФИЛЬ
# ══════════════════════════════════════════════════════════════

@app.get("/api/user/{user_id}")
def get_user(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Пользователь не найден")
        r = dict(row)
        r.pop("game_state", None)
        return r
    finally:
        cur.close()
        release_conn(conn)


# ══════════════════════════════════════════════════════════════
# STREAK & DAILY QUESTS
# ══════════════════════════════════════════════════════════════

class StreakClaimRequest(BaseModel):
    tg_id: int
    init_data: Optional[str] = None


@app.get("/api/daily/{tg_id}")
def get_daily_data(tg_id: int):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM daily_data WHERE user_id=%s", (tg_id,))
        row = cur.fetchone()
        if not row:
            return {"ok": True, "streak_days": 0, "streak_last_ts": 0, "quests_json": "{}", "quests_date": ""}
        return {
            "ok": True,
            "streak_days": int(row["streak_days"] or 0),
            "streak_last_ts": int(row["streak_last_ts"] or 0),
            "quests_json": row["quests_json"] or "{}",
            "quests_date": row["quests_date"] or "",
        }
    finally:
        cur.close()
        release_conn(conn)


class DailySaveRequest(BaseModel):
    tg_id: int
    streak_days: Optional[int] = None
    streak_last_ts: Optional[int] = None
    quests_json: Optional[str] = None
    quests_date: Optional[str] = None


@app.post("/api/daily/save")
def save_daily_data(req: DailySaveRequest):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT user_id FROM daily_data WHERE user_id=%s", (req.tg_id,))
        exists = cur.fetchone()
        now = int(time.time())
        if exists:
            fields, vals = [], []
            if req.streak_days is not None:
                fields.append("streak_days=%s")
                vals.append(req.streak_days)
            if req.streak_last_ts is not None:
                fields.append("streak_last_ts=%s")
                vals.append(req.streak_last_ts)
            if req.quests_json is not None:
                fields.append("quests_json=%s")
                vals.append(req.quests_json)
            if req.quests_date is not None:
                fields.append("quests_date=%s")
                vals.append(req.quests_date)
            if fields:
                fields.append("updated_at=%s")
                vals.append(now)
                vals.append(req.tg_id)
                cur.execute(f"UPDATE daily_data SET {','.join(fields)} WHERE user_id=%s", vals)
        else:
            cur.execute(
                "INSERT INTO daily_data (user_id,streak_days,streak_last_ts,quests_json,quests_date,updated_at) VALUES (%s,%s,%s,%s,%s,%s)",
                (req.tg_id, req.streak_days or 0, req.streak_last_ts or 0,
                 req.quests_json or "{}", req.quests_date or "", now)
            )
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


@app.post("/api/streak/claim")
async def claim_streak(req: StreakClaimRequest):
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM daily_data WHERE user_id=%s", (req.tg_id,))
        row = cur.fetchone()
        now = int(time.time())
        today = datetime.date.today().isoformat()

        streak_days = int(row["streak_days"] or 0) if row else 0
        streak_last_ts = int(row["streak_last_ts"] or 0) if row else 0

        last_date = datetime.date.fromtimestamp(streak_last_ts).isoformat() if streak_last_ts > 0 else ""
        if last_date == today:
            raise HTTPException(400, "Бонус уже получен сегодня")

        days_since = (now - streak_last_ts) / 86400 if streak_last_ts > 0 else 999
        if streak_last_ts == 0 or days_since > 2:
            new_days = 1
        else:
            new_days = streak_days + 1

        bonus = streak_days * 50
        min_r = 1000 + bonus
        max_r = 17500 + bonus
        reward = random.randint(min_r, max_r)

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

        cur.execute("UPDATE users SET game_balance=game_balance+%s WHERE user_id=%s", (reward, req.tg_id))
        cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (req.tg_id,))
        new_bal_row = cur.fetchone()
        new_balance = int(new_bal_row["game_balance"]) if new_bal_row else 0
        conn.commit()

        return {
            "ok": True,
            "reward": reward,
            "streak_days": new_days,
            "streak_last_ts": now,
            "new_balance": new_balance,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


class QuestClaimRequest(BaseModel):
    tg_id: int
    quest_id: str
    reward: int
    init_data: Optional[str] = None


@app.post("/api/daily/quest_claim")
def claim_quest(req: QuestClaimRequest):
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    if not req.tg_id or not req.quest_id or req.reward <= 0:
        raise HTTPException(400, "Bad request")

    MAX_QUEST_REWARD = 500
    safe_reward = min(req.reward, MAX_QUEST_REWARD)
    today = datetime.date.today().isoformat()

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT quests_json, quests_date FROM daily_data WHERE user_id=%s", (req.tg_id,))
        row = cur.fetchone()

        quests = {}
        if row:
            qdate = row["quests_date"] or ""
            if qdate == today:
                try:
                    quests = json.loads(row["quests_json"] or "{}")
                except Exception:
                    quests = {}

        if quests.get(req.quest_id):
            raise HTTPException(400, "Задание уже выполнено")

        quests[req.quest_id] = True
        now = int(time.time())
        quests_json = json.dumps(quests)

        if row:
            cur.execute(
                "UPDATE daily_data SET quests_json=%s,quests_date=%s,updated_at=%s WHERE user_id=%s",
                (quests_json, today, now, req.tg_id)
            )
        else:
            cur.execute(
                "INSERT INTO daily_data (user_id,streak_days,streak_last_ts,quests_json,quests_date,updated_at) VALUES (%s,0,0,%s,%s,%s)",
                (req.tg_id, quests_json, today, now)
            )

        cur.execute("UPDATE users SET game_balance=game_balance+%s WHERE user_id=%s", (safe_reward, req.tg_id))
        cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (req.tg_id,))
        nb = cur.fetchone()
        conn.commit()
        return {"ok": True, "new_balance": int(nb["game_balance"]) if nb else 0}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


# ── Ачивки ────────────────────────────────────────────────────

_VALID_ACHS = {
    "click100":  5000,
    "click1k":   15000,
    "click10k":  50000,
    "earn1k":    5000,
    "earn100k":  25000,
    "earn1m":    100000,
    "earn10m":   250000,
    "earn1b":    777000,
    "ps10":      20000,
    "ps100":     150000,
    "click100p": 75000,
    "prestige1": 500000,
    "prestige3": 2000000,
    "streak7":   50000,
    "streak30":  300000,
    "combo50":   100000,
    "diamonds5": 0,
    "alltrades": 10000,
    "maxupg1":   200000,
    "boost1":    15000,
}

# Server-side verification — проверяем что достижение реально выполнено
_ACH_CHECKS = {
    "click100":  lambda s: int(s.get("totalClicks", 0)) >= 100,
    "click1k":   lambda s: int(s.get("totalClicks", 0)) >= 1000,
    "click10k":  lambda s: int(s.get("totalClicks", 0)) >= 10000,
    "earn1k":    lambda s: int(s.get("totalCoins", 0)) >= 1000,
    "earn100k":  lambda s: int(s.get("totalCoins", 0)) >= 100000,
    "earn1m":    lambda s: int(s.get("totalCoins", 0)) >= 1000000,
    "earn10m":   lambda s: int(s.get("totalCoins", 0)) >= 10000000,
    "earn1b":    lambda s: int(s.get("totalCoins", 0)) >= 1000000000,
    "ps10":      lambda s: float(s.get("perSecond", 0)) >= 10,
    "ps100":     lambda s: float(s.get("perSecond", 0)) >= 100,
    "click100p": lambda s: int(s.get("clickPower", 1)) >= 100,
    "prestige1": lambda s: int(s.get("prestige", 0)) >= 1,
    "prestige3": lambda s: int(s.get("prestige", 0)) >= 3,
    "streak7":   lambda s: int(s.get("_streakDays", 0)) >= 7,
    "streak30":  lambda s: int(s.get("_streakDays", 0)) >= 30,
    "combo50":   lambda s: int(s.get("_maxCombo", 0)) >= 50,
    "diamonds5": lambda s: int(s.get("diamonds", 0)) >= 5,
    "alltrades": lambda s: int(s.get("_totalTrades", 0)) >= 1,
    "maxupg1":   lambda s: True,  # клиент проверяет по апгрейдам, сервер доверяет
    "boost1":    lambda s: int(s.get("_totalBoosts", 0)) >= 1,
}


class AchClaimRequest(BaseModel):
    tg_id: int
    ach_id: str
    reward: int
    init_data: Optional[str] = None


@app.post("/api/ach/claim")
def claim_achievement(req: AchClaimRequest):
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    if not req.tg_id or not req.ach_id:
        raise HTTPException(400, "Bad request")

    max_reward = _VALID_ACHS.get(req.ach_id)
    if max_reward is None:
        raise HTTPException(400, f"Unknown achievement: {req.ach_id}")
    safe_reward = min(req.reward, max_reward)

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT game_state FROM users WHERE user_id=%s", (req.tg_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Пользователь не найден")

        state = {}
        if row.get("game_state"):
            try:
                state = json.loads(row["game_state"])
            except Exception:
                pass

        claimed_achs = state.get("claimedAchs") or {}
        if claimed_achs.get(req.ach_id):
            raise HTTPException(400, "Достижение уже получено")

        # Проверяем выполнение достижения по данным из state
        check_fn = _ACH_CHECKS.get(req.ach_id)
        if check_fn and not check_fn(state):
            logger.warning(f"[ACH_CHEAT] uid={req.tg_id} ach={req.ach_id} not earned, state={state}")
            raise HTTPException(400, "Достижение ещё не выполнено")

        claimed_achs[req.ach_id] = True
        state["claimedAchs"] = claimed_achs

        cur.execute(
            "UPDATE users SET game_balance=game_balance+%s, game_state=%s, updated_at=%s WHERE user_id=%s",
            (safe_reward, json.dumps(state, ensure_ascii=False), int(time.time()), req.tg_id)
        )
        cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (req.tg_id,))
        nb = cur.fetchone()
        conn.commit()
        logger.info(f"[ACH_CLAIM] uid={req.tg_id} ach={req.ach_id} reward={safe_reward}")
        return {"ok": True, "new_balance": int(nb["game_balance"]) if nb else 0, "ach_id": req.ach_id}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


# ── Топ с полем streak ────────────────────────────────────────

@app.get("/api/top/extended")
def top_extended(limit: int = 30):
    conn = get_conn()
    cur = conn.cursor()
    try:
        # FIX #8: коррелированный подзапрос заменён на LEFT JOIN
        cur.execute(
            """SELECT u.user_id, u.username, u.game_balance, u.level,
                      c.name AS clan_name,
                      COALESCE(d.streak_days, 0) AS streak_days
               FROM users u
               LEFT JOIN daily_data d ON u.user_id = d.user_id
               LEFT JOIN clan_members cm ON u.user_id = cm.user_id
               LEFT JOIN clans c ON cm.clan_id = c.id
               WHERE u.is_banned = FALSE
                 AND (u.inactive_banned IS NULL OR u.inactive_banned = FALSE)
                 AND COALESCE(u.game_balance, 0) > 0
                 AND (u.click_ban_until IS NULL OR u.click_ban_until = 0
                      OR u.click_ban_until < EXTRACT(EPOCH FROM NOW())::BIGINT)
               ORDER BY u.game_balance DESC NULLS LAST
               LIMIT %s""",
            (min(limit, 100),)
        )
        rows = cur.fetchall()
        return {
            "top": [
                {
                    "user_id": r["user_id"],
                    "username": r["username"] or f"id{r['user_id']}",
                    "coins": int(r["game_balance"] or 0),
                    "game_balance": int(r["game_balance"] or 0),
                    "level": r["level"] or 1,
                    "clan_name": r["clan_name"] or None,
                    "streak_days": int(r["streak_days"] or 0),
                }
                for r in rows
            ]
        }
    finally:
        cur.close()
        release_conn(conn)



# ══════════════════════════════════════════════════════════════
# ADMIN: неактивность и прочее
# ══════════════════════════════════════════════════════════════

class AdminUnbanInactiveRequest(BaseModel):
    tg_id: int


@app.post("/api/game/admin_unban_inactive")
def admin_unban_inactive(req: AdminUnbanInactiveRequest):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE users SET inactive_banned=FALSE, inactive_ban_ts=0 WHERE user_id=%s",
            (req.tg_id,)
        )
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


# ══════════════════════════════════════════════════════════════
# ПРЕСТИЖ
# ══════════════════════════════════════════════════════════════

PRESTIGE_COSTS = [1_000_000, 5_000_000, 25_000_000, 100_000_000, 500_000_000]
PRESTIGE_BONUS = 0.15  # +15% к доходу за каждый уровень


class PrestigeRequest(BaseModel):
    tg_id: int
    init_data: Optional[str] = None


@app.post("/api/prestige")
async def do_prestige(req: PrestigeRequest):
    verified_id = get_verified_tg_id(req.init_data, req.tg_id)
    if verified_id is None:
        raise HTTPException(403, "Неверная подпись Telegram")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT game_balance, game_state FROM users WHERE user_id=%s FOR UPDATE",
            (req.tg_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Пользователь не найден")

        state = {}
        if row["game_state"]:
            try:
                state = json.loads(row["game_state"])
            except Exception:
                pass

        prestige_level = int(state.get("prestige", 0))
        if prestige_level >= len(PRESTIGE_COSTS):
            raise HTTPException(400, "Максимальный уровень престижа достигнут")

        cost = PRESTIGE_COSTS[prestige_level]
        balance = int(row["game_balance"] or 0)
        if balance < cost:
            raise HTTPException(400, f"Нужно {cost} монет для престижа")

        new_prestige = prestige_level + 1
        new_multiplier = round(1 + new_prestige * PRESTIGE_BONUS, 4)

        # Сбрасываем прогресс но сохраняем важные данные
        new_state = {
            "prestige": new_prestige,
            "prestigeMultiplier": new_multiplier,
            "diamonds":    state.get("diamonds", 0),
            "ownedSkins":  state.get("ownedSkins", []),
            "ownedBgs":    state.get("ownedBgs", []),
            "activeSkin":  state.get("activeSkin", "default"),
            "activeBg":    state.get("activeBg", "default"),
            "username":    state.get("username", f"id{req.tg_id}"),
            "tgId":        req.tg_id,
            "clan":        state.get("clan"),
            "claimedAchs": state.get("claimedAchs", {}),
            "dailyClaimed":state.get("dailyClaimed", {}),
            "coins":       0,
            "totalCoins":  int(state.get("totalCoins", 0)),
            "totalClicks": int(state.get("totalClicks", 0)),
            "clickPower":  1,
            "perSecond":   0,
            "upgrades":    [],
            "_totalBoosts":int(state.get("_totalBoosts", 0)),
            "_totalTrades":int(state.get("_totalTrades", 0)),
            "_maxCombo":   int(state.get("_maxCombo", 0)),
            "_lastSeen":   int(time.time()),
        }

        cur.execute(
            "UPDATE users SET game_balance=0, game_state=%s, updated_at=%s WHERE user_id=%s",
            (json.dumps(new_state, ensure_ascii=False), int(time.time()), req.tg_id)
        )
        conn.commit()
        logger.info(f"[PRESTIGE] uid={req.tg_id} level={new_prestige} mult={new_multiplier}")

        asyncio.ensure_future(notify_user(
            req.tg_id,
            f"⭐ <b>Престиж {new_prestige}!</b>\n\n"
            f"Вы сбросили прогресс и получили <b>+{int(PRESTIGE_BONUS*100)}%</b> к доходу навсегда.\n"
            f"Текущий множитель: <b>×{new_multiplier}</b>"
        ))

        return {
            "ok": True,
            "new_prestige": new_prestige,
            "new_multiplier": new_multiplier,
            "prestige_costs": PRESTIGE_COSTS,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"prestige error uid={req.tg_id}: {e}")
        raise HTTPException(500, "Ошибка сервера")
    finally:
        cur.close()
        release_conn(conn)


@app.get("/api/prestige/info")
def prestige_info():
    return {
        "costs": PRESTIGE_COSTS,
        "bonus_per_level": PRESTIGE_BONUS,
        "max_level": len(PRESTIGE_COSTS),
    }


# ══════════════════════════════════════════════════════════════
# СЕЗОН — завершение и сброс
# ══════════════════════════════════════════════════════════════

@app.post("/api/season/end")
async def end_season(secret: str = Query("")):
    admin_secret = os.environ.get("ADMIN_SECRET", "goldclick_admin_2024")
    if not secret or secret != admin_secret:
        raise HTTPException(403, "Forbidden")

    conn = get_conn()
    cur = conn.cursor()
    try:
        # Получаем топ-3 перед сбросом
        cur.execute("""
            SELECT user_id, username, game_balance FROM users
            WHERE is_banned=FALSE AND COALESCE(game_balance,0) > 0
            ORDER BY game_balance DESC LIMIT 3
        """)
        top3_rows = cur.fetchall()

        # Номер сезона
        cur.execute("SELECT COALESCE(MAX(season_num),0)+1 AS next FROM season_archive")
        season_num = cur.fetchone()["next"]

        top3_data = [dict(r) for r in top3_rows]

        # Архивируем сезон
        cur.execute(
            "INSERT INTO season_archive (season_num, ended_at, top_json) VALUES (%s,%s,%s)",
            (season_num, int(time.time()), json.dumps(top3_data))
        )

        # Выдаём награды победителям
        diamond_rewards = [30, 20, 10]
        for i, player in enumerate(top3_rows):
            uid = player["user_id"]
            diamonds = diamond_rewards[i] if i < len(diamond_rewards) else 5
            cur.execute("SELECT game_state FROM users WHERE user_id=%s", (uid,))
            gs_row = cur.fetchone()
            pstate = {}
            if gs_row and gs_row.get("game_state"):
                try:
                    pstate = json.loads(gs_row["game_state"])
                except Exception:
                    pass
            pstate["diamonds"] = int(pstate.get("diamonds", 0)) + diamonds
            cur.execute(
                "UPDATE users SET game_state=%s WHERE user_id=%s",
                (json.dumps(pstate, ensure_ascii=False), uid)
            )
            asyncio.ensure_future(notify_user(
                uid,
                f"🏆 <b>Сезон {season_num} завершён!</b>\n\n"
                f"Место: <b>#{i+1}</b> · Монет: <b>{player['game_balance']:,}</b>\n"
                f"Награда: <b>+{diamonds} 💎 алмазов</b> зачислены!"
            ))

        # Сбрасываем балансы и прокачки у всех
        # Читаем всех пользователей и сбрасываем game_state, сохраняя алмазы/скины/фоны
        cur.execute("SELECT user_id, game_state FROM users WHERE game_state IS NOT NULL")
        all_users = cur.fetchall()
        for u in all_users:
            try:
                st = json.loads(u["game_state"]) if u["game_state"] else {}
            except Exception:
                st = {}
            reset_state = {
                "coins": 0,
                "totalCoins": 0,
                "totalClicks": 0,
                "clickPower": 1,
                "perSecond": 0,
                "diamonds": 0,
                "upgrades": [],
                "activeSkin": "default",
                "activeBg": "default",
                "ownedSkins": [],
                "ownedBgs": [],
                "username": st.get("username", f"id{u['user_id']}"),
                "tgId": u["user_id"],
                "clan": st.get("clan"),
                "claimedAchs": {},
                "prestige": 0,
                "prestigeMultiplier": 1.0,
                "dailyStreak": 0,
                "lastDailyTs": 0,
                "_lastSeen": int(time.time()),
            }
            cur.execute(
                "UPDATE users SET game_balance=0, game_state=%s WHERE user_id=%s",
                (json.dumps(reset_state, ensure_ascii=False), u["user_id"])
            )
        # Для пользователей без game_state — просто обнуляем баланс
        cur.execute("UPDATE users SET game_balance=0 WHERE game_state IS NULL")

        # Записываем новый конец сезона (+30 дней) и timestamp сброса
        next_end = int(time.time()) + 30 * 86400
        now_ts = str(int(time.time()))
        for k, v in [("season_end_ts", str(next_end)), ("last_season_reset_ts", now_ts)]:
            cur.execute(
                "INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                (k, v)
            )
        conn.commit()

        logger.info(f"[SEASON_END] season={season_num} top3={top3_data} next_end={next_end}")
        return {
            "ok": True,
            "season_num": season_num,
            "top3": top3_data,
            "next_end_ts": next_end,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"end_season error: {e}")
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


@app.post("/api/season/start")
def start_season(secret: str = Query(""), days: int = Query(30)):
    admin_secret = os.environ.get("ADMIN_SECRET", "goldclick_admin_2024")
    if not secret or secret != admin_secret:
        raise HTTPException(403, "Forbidden")
    end_ts = int(time.time()) + days * 86400
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO settings(key,value) VALUES('season_end_ts',%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            (str(end_ts),)
        )
        conn.commit()
        return {"ok": True, "end_ts": end_ts, "days": days}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_conn(conn)


# ══════════════════════════════════════════════════════════════
# TELEGRAM WEBHOOK ENDPOINT
# ══════════════════════════════════════════════════════════════

@app.post("/tg-webhook")
async def tg_webhook_endpoint(request: Request):
    """Receive Telegram updates via webhook."""
    if not _bot_app:
        logger.warning("[WEBHOOK] Bot app not initialized")
        return {"ok": False, "error": "bot not ready"}
    try:
        from telegram import Update as TgUpdate
        data = await request.json()
        update = TgUpdate.de_json(data, _bot_app.bot)
        await _bot_app.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"[WEBHOOK] process_update error: {e}")
        return {"ok": False}


@app.get("/tg-webhook/info")
async def tg_webhook_info():
    """Check webhook status."""
    if not _bot_app:
        return {"webhook_active": False, "mode": "polling or not started"}
    try:
        info = await _bot_app.bot.get_webhook_info()
        return {
            "webhook_active": True,
            "url": info.url,
            "pending_update_count": info.pending_update_count,
            "mode": "webhook",
        }
    except Exception as e:
        return {"webhook_active": False, "error": str(e)}
