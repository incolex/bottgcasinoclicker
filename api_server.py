# api_server.py — FastAPI сервер для GOLDCLICK Bot
# Все данные хранятся в PostgreSQL, никакой локальной логики в HTML

from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import psycopg2
import psycopg2.extras
import json
import time
import os
import httpx
import asyncio
import logging
import datetime
import random
import math

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
        "ALTER TABLE transfers DROP COLUMN IF EXISTS user_id",
        "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS from_id BIGINT",
        "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS to_id BIGINT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS click_ban_until INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS click_ban_reason TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS inactive_banned BOOLEAN DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS inactive_ban_ts INTEGER DEFAULT 0",
    ]:
        try:
            cur.execute(sql); conn.commit()
        except Exception:
            conn.rollback()
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_users_game_balance ON users(game_balance DESC NULLS LAST) WHERE is_banned=FALSE",
        "CREATE INDEX IF NOT EXISTS idx_clan_members_user ON clan_members(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_daily_data_user ON daily_data(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_transfers_from ON transfers(from_id)",
        "CREATE INDEX IF NOT EXISTS idx_transfers_to ON transfers(to_id)",
    ]:
        try: cur.execute(idx_sql); conn.commit()
        except Exception: conn.rollback()
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

    # ИСПРАВЛЕНО: проверяем row ДО закрытия соединения
    if not row:
        cur.close(); conn.close()
        raise HTTPException(404, "Сначала напиши /start боту!")

    if row.get("is_banned"):
        cur.close(); conn.close()
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
        cur2.execute(
            "SELECT c.id, c.name, c.emoji, cm.role FROM clans c "
            "JOIN clan_members cm ON c.id=cm.clan_id WHERE cm.user_id=%s",
            (tg_id,)
        )
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

    # ИСПРАВЛЕНО: сброс истёкшего бана — используем то же соединение пока оно открыто
    if click_ban_until > 0 and click_ban_until < int(time.time()):
        click_ban_until = 0
        click_ban_reason = ""
        try:
            cur.execute(
                "UPDATE users SET click_ban_until=0, click_ban_reason='' WHERE user_id=%s",
                (tg_id,)
            )
            conn.commit()
        except Exception:
            conn.rollback()

    cur.close(); conn.close()

    # Извлекаем claimedAchs из state для явного возврата клиенту
    claimed_achs = {}
    if state and isinstance(state.get("claimedAchs"), dict):
        claimed_achs = state["claimedAchs"]

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
        "claimed_achs":      claimed_achs,
    }


class SaveStateRequest(BaseModel):
    tg_id: int
    state: dict
    coins: Optional[int] = None
    delta: Optional[int] = None
    exact: Optional[bool] = False

# Античит: максимальный delta за один save-запрос
_MAX_DELTA_PER_SAVE = 200_000
_MAX_BALANCE = 10_000_000_000  # 10 млрд


