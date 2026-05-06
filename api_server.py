# api_server.py — FastAPI сервер для GOLDCLICK Bot
# Запуск: uvicorn api_server:app --host 0.0.0.0 --port 8000
# В Termux: запусти в отдельном окне screen

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import sqlite3
import config

app = FastAPI(title="GOLDCLICK API")

# CORS — разрешаем запросы из HTML (GitHub Pages, Vercel и тд)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # В продакшене замени на свой домен
    allow_methods=["GET"],
    allow_headers=["*"],
)


def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Топ игроков ───────────────────────────────────────────────

@app.get("/api/top")
def top_players(limit: int = 20):
    """Возвращает топ игроков по totalCoins (balance + game_balance)."""
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
    """Возвращает данные конкретного игрока."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
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
    """Возвращает позицию игрока в глобальном рейтинге."""
    conn = get_conn()
    row = conn.execute(
        "SELECT balance, game_balance FROM users WHERE user_id=?", (user_id,)
    ).fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    total = (row["balance"] or 0) + (row["game_balance"] or 0)
    rank_row = conn.execute(
        """SELECT COUNT(*) as cnt FROM users
           WHERE (balance + COALESCE(game_balance, 0)) > ?""",
        (total,)
    ).fetchone()
    conn.close()

    return {"rank": (rank_row["cnt"] or 0) + 1, "total_coins": total}


# ── Общая статистика ──────────────────────────────────────────

@app.get("/api/stats")
def global_stats():
    """Общая статистика игры."""
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) as players, SUM(balance) as total FROM users"
    ).fetchone()
    conn.close()
    return {
        "total_players": row["players"] or 0,
        "total_coins": row["total"] or 0,
    }
