import subprocess
import sys
import os
import ast
import asyncio
import httpx
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")   # https://your-app.onrender.com
PORT = int(os.environ.get("PORT", 10000))

# ── Проверка синтаксиса ───────────────────────────────────────
def check_syntax(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            src = f.read()
        ast.parse(src)
        print(f"[start] ✅ Синтаксис {os.path.basename(path)} OK")
        return True
    except SyntaxError as e:
        print(f"[start] ❌ SyntaxError в {os.path.basename(path)}: {e}")
        return False

for fname in ("main.py", "api_server.py"):
    if not check_syntax(os.path.join(BASE_DIR, fname)):
        print(f"[start] Не запущено из-за ошибки синтаксиса в {fname}")
        sys.exit(1)

# ── Удаляем вебхук если работаем в polling-режиме ────────────
async def delete_webhook():
    if not BOT_TOKEN:
        print("[start] BOT_TOKEN не задан, пропускаем удаление вебхука")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            print(f"[start] deleteWebhook → {r.json()}")
    except Exception as e:
        print(f"[start] Ошибка при удалении вебхука: {e}")

if not WEBHOOK_URL:
    # Polling mode: очищаем старый webhook
    asyncio.run(delete_webhook())
    print("[start] Ожидание 3 секунды...")
    time.sleep(3)
else:
    print(f"[start] Webhook mode: {WEBHOOK_URL}/tg-webhook")
    print("[start] Webhook будет установлен автоматически при старте FastAPI")

# ── Запуск процессов ──────────────────────────────────────────
print("[start] Запуск API сервера и бота...")

api_proc = subprocess.Popen([
    sys.executable, "-m", "uvicorn", "api_server:app",
    "--host", "0.0.0.0",
    "--port", str(PORT),
    "--workers", "1",      # один воркер — бот-приложение singleton
])

if WEBHOOK_URL:
    # В webhook режиме main.py просто ждёт (обновления идут через FastAPI)
    bot_proc = subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "main.py")])
else:
    # Polling режим
    bot_proc = subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "main.py")])

bot_proc.wait()
api_proc.wait()