@app.post("/api/game/save")
def save_game_state(req: SaveStateRequest):
    conn = get_conn(); cur = conn.cursor()
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
            cur.close(); conn.close()
            raise HTTPException(404, "Пользователь не найден. Напишите /start боту.")
    if row["is_banned"]:
        cur.close(); conn.close()
        raise HTTPException(403, "Аккаунт заблокирован.")

    db_game_balance = int(row["game_balance"] or 0)

    state_to_save = dict(req.state)
    state_to_save.pop("coins", None)

    client_coins = int(req.coins) if req.coins is not None else None
    client_delta = int(req.delta) if req.delta is not None else 0

    if client_delta < 0:
        client_delta = 0
    if client_delta > _MAX_DELTA_PER_SAVE:
        logger.warning(f"[ANTICHEAT] tg_id={req.tg_id} delta={client_delta} exceeds max {_MAX_DELTA_PER_SAVE}, clamped")
        client_delta = _MAX_DELTA_PER_SAVE

    if req.exact and client_coins is not None:
        # exact=True — клиент явно списал монеты (апгрейд, покупка)
        # Античит: нельзя поставить больше чем в БД
        if client_coins > db_game_balance:
            logger.warning(f"[ANTICHEAT] tg_id={req.tg_id} exact coins={client_coins} > db={db_game_balance}, using db")
            client_coins = db_game_balance

    if client_delta > 0:
        # Клиент заработал монеты — добавляем дельту к БАЗЕ ИЗ БД
        # База всегда берётся из БД, клиентский base игнорируется
        # Это исключает восстановление баланса после казино/трейда/вывода
        new_balance = db_game_balance + client_delta
    elif req.exact and client_coins is not None and client_coins >= 0:
        # Точное списание (апгрейд, покупка алмаза) — берём client_coins
        new_balance = max(0, client_coins)
    else:
        # Обычный save без дельты и без exact:
        # НИКОГДА не поднимаем баланс выше того что в БД
        # Если клиент шлёт меньше — берём БД (клиент мог не успеть подгрузить)
        # Если клиент шлёт больше — тоже берём БД (казино/трейд уменьшили баланс)
        new_balance = db_game_balance
        if client_coins is not None and 0 < client_coins < db_game_balance:
            # Клиент показывает меньше — возможно он сам потратил (но без exact?)
            # Оставляем БД — безопаснее не уменьшать без явного подтверждения
            pass

    new_balance = max(0, min(new_balance, _MAX_BALANCE))

    # ── Читаем game_state из БД ОДИН РАЗ для всех проверок ──
    db_state = {}
    cur.execute("SELECT game_state FROM users WHERE user_id=%s", (req.tg_id,))
    gs_row = cur.fetchone()
    if gs_row and gs_row.get("game_state"):
        try:
            db_state = json.loads(gs_row["game_state"])
        except Exception:
            db_state = {}

    # ── ФИКС АЛМАЗОВ: берём MAX(db, client) — алмазы не могут исчезнуть сами ──
    # Уменьшение возможно только через явную покупку (exact=True)
    client_diamonds = int(state_to_save.get("diamonds") or 0)
    if not req.exact:
        db_diamonds = int(db_state.get("diamonds") or 0)
        if db_diamonds > client_diamonds:
            state_to_save["diamonds"] = db_diamonds
            logger.info(f"[DIAMONDS] uid={req.tg_id} kept db={db_diamonds} over client={client_diamonds}")

    # ── ФИКС OWNEDСKINS/OWNЕDBGS: объединяем массивы — купленное не пропадает ──
    db_owned_skins = db_state.get("ownedSkins") or []
    db_owned_bgs   = db_state.get("ownedBgs")   or []
    client_owned_skins = state_to_save.get("ownedSkins") or []
    client_owned_bgs   = state_to_save.get("ownedBgs")   or []
    # Union: всё что есть в БД + всё что есть у клиента
    merged_skins = list(set(db_owned_skins) | set(s for s in client_owned_skins if isinstance(s, str)))
    merged_bgs   = list(set(db_owned_bgs)   | set(b for b in client_owned_bgs   if isinstance(b, str)))
    if merged_skins:
        state_to_save["ownedSkins"] = merged_skins
    if merged_bgs:
        state_to_save["ownedBgs"]   = merged_bgs

    # ── ФИКС TOTALCLICKS: не уменьшаем никогда ──
    db_total_clicks = int(db_state.get("totalClicks") or 0)
    client_total_clicks = int(state_to_save.get("totalClicks") or 0)
    if db_total_clicks > client_total_clicks:
        state_to_save["totalClicks"] = db_total_clicks

    # ── ФИКС АЧИВОК: мержим claimedAchs из БД — не даём затереть уже полученные ──
    client_achs = state_to_save.get("claimedAchs") or {}
    db_achs = db_state.get("claimedAchs") or {}
    # Приоритет у БД: то что уже помечено — не стираем
    state_to_save["claimedAchs"] = {**client_achs, **db_achs}

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
        """SELECT u.user_id, u.username, u.game_balance, u.level,
                  (SELECT c2.name FROM clan_members cm2
                   JOIN clans c2 ON cm2.clan_id=c2.id
                   WHERE cm2.user_id=u.user_id LIMIT 1) AS clan_name
           FROM users u
           WHERE (u.is_banned IS NULL OR u.is_banned=FALSE)
             AND (u.inactive_banned IS NULL OR u.inactive_banned=FALSE)
             AND COALESCE(u.game_balance, 0) > 0
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
        "SELECT COUNT(*) FROM users WHERE (is_banned IS NULL OR is_banned=FALSE) "
        "AND (inactive_banned IS NULL OR inactive_banned=FALSE) "
        "AND COALESCE(game_balance,0)>%s",
        (bal,)
    )
    cnt_row = cur.fetchone()
    cnt = cnt_row["count"] if cnt_row else 0
    cur.close(); conn.close()
    return {"rank": cnt + 1, "game_balance": bal}


@app.api_route("/api/stats", methods=["GET", "HEAD"])
def global_stats():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(game_balance) FROM users WHERE is_banned=FALSE")
    row = cur.fetchone()
    cur.close(); conn.close()
    return {"total_players": row["count"] or 0, "total_coins": int(row["sum"] or 0)}


# Таймер сезона хранится в БД — переживает рестарты

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

    # ИСПРАВЛЕНО: откатываем транзакцию при ошибке, season_num считается безопасно
    season_num = 1
    try:
        cur.execute("SELECT COALESCE(MAX(season_num),0)+1 AS season_num FROM season_archive")
        season_num = cur.fetchone()["season_num"]
    except Exception:
        conn.rollback()

    cur.close(); conn.close()

    started_ago = 0
    try:
        conn2 = get_conn(); cur2 = conn2.cursor()
        cur2.execute("SELECT MIN(created_at) AS oldest FROM users")
        row2 = cur2.fetchone()
        cur2.close(); conn2.close()
        oldest = int(row2["oldest"] or 0) if row2 else 0
        started_ago = max(0, int((time.time() - oldest) / 86400)) if oldest > 0 else 0
    except Exception:
        pass

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
    conn = get_conn(); cur = conn.cursor()
    _set_season_ts(cur, req.end_ts)
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "end_ts": req.end_ts}


class SeasonRewardRequest(BaseModel):
    user_id: int
    diamonds: int

@app.post("/api/season/reward")
def give_season_reward(req: SeasonRewardRequest):
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
# МИНЫ — полная реализация
# ══════════════════════════════════════════════════════════════
# Таблица 5x5 = 25 клеток. mines — кол-во мин (1-24).
# Множитель рассчитывается по формуле с учётом RTP ~97%

MINES_GRID_SIZE = 25
MINES_RTP = 0.97  # 97% возврат

def _mines_multiplier(mines: int, opened: int) -> float:
    """
    Честный множитель на основе гипергеометрического распределения.
    При каждом открытии учитываем вероятность не попасть на мину.
    """
    safe_cells = MINES_GRID_SIZE - mines
    if opened <= 0 or opened > safe_cells:
        return 1.0

    # P(открыть opened безопасных клеток подряд)
    prob = 1.0
    for i in range(opened):
        prob *= (safe_cells - i) / (MINES_GRID_SIZE - i)

    if prob <= 0:
        return 1.0

    multiplier = (MINES_RTP / prob)
    return round(multiplier, 4)


class MinesStartRequest(BaseModel):
    tg_id: int
    bet: int
    mines: int  # 1–24


@app.post("/api/mines/start")
def mines_start(req: MinesStartRequest):
    """Начать игру в мины: поставить ставку, сгенерировать позиции мин."""
    if req.bet <= 0:
        raise HTTPException(400, "Ставка должна быть > 0")
    if not (1 <= req.mines <= 24):
        raise HTTPException(400, "Количество мин: 1–24")

    conn = get_conn(); cur = conn.cursor()

    # Проверить баланс
    cur.execute("SELECT game_balance, is_banned FROM users WHERE user_id=%s", (req.tg_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(404, "Пользователь не найден")
    if row["is_banned"]:
        cur.close(); conn.close()
        raise HTTPException(403, "Аккаунт заблокирован")

    balance = int(row["game_balance"] or 0)
    if balance < req.bet:
        cur.close(); conn.close()
        raise HTTPException(400, f"Недостаточно монет. Баланс: {balance}")

    # Если уже есть активная сессия — форфейтим её (ставка уже была списана)
    cur.execute("SELECT bet FROM mines_sessions WHERE user_id=%s", (req.tg_id,))
    existing = cur.fetchone()
    if existing:
        cur.execute("DELETE FROM mines_sessions WHERE user_id=%s", (req.tg_id,))
        conn.commit()

    # Генерируем позиции мин (случайные уникальные индексы 0-24)
    mine_positions = random.sample(range(MINES_GRID_SIZE), req.mines)

    # Списываем ставку
    new_balance = balance - req.bet
    cur.execute(
        "UPDATE users SET game_balance=%s, updated_at=%s WHERE user_id=%s",
        (new_balance, int(time.time()), req.tg_id)
    )

    # Сохраняем сессию
    cur.execute(
        """INSERT INTO mines_sessions (user_id, bet, mines, mine_positions, opened_cells, created_at)
           VALUES (%s, %s, %s, %s, '', %s)
           ON CONFLICT (user_id) DO UPDATE SET
               bet=%s, mines=%s, mine_positions=%s, opened_cells='', created_at=%s""",
        (req.tg_id, req.bet, req.mines,
         json.dumps(mine_positions), int(time.time()),
         req.bet, req.mines, json.dumps(mine_positions), int(time.time()))
    )
    conn.commit(); cur.close(); conn.close()

    # Первый множитель (0 открытых клеток = x1.0)
    return {
        "ok": True,
        "new_balance": new_balance,
        "multiplier": 1.0,
        "next_multiplier": _mines_multiplier(req.mines, 1),
        "opened_cells": [],
        "mines_count": req.mines,
        "safe_count": MINES_GRID_SIZE - req.mines,
    }


class MinesOpenRequest(BaseModel):
    tg_id: int
    cell: int  # 0-24


@app.post("/api/mines/open")
def mines_open(req: MinesOpenRequest):
    """Открыть клетку в активной игре мины."""
    if not (0 <= req.cell < MINES_GRID_SIZE):
        raise HTTPException(400, f"Клетка должна быть от 0 до {MINES_GRID_SIZE - 1}")

    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM mines_sessions WHERE user_id=%s", (req.tg_id,))
    session = cur.fetchone()
    if not session:
        cur.close(); conn.close()
        raise HTTPException(404, "Активная игра не найдена. Начните новую игру.")

    bet = int(session["bet"])
    mines_count = int(session["mines"])
    mine_positions: List[int] = json.loads(session["mine_positions"])
    opened_raw = session["opened_cells"] or ""
    opened_cells: List[int] = json.loads(opened_raw) if opened_raw.strip().startswith("[") else []

    if req.cell in opened_cells:
        cur.close(); conn.close()
        raise HTTPException(400, "Клетка уже открыта")

    is_mine = req.cell in mine_positions

    if is_mine:
        # Попали на мину — игра окончена, ставка потеряна
        cur.execute("DELETE FROM mines_sessions WHERE user_id=%s", (req.tg_id,))
        # Обновить статистику
        cur.execute(
            "UPDATE users SET games_played=games_played+1, losses=losses+1 WHERE user_id=%s",
            (req.tg_id,)
        )
        conn.commit()
        cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (req.tg_id,))
        bal_row = cur.fetchone()
        cur.close(); conn.close()
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

    # Безопасная клетка
    opened_cells.append(req.cell)
    safe_opened = len(opened_cells)
    safe_total = MINES_GRID_SIZE - mines_count

    current_multiplier = _mines_multiplier(mines_count, safe_opened)
    potential_win = int(bet * current_multiplier)

    # Если открыты все безопасные клетки — авто-кэшаут
    if safe_opened >= safe_total:
        cur.execute("DELETE FROM mines_sessions WHERE user_id=%s", (req.tg_id,))
        cur.execute(
            "UPDATE users SET game_balance=game_balance+%s, games_played=games_played+1, wins=wins+1, updated_at=%s WHERE user_id=%s",
            (potential_win, int(time.time()), req.tg_id)
        )
        conn.commit()
        cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (req.tg_id,))
        bal_row = cur.fetchone()
        cur.close(); conn.close()
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

    # Обновить сессию
    next_multiplier = _mines_multiplier(mines_count, safe_opened + 1)
    cur.execute(
        "UPDATE mines_sessions SET opened_cells=%s WHERE user_id=%s",
        (json.dumps(opened_cells), req.tg_id)
    )
    conn.commit(); cur.close(); conn.close()

    return {
        "ok": True,
        "hit_mine": False,
        "cell": req.cell,
        "opened_cells": opened_cells,
        "win": potential_win,
        "new_balance": None,  # баланс не меняется до cashout
        "multiplier": current_multiplier,
        "next_multiplier": next_multiplier,
    }


class MinesCashoutRequest(BaseModel):
    tg_id: int


@app.post("/api/mines/cashout")
def mines_cashout(req: MinesCashoutRequest):
    """Забрать выигрыш в активной игре мины."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM mines_sessions WHERE user_id=%s", (req.tg_id,))
    session = cur.fetchone()
    if not session:
        cur.close(); conn.close()
        raise HTTPException(404, "Активная игра не найдена")

    bet = int(session["bet"])
    mines_count = int(session["mines"])
    mine_positions: List[int] = json.loads(session["mine_positions"])
    opened_raw = session["opened_cells"] or ""
    opened_cells: List[int] = json.loads(opened_raw) if opened_raw.strip().startswith("[") else []
    safe_opened = len(opened_cells)

    if safe_opened == 0:
        # Не открыто ни одной клетки — возвращаем ставку
        multiplier = 1.0
        win = bet
    else:
        multiplier = _mines_multiplier(mines_count, safe_opened)
        win = int(bet * multiplier)

    # Удалить сессию и начислить выигрыш
    cur.execute("DELETE FROM mines_sessions WHERE user_id=%s", (req.tg_id,))
    cur.execute(
        "UPDATE users SET game_balance=game_balance+%s, games_played=games_played+1, wins=wins+1, updated_at=%s WHERE user_id=%s",
        (win, int(time.time()), req.tg_id)
    )
    conn.commit()
    cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (req.tg_id,))
    bal_row = cur.fetchone()
    cur.close(); conn.close()

    return {
        "ok": True,
        "win": win,
        "multiplier": multiplier,
        "opened_cells": opened_cells,
        "mine_positions": mine_positions,
        "new_balance": int(bal_row["game_balance"]) if bal_row else 0,
    }


