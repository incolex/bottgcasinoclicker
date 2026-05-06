# api_server.py — FastAPI сервер для GOLDCLICK Bot
# Запуск: uvicorn api_server:app --host 0.0.0.0 --port 8000

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import json
import time
import config

app = FastAPI(title="GOLDCLICK API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Модели ────────────────────────────────────────────────────

class SaveStateRequest(BaseModel):
    tg_id: int
    state: dict  # весь объект S из игры


# ── Топ игроков ───────────────────────────────────────────────

@app.get("/api/top")
def top_players(limit: int = 20):
    conn = get_conn()
    rows = conn.execute(
        """SELECT user_id, username, balance, game_balance, level
           FROM users
           ORDER BY (balance + COALESCE(game_balance, 0)) DESC
           LIMIT ?""",
        (min(limit, 50),)
    ).fetchall()
    conn.close()
    return {
        "top": [
            {
                "user_id": r["user_id"],
                "username": r["username"] or f"id{r['user_id']}",
                "coins": (r["balance"] or 0) + (r["game_balance"] or 0),
                "level": r["level"] or 1,
            }
            for r in rows
        ]
    }


# ── Профиль игрока ────────────────────────────────────────────

@app.get("/api/user/{user_id}")
def get_user(user_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "balance": row["balance"],
        "game_balance": row["game_balance"] or 0,
        "level": row["level"],
        "xp": row["xp"],
        "wins": row["wins"],
        "losses": row["losses"],
        "games_played": row["games_played"],
    }


# ── Ранг игрока ───────────────────────────────────────────────

@app.get("/api/rank/{user_id}")
def get_rank(user_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT balance, game_balance FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    total = (row["balance"] or 0) + (row["game_balance"] or 0)
    rank_row = conn.execute(
        "SELECT COUNT(*) as cnt FROM users WHERE (balance + COALESCE(game_balance,0)) > ?",
        (total,)
    ).fetchone()
    conn.close()
    return {"rank": (rank_row["cnt"] or 0) + 1, "total_coins": total}


# ── Общая статистика ──────────────────────────────────────────

@app.get("/api/stats")
def global_stats():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) as players, SUM(balance) as total FROM users").fetchone()
    conn.close()
    return {"total_players": row["players"] or 0, "total_coins": row["total"] or 0}


# ── ПРОВЕРИТЬ — зарегистрирован ли пользователь ──────────────

@app.get("/api/game/check/{tg_id}")
def check_user(tg_id: int):
    """Проверяет, есть ли пользователь в боте (нажимал /start)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT user_id, username FROM users WHERE user_id=?", (tg_id,)
    ).fetchone()
    conn.close()
    if not row:
        return {"registered": False}
    return {"registered": True, "username": row["username"]}


# ── ЗАГРУЗИТЬ состояние игры ──────────────────────────────────

@app.get("/api/game/load/{tg_id}")
def load_game_state(tg_id: int):
    """
    Загружает полное состояние игры.
    Если юзер не зарегистрирован в боте — 404.
    game_balance из БД всегда приоритетнее (бот мог изменить его).
    """
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (tg_id,)).fetchone()
    conn.close()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Сначала напиши /start боту чтобы зарегистрироваться!"
        )

    state = None
    if "game_state" in row.keys() and row["game_state"]:
        try:
            state = json.loads(row["game_state"])
        except Exception:
            state = None

    db_game_balance = row["game_balance"] or 0

    # Если есть сохранённое состояние — синхронизируем монеты с БД
    if state:
        state["coins"] = db_game_balance

    return {
        "found": True,
        "state": state,
        "db_game_balance": db_game_balance,
        "username": row["username"],
        "user_id": row["user_id"],
        "level": row["level"],
        "xp": row["xp"],
    }


# ── СОХРАНИТЬ состояние игры ──────────────────────────────────

@app.post("/api/game/save")
def save_game_state(req: SaveStateRequest):
    """
    Сохраняет полный JSON-стейт игры.
    Обновляет game_balance = текущие монеты (синхронизация с ботом).
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT user_id FROM users WHERE user_id=?", (req.tg_id,)
    ).fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Пользователь не найден. Сначала /start в боте.")

    game_coins = int(req.state.get("coins", 0))

    conn.execute(
        """UPDATE users
           SET game_balance=?, game_state=?, updated_at=?
           WHERE user_id=?""",
        (game_coins, json.dumps(req.state, ensure_ascii=False), int(time.time()), req.tg_id)
    )
    conn.commit()
    conn.close()

    return {"ok": True, "saved_coins": game_coins}
