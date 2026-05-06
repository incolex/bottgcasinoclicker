# api_server.py — FastAPI сервер для GOLDCLICK Bot (PostgreSQL)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg2
import json
import time
import os
import config

app = FastAPI(title="GOLDCLICK API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def get_conn():
    return psycopg2.connect(os.environ.get("DATABASE_URL", ""))


def _row(cur, row):
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


class SaveStateRequest(BaseModel):
    tg_id: int
    state: dict


# ── Топ игроков ───────────────────────────────────────────────

@app.get("/api/top")
def top_players(limit: int = 20):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        """SELECT user_id,username,balance,game_balance,level FROM users
           ORDER BY (balance + COALESCE(game_balance,0)) DESC LIMIT %s""",
        (min(limit, 50),)
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return {"top": [{"user_id":r[0],"username":r[1] or f"id{r[0]}","coins":(r[2] or 0)+(r[3] or 0),"level":r[4] or 1} for r in rows]}


# ── Профиль ───────────────────────────────────────────────────

@app.get("/api/user/{user_id}")
def get_user(user_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
    row = _row(cur, cur.fetchone())
    cur.close(); conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return row


# ── Ранг ──────────────────────────────────────────────────────

@app.get("/api/rank/{user_id}")
def get_rank(user_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT balance,game_balance FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    if not row: cur.close(); conn.close(); raise HTTPException(status_code=404, detail="Не найден")
    total = (row[0] or 0) + (row[1] or 0)
    cur.execute("SELECT COUNT(*) FROM users WHERE (balance+COALESCE(game_balance,0))>%s", (total,))
    cnt = cur.fetchone()[0]
    cur.close(); conn.close()
    return {"rank": cnt+1, "total_coins": total}


# ── Статистика ────────────────────────────────────────────────

@app.get("/api/stats")
def global_stats():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*),SUM(balance) FROM users")
    row = cur.fetchone()
    cur.close(); conn.close()
    return {"total_players": row[0] or 0, "total_coins": row[1] or 0}


# ── Проверка / авторегистрация ────────────────────────────────

@app.get("/api/game/check/{tg_id}")
def check_user(tg_id: int, username: str = ""):
    if tg_id == 0:
        return {"registered": True, "username": "guest"}
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id,username FROM users WHERE user_id=%s", (tg_id,))
    row = cur.fetchone()
    if not row:
        uname = username or f"id{tg_id}"
        cur.execute(
            """INSERT INTO users (user_id,username,balance,game_balance,game_state,xp,level,
               games_played,wins,losses,daily_last,referrer_id,created_at,updated_at)
               VALUES (%s,%s,1000,0,NULL,0,1,0,0,0,0,NULL,%s,%s)""",
            (tg_id, uname, int(time.time()), int(time.time()))
        )
        conn.commit()
        cur.close(); conn.close()
        return {"registered": True, "username": uname, "new": True}
    cur.close(); conn.close()
    return {"registered": True, "username": row[1]}


# ── Загрузка состояния ────────────────────────────────────────

@app.get("/api/game/load/{tg_id}")
def load_game_state(tg_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=%s", (tg_id,))
    row = _row(cur, cur.fetchone())
    cur.close(); conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Сначала напиши /start боту!")

    state = None
    if row.get("game_state"):
        try:
            state = json.loads(row["game_state"])
        except:
            state = None

    db_game_balance = row.get("game_balance") or 0
    if state:
        state["coins"] = db_game_balance

    return {
        "found": True,
        "state": state,
        "db_game_balance": db_game_balance,
        "username": row.get("username"),
        "user_id": row.get("user_id"),
        "level": row.get("level"),
        "xp": row.get("xp"),
    }


# ── Сохранение состояния ──────────────────────────────────────

@app.post("/api/game/save")
def save_game_state(req: SaveStateRequest):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=%s", (req.tg_id,))
    if not cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="Пользователь не найден.")

    game_coins = int(req.state.get("coins", 0))
    cur.execute(
        "UPDATE users SET game_balance=%s,game_state=%s,updated_at=%s WHERE user_id=%s",
        (game_coins, json.dumps(req.state, ensure_ascii=False), int(time.time()), req.tg_id)
    )
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "saved_coins": game_coins}