@app.get("/api/mines/session/{tg_id}")
def mines_get_session(tg_id: int):
    """Получить текущую активную сессию мин (для восстановления после перезагрузки)."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM mines_sessions WHERE user_id=%s", (tg_id,))
    session = cur.fetchone()
    cur.close(); conn.close()

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


# ══════════════════════════════════════════════════════════════
# АНТИ-КЛИКЕР — сохранить бан
# ══════════════════════════════════════════════════════════════

class ClickBanRequest(BaseModel):
    tg_id: int
    reason: str = ""
    ban_until: Optional[int] = None

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
    logger.warning(f"[ANTI-CLICKER] tg_id={req.tg_id} ban_until={ban_until} reason={req.reason}")
    cur.close(); conn.close()
    return {
        "ok": True,
        "ban_until": ban_until,
        "ban_until_readable": time.strftime("%H:%M:%S %d.%m", time.localtime(ban_until))
    }

@app.get("/api/game/click_ban/{tg_id}")
def get_click_ban(tg_id: int):
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
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT inactive_banned, inactive_ban_ts FROM users WHERE user_id=%s", (req.tg_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return {"inactive_banned": False}
    return {
        "inactive_banned": bool(row["inactive_banned"]),
        "inactive_ban_ts": int(row["inactive_ban_ts"] or 0),
    }

@app.post("/api/game/self_unban")
async def self_unban(req: InactiveBanCheckRequest):
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
    return {"ok": True, "unbanned": True}

@app.post("/api/game/apply_inactive_bans")
def apply_inactive_bans():
    conn = get_conn(); cur = conn.cursor()
    cutoff = int(time.time()) - 3 * 86400
    try:
        cur.execute("""
            SELECT u.user_id FROM users u
            LEFT JOIN daily_data d ON u.user_id = d.user_id
            WHERE (u.is_banned IS NULL OR u.is_banned = FALSE)
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
    except Exception as e:
        conn.rollback()
        logger.error(f"apply_inactive_bans error: {e}")
        cur.close(); conn.close()
        raise HTTPException(500, "Ошибка при применении банов")
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
# КАЗИНО-БОТ
# ══════════════════════════════════════════════════════════════

