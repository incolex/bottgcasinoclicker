# config.py — Настройки бота (обновленная экономика и предметы)
import os

# ВАЖНО: токен лучше хранить в .env файле
# Для продакшена: BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")

# --- Экономика ---
STARTING_BALANCE     = 1000
DAILY_BONUS          = 500
REFERRAL_BONUS       = 300

# --- Ежедневный бонус (ИСПРАВЛЕНО: добавлены отсутствующие переменные) ---
DAILY_COOLDOWN       = 86400   # 24 часа в секундах
DAILY_MIN            = 300     # минимальный бонус
DAILY_MAX            = 800     # максимальный бонус

# --- Опыт / Уровни ---
XP_PER_GAME          = 10
XP_PER_WIN           = 25
LEVEL_BASE_XP        = 100

# --- Казино ---
SLOT_MAX_BET         = 10000
BLACKJACK_MIN_BET    = 50
ROULETTE_MIN_BET     = 50
DUEL_MIN_BET         = 100

# --- Лиги (пороги для побед) ---
LEAGUES = {
    "Железо": 0, "Бронза": 5, "Серебро": 15, "Золото": 30,
    "Платина": 60, "Алмаз": 100, "Элита": 150, "Бессмертие": 250
}

# --- Генераторы дохода ---
INCOME_GENERATORS = {
    "farm":         {"name": "🌿 Ферма",          "price": 1500,   "income": 5,    "desc": "Выращивает монеты"},
    "shop_market":  {"name": "🏪 Магазин",        "price": 5000,   "income": 15,   "desc": "Приносит доход с продаж"},
    "base_military": {"name": "🪖 Военная база",  "price": 12000,  "income": 35,   "desc": "Обеспечивает безопасность за плату"},
    "factory":      {"name": "🏭 Завод",          "price": 30000,  "income": 80,   "desc": "Промышленное производство"},
    "bank":         {"name": "🏦 Банк",           "price": 75000,  "income": 180,  "desc": "Финансовые операции"},
    "oil_rig":      {"name": "🏗️ Нефтяная вышка", "price": 150000, "income": 350,  "desc": "Черное золото"},
    "corporation":  {"name": "🏢 Корпорация",     "price": 300000, "income": 700,  "desc": "Крупный бизнес"},
    "tech_lab":     {"name": "🔬 Тех-лаборатория", "price": 600000,"income": 1400,  "desc": "Инновации и патенты"},
    "space_center": {"name": "🚀 Космоцентр",      "price": 1500000,"income": 3000, "desc": "Освоение космоса"},
    "ai_core":      {"name": "🧠 ИИ-Ядро",       "price": 4000000,"income": 7000,  "desc": "Искусственный интеллект управляет рынками"},
    "city":         {"name": "🌆 Мегаполис",      "price": 10000000,"income": 15000,"desc": "Целый город работает на вас"},
    "empire":       {"name": "👑 Империя",        "price": 50000000,"income": 50000,"desc": "Мировое господство"},
}

# --- Магазин предметов ---
SHOP_ITEMS = {
    "lucky_charm":  {"name": "🍀 Талисман удачи", "price": 2000,  "desc": "+5% к выигрышу в слотах"},
    "shield":       {"name": "🛡️ Щит",             "price": 1500,  "desc": "Защита от потери в дуэли (1 раз)"},
    "xp_boost":     {"name": "⚡ Буст опыта",       "price": 1000,  "desc": "×2 XP на 24 часа"},
    "vip_pass":     {"name": "👑 VIP-пропуск",     "price": 5000,  "desc": "VIP статус на 7 дней"},
    "mystery_box":  {"name": "🎁 Тайный ящик",     "price": 800,   "desc": "Случайный предмет или монеты"},
    "lottery":      {"name": "🎟️ Лотерейный билет", "price": 200,  "desc": "Шанс выиграть крупный приз"},
}

# --- Клан ---
CLAN_CREATE_COST     = 5000
CLAN_MAX_MEMBERS     = 50

# --- Анти-спам ---
SPAM_LIMIT           = 5
SPAM_WINDOW          = 10

# --- Путь к БД ---
DB_PATH              = "bot_database.db"

# --- Battle Pass ---
BP_LEVELS = 50
BP_XP_PER_LEVEL = 100
BP_FREE_REWARDS = {
    5: 500, 10: 1000, 15: 1500, 20: "lucky_charm", 25: 2500,
    30: "shield", 35: 3000, 40: "xp_boost", 45: 5000, 50: "vip_pass"
}
BP_PREMIUM_REWARDS = {
    1: 2000, 2: "lucky_charm", 3: 1000, 7: 3000, 10: "xp_boost",
    14: "shield", 18: 5000, 22: 4000, 26: "xp_boost", 30: 10000,
    35: "vip_pass", 40: 15000, 45: "lucky_charm", 50: 50000
}
BP_PREMIUM_COST = 5000
