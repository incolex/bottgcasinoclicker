import subprocess
import sys
import os

# Запускаем бота и API сервер одновременно
bot = subprocess.Popen([sys.executable, "main.py"])
api = subprocess.Popen([
    sys.executable, "-m", "uvicorn", "api_server:app",
    "--host", "0.0.0.0",
    "--port", str(os.environ.get("PORT", 8000))
])

# Ждём пока оба процесса работают
bot.wait()
api.wait()