class CasinoDepositRequest(BaseModel):
    tg_id: int
    amount: int


@app.post("/api/casino_deposit")
async def casino_deposit(req: CasinoDepositRequest):
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
    new_bot  = bot_bal  + req.amount
    cur.execute(
        "UPDATE users SET game_balance=%s, balance=%s, updated_at=%s WHERE user_id=%s",
        (new_game, new_bot, int(time.time()), req.tg_id)
    )
    cur.execute(
        "INSERT INTO transfers (from_id,to_id,direction,amount,note,created_at) VALUES (%s,%s,'casino',%s,'Казино через игру',%s)",
        (req.tg_id, req.tg_id, req.amount, int(time.time()))
    )
    conn.commit(); cur.close(); conn.close()

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

    lock_ids = sorted([req.from_id, to_id])
    cur.execute("SELECT user_id FROM users WHERE user_id=ANY(%s) FOR UPDATE", (lock_ids,))

    cur.execute("SELECT game_balance, username FROM users WHERE user_id=%s", (req.from_id,))
    from_row = cur.fetchone()
    if not from_row:
        cur.close(); conn.close()
        raise HTTPException(404, "Отправитель не найден")
    from_game_bal = int(from_row["game_balance"] or 0)
    from_name     = from_row["username"] or f"id{req.from_id}"

    if from_game_bal < total:
        cur.close(); conn.close()
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
    cur.close(); conn.close()

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
    r.pop("game_state", None)
    return r


