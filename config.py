# config.py — GOLDCLICK Bot
import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")

# --- Экономика ---
STARTING_BALANCE     = 1000
REFERRAL_BONUS       = 300

# --- Ежедневный бонус ---
DAILY_COOLDOWN       = 86400
DAILY_MIN            = 300
DAILY_MAX            = 800

# --- Опыт / Уровни ---
XP_PER_GAME          = 10
XP_PER_WIN           = 25
LEVEL_BASE_XP        = 100

# --- Ставки в играх ---
BET_PRESETS = [250, 750, 3000, 9000, 15000, 25000, 50000]

# --- Анти-спам ---
SPAM_LIMIT           = 5
SPAM_WINDOW          = 10
