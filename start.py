import subprocess
import sys
import os

# Абсолютный путь к директории, где лежит start.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Запускаем бота и API сервер одновременно
bot = subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "main.py")])
api = subprocess.Popen([
    sys.executable, "-m", "uvicorn", "api_server:app",
    "--host", "0.0.0.0",
    "--port", str(os.environ.get("PORT", 10000))
])

# Ждём пока оба процесса работают
bot.wait()
api.wait()