class ClanKickRequest(BaseModel):
    tg_id: int
    kick_id: int


@app.post("/api/clans/kick")
def clan_kick(req: ClanKickRequest):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT role, clan_id FROM clan_members WHERE user_id=%s", (req.tg_id,))
    r = cur.fetchone()
    if not r or r["role"] != "leader":
        cur.close(); conn.close()
        raise HTTPException(403, "Только лидер может исключать участников")
    clan_id = r["clan_id"]
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
# STREAK & DAILY QUESTS
# ══════════════════════════════════════════════════════════════

class StreakClaimRequest(BaseModel):
    tg_id: int

@app.get("/api/daily/{tg_id}")
def get_daily_data(tg_id: int):
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
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM daily_data WHERE user_id=%s", (req.tg_id,))
    row = cur.fetchone()
    now = int(time.time())
    today = datetime.date.today().isoformat()

    streak_days    = int(row["streak_days"] or 0) if row else 0
    streak_last_ts = int(row["streak_last_ts"] or 0) if row else 0

    last_date = datetime.date.fromtimestamp(streak_last_ts).isoformat() if streak_last_ts > 0 else ""
    if last_date == today:
        cur.close(); conn.close()
        raise HTTPException(400, "Бонус уже получен сегодня")

    days_since = (now - streak_last_ts) / 86400 if streak_last_ts > 0 else 999
    if streak_last_ts == 0 or days_since > 2:
        new_days = 1
    else:
        new_days = streak_days + 1

    bonus = (streak_days) * 50
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
    conn.commit(); cur.close(); conn.close()

    return {
        "ok": True,
        "reward": reward,
        "streak_days": new_days,
        "streak_last_ts": now,
        "new_balance": new_balance,
    }


