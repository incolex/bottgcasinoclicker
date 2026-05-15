import subprocess
import sys
import os
import ast
import asyncio
import httpx
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# ── Проверка синтаксиса main.py перед запуском ───────────────
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

if not check_syntax(os.path.join(BASE_DIR, "main.py")):
    print("[start] Бот не запущен из-за ошибки синтаксиса в main.py")
    sys.exit(1)

# ── Удаляем вебхук перед запуском ────────────────────────────
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

asyncio.run(delete_webhook())

# Пауза чтобы старый процесс успел умереть
print("[start] Ожидание 3 секунды...")
time.sleep(3)

# ── Запускаем бот и API одновременно ─────────────────────────
print("[start] Запуск бота и API сервера...")
bot = subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "main.py")])
api = subprocess.Popen([
    sys.executable, "-m", "uvicorn", "api_server:app",
    "--host", "0.0.0.0",
    "--port", str(os.environ.get("PORT", 10000))
])

bot.wait()
api.wait()
