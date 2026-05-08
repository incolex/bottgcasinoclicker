import subprocess
import sys
import os
import asyncio
import httpx
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

async def delete_webhook():
    """Удаляем вебхук и сбрасываем pending обновления перед запуском."""
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

# Сначала чистим вебхук
asyncio.run(delete_webhook())

# Небольшая пауза чтобы старый процесс бота успел умереть
print("[start] Ожидание 5 секунд перед запуском бота...")
time.sleep(5)

# Запускаем бота и API сервер одновременно
print("[start] Запуск бота и API сервера...")
bot = subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "main.py")])
api = subprocess.Popen([
    sys.executable, "-m", "uvicorn", "api_server:app",
    "--host", "0.0.0.0",
    "--port", str(os.environ.get("PORT", 10000))
])

# Ждём пока оба процесса работают
bot.wait()
api.wait()