# ИСПРАВЛЕНО: используем Pydantic-модель вместо dict
class QuestClaimRequest(BaseModel):
    tg_id: int
    quest_id: str
    reward: int

@app.post("/api/daily/quest_claim")
def claim_quest(req: QuestClaimRequest):
    if not req.tg_id or not req.quest_id or req.reward <= 0:
        raise HTTPException(400, "Bad request")

    # Лимит награды — защита от накрутки через прямые запросы
    MAX_QUEST_REWARD = 500
    safe_reward = min(req.reward, MAX_QUEST_REWARD)

    today = datetime.date.today().isoformat()

    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT quests_json, quests_date FROM daily_data WHERE user_id=%s", (req.tg_id,))
    row = cur.fetchone()

    quests = {}
    if row:
        qdate = row["quests_date"] or ""
        if qdate == today:
            try: quests = json.loads(row["quests_json"] or "{}")
            except: quests = {}
        # Если дата другая — новый день, квесты сбрасываются автоматически

    if quests.get(req.quest_id):
        cur.close(); conn.close()
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
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "new_balance": int(nb["game_balance"]) if nb else 0}


# ── Серверный клейм ачивок — защита от повторного получения ──

# Список допустимых ачивок и максимальных наград (захардкожено на сервере)
_VALID_ACHS = {
    "click100":  5000,
    "click1k":   15000,
    "click10k":  50000,
    "earn1k":    5000,
    "earn100k":  25000,
    "earn1m":    100000,
    "earn1b":    777000,
    "ps10":      20000,
    "ps100":     150000,
    "click100p": 75000,
}

class AchClaimRequest(BaseModel):
    tg_id: int
    ach_id: str
    reward: int

@app.post("/api/ach/claim")
def claim_achievement(req: AchClaimRequest):
    if not req.tg_id or not req.ach_id:
        raise HTTPException(400, "Bad request")

    max_reward = _VALID_ACHS.get(req.ach_id)
    if max_reward is None:
        raise HTTPException(400, f"Unknown achievement: {req.ach_id}")
    safe_reward = min(req.reward, max_reward)

    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT game_state FROM users WHERE user_id=%s", (req.tg_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(404, "Пользователь не найден")

    state = {}
    if row.get("game_state"):
        try: state = json.loads(row["game_state"])
        except: state = {}

    claimed_achs = state.get("claimedAchs") or {}
    if claimed_achs.get(req.ach_id):
        cur.close(); conn.close()
        raise HTTPException(400, "Достижение уже получено")

    claimed_achs[req.ach_id] = True
    state["claimedAchs"] = claimed_achs

    cur.execute(
        "UPDATE users SET game_balance=game_balance+%s, game_state=%s, updated_at=%s WHERE user_id=%s",
        (safe_reward, json.dumps(state, ensure_ascii=False), int(time.time()), req.tg_id)
    )
    cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (req.tg_id,))
    nb = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    logger.info(f"[ACH_CLAIM] uid={req.tg_id} ach={req.ach_id} reward={safe_reward}")
    return {"ok": True, "new_balance": int(nb["game_balance"]) if nb else 0, "ach_id": req.ach_id}





# ── Топ с полем streak ────────────────────────────────────────
@app.get("/api/top/extended")
def top_extended(limit: int = 30):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        """SELECT u.user_id, u.username, u.game_balance, u.level,
                  (SELECT c2.name FROM clan_members cm2
                   JOIN clans c2 ON cm2.clan_id=c2.id
                   WHERE cm2.user_id=u.user_id LIMIT 1) AS clan_name,
                  COALESCE(d.streak_days, 0) AS streak_days
           FROM users u
           LEFT JOIN daily_data d ON u.user_id = d.user_id
           WHERE (u.is_banned IS NULL OR u.is_banned=FALSE)
             AND (u.inactive_banned IS NULL OR u.inactive_banned=FALSE)
             AND COALESCE(u.game_balance, 0) > 0
             AND (u.click_ban_until IS NULL OR u.click_ban_until = 0 OR u.click_ban_until < EXTRACT(EPOCH FROM NOW())::BIGINT)
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
# ADMIN INACTIVE UNBAN
# ══════════════════════════════════════════════════════════════

class AdminUnbanInactiveRequest(BaseModel):
    tg_id: int

@app.post("/api/game/admin_unban_inactive")
def admin_unban_inactive(req: AdminUnbanInactiveRequest):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        "UPDATE users SET inactive_banned=FALSE, inactive_ban_ts=0 WHERE user_id=%s",
        (req.tg_id,)
    )
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}
