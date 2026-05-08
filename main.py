# main.py — GOLDCLICK Bot (python-telegram-bot 20.x)

import logging
import asyncio
from dotenv import load_dotenv
load_dotenv()
import time
import random
import datetime

from telegram import (
    Update, ReplyKeyboardMarkup,
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import json

import config
import database as db
from database import get_conn
import games

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DEVELOPER_ID = 8556838707
_admin_ids: set = {DEVELOPER_ID}

user_sessions: dict = {}
spam_tracker: dict = {}

WEBAPP_URL = "https://incolex.github.io/bottgcasinoclicker/clicker.html"

# ── Ставки ───────────────────────────────────────────────────
BETS = config.BET_PRESETS  # [250, 750, 3000, 9000, 15000, 25000, 50000]


# ── Клавиатуры ───────────────────────────────────────────────

def make_main_kb():
    return ReplyKeyboardMarkup(
        [
            ["🎰 Игры", "📊 Профиль"],
            ["🎁 Ежедневный бонус"],
        ],
        resize_keyboard=True
    )


GAMES_KB = ReplyKeyboardMarkup(
    [
        ["🎰 Слоты", "🃏 Блэкджек"],
        ["🎯 Рулетка", "💣 Мины"],
        ["◀️ Назад"],
    ],
    resize_keyboard=True
)


# ── Утилиты ──────────────────────────────────────────────────

def check_spam(user_id):
    now = time.time()
    hist = [t for t in spam_tracker.get(user_id, []) if now - t < config.SPAM_WINDOW]
    hist.append(now)
    spam_tracker[user_id] = hist
    return len(hist) > config.SPAM_LIMIT


def level_bar(xp, level):
    needed = level * config.LEVEL_BASE_XP
    filled = int((xp / needed) * 10) if needed else 0
    return "█" * filled + "░" * (10 - filled)


async def give_xp_notify(update, user_id, xp):
    leveled, new_lvl = db.add_xp(user_id, xp)
    if leveled:
        await update.effective_message.reply_text(
            f"⬆️ <b>Уровень повышен!</b> Теперь вы <b>{new_lvl} уровня</b>!",
            parse_mode="HTML"
        )


def is_admin(user_id: int) -> bool:
    return user_id in _admin_ids


def bet_keyboard(prefix: str):
    """Генерирует клавиатуру со стандартными ставками."""
    rows = []
    row = []
    for b in BETS:
        label = f"{b:,}".replace(",", " ")
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}{b}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Своя ставка", callback_data=f"{prefix}custom")])
    return InlineKeyboardMarkup(rows)


# ── Бан-фильтр ────────────────────────────────────────────────

async def ban_check(update: Update) -> bool:
    """Возвращает True если пользователь забанен (обработка прекращается)."""
    user = update.effective_user
    if user and db.is_banned(user.id):
        return True
    return False


# ── /start ───────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await ban_check(update):
        return
    user = update.effective_user
    args = ctx.args
    referrer_id = int(args[0]) if args and args[0].isdigit() else None
    db.ensure_user(user.id, user.username or user.first_name, referrer_id)
    row = db.get_user(user.id)
    game_bal = row["game_balance"] if row and row["game_balance"] is not None else 0

    text = (
        f"👋 Добро пожаловать, <b>{user.first_name}</b>!\n\n"
        f"🎮 Нажмите <b>«🎮 Открыть игру»</b> чтобы запустить GOLDCLICK.\n"
        f"💰 Стартовый баланс: <b>{config.STARTING_BALANCE} монет</b>"
    )
    if referrer_id:
        text += f"\n\n🎁 Реферальный бонус: +{config.REFERRAL_BONUS} монет каждому!"

    await update.message.reply_text(text, reply_markup=make_main_kb(), parse_mode="HTML")


# ── Профиль ──────────────────────────────────────────────────

async def show_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await ban_check(update):
        return
    user = update.effective_user
    db.ensure_user(user.id, user.username or user.first_name)
    row = db.get_user(user.id)
    game_bal = row["game_balance"] if row["game_balance"] is not None else 0

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Вывести монеты в игру", callback_data="profile_deposit")],
    ])

    await update.message.reply_text(
        f"👤 <b>{user.first_name}</b>\n"
        f"🪪 ID: <code>{user.id}</code>\n\n"
        f"💰 Баланс (бот): <b>{row['balance']} монет</b>\n"
        f"🎮 Баланс (игра): <b>{game_bal} монет</b>\n"
        f"⭐ Уровень: <b>{row['level']}</b>  |  XP: {row['xp']}/{row['level'] * config.LEVEL_BASE_XP}\n"
        f"📊 [{level_bar(row['xp'], row['level'])}]\n\n"
        f"🎮 Игр: {row['games_played']}  ✅ Побед: {row['wins']}  ❌ Поражений: {row['losses']}\n\n"
        f"🔗 Реферальная ссылка:\n<code>https://t.me/{ctx.bot.username}?start={user.id}</code>",
        reply_markup=kb,
        parse_mode="HTML"
    )


async def profile_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    data = q.data

    if data == "profile_withdraw_game":
        row = db.get_user(user_id)
        game_bal = row["game_balance"] if row and row["game_balance"] is not None else 0
        if game_bal <= 0:
            await q.answer("❌ Недостаточно средств в игре.", show_alert=True)
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("100",  callback_data="wgame_100"),
             InlineKeyboardButton("500",  callback_data="wgame_500"),
             InlineKeyboardButton("1000", callback_data="wgame_1000")],
            [InlineKeyboardButton("5000", callback_data="wgame_5000"),
             InlineKeyboardButton("Всё",  callback_data=f"wgame_{game_bal}")],
            [InlineKeyboardButton("✏️ Своя сумма", callback_data="wgame_custom")],
            [InlineKeyboardButton("◀️ Назад", callback_data="deposit_back")],
        ])
        await q.edit_message_text(
            f"⬅️ <b>Вывод из игры в бот</b>\n\n"
            f"🎮 Баланс игры: <b>{game_bal} монет</b>\n"
            f"💰 Баланс бота: <b>{row['balance']} монет</b>\n\n"
            f"Выберите сумму для вывода:",
            reply_markup=kb, parse_mode="HTML"
        )

    elif data.startswith("wgame_") and data != "wgame_custom":
        amount_str = data.split("_")[1]
        if not amount_str.isdigit():
            return
        amount = int(amount_str)
        row = db.get_user(user_id)
        game_bal = row["game_balance"] if row and row["game_balance"] is not None else 0
        if amount > game_bal:
            await q.answer(f"❌ Недостаточно средств. В игре: {game_bal} монет", show_alert=True)
            return
        ok, result = db.withdraw_from_game(user_id, amount)
        if ok:
            await q.edit_message_text(
                f"✅ <b>Выведено {amount} монет из игры в бот!</b>\n\n"
                f"💰 Баланс бота: <b>{result} монет</b>",
                parse_mode="HTML"
            )
        else:
            await q.answer(f"❌ {result}", show_alert=True)

    elif data == "wgame_custom":
        user_sessions[user_id] = {"type": "wgame_custom"}
        await q.edit_message_text("✏️ Введите сумму для вывода из игры в бот:")

    elif data == "profile_deposit":
        row = db.get_user(user_id)
        if not row or row["balance"] <= 0:
            await q.answer("❌ У вас нет монет для перевода.", show_alert=True)
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("100",  callback_data="deposit_100"),
             InlineKeyboardButton("500",  callback_data="deposit_500"),
             InlineKeyboardButton("1000", callback_data="deposit_1000")],
            [InlineKeyboardButton("5000", callback_data="deposit_5000"),
             InlineKeyboardButton("Всё",  callback_data=f"deposit_{row['balance']}")],
            [InlineKeyboardButton("✏️ Своя сумма", callback_data="deposit_custom")],
            [InlineKeyboardButton("◀️ Назад", callback_data="deposit_back")],
        ])
        await q.edit_message_text(
            f"🎮 <b>Вывод монет в игру</b>\n\n"
            f"💰 Баланс бота: <b>{row['balance']} монет</b>\n"
            f"🎮 Баланс игры: <b>{row['game_balance'] or 0} монет</b>\n\n"
            f"Выберите сумму для перевода:",
            reply_markup=kb, parse_mode="HTML"
        )

    elif data.startswith("deposit_") and data not in ("deposit_custom", "deposit_back"):
        amount_str = data.split("_", 1)[1]  # FIX: use split with maxsplit=1
        amount_str = amount_str.strip()
        if not amount_str.isdigit():
            await q.answer("❌ Неверная сумма.", show_alert=True)
            return
        amount = int(amount_str)
        if amount <= 0:
            await q.answer("❌ Сумма должна быть больше 0.", show_alert=True)
            return
        ok, result = db.deposit_to_game(user_id, amount)
        if ok:
            await q.edit_message_text(
                f"✅ <b>Переведено {amount} монет в игру!</b>\n\n"
                f"🎮 Баланс игры: <b>{result} монет</b>",
                parse_mode="HTML"
            )
        else:
            await q.answer(f"❌ {result}", show_alert=True)

    elif data == "deposit_custom":
        user_sessions[user_id] = {"type": "deposit_custom"}
        await q.edit_message_text("✏️ Введите сумму для перевода в игру:")

    elif data == "deposit_back":
        row = db.get_user(user_id)
        game_bal = row["game_balance"] if row["game_balance"] is not None else 0
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 Вывести монеты в игру", callback_data="profile_deposit")],
        ])
        await q.edit_message_text(
            f"👤 <b>{q.from_user.first_name}</b>\n"
            f"🪪 ID: <code>{user_id}</code>\n\n"
            f"💰 Баланс (бот): <b>{row['balance']} монет</b>\n"
            f"🎮 Баланс (игра): <b>{game_bal} монет</b>",
            reply_markup=kb, parse_mode="HTML"
        )


# ── Ежедневный бонус ─────────────────────────────────────────

async def daily_bonus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await ban_check(update):
        return
    user = update.effective_user
    db.ensure_user(user.id, user.username or user.first_name)
    success, val = db.claim_daily(user.id)
    if success:
        await give_xp_notify(update, user.id, 30)
        await update.message.reply_text(
            f"🎁 Ежедневный бонус получен!\n💰 +<b>{val} монет</b>\n⭐ +30 XP",
            parse_mode="HTML"
        )
    else:
        hours, mins = val // 3600, (val % 3600) // 60
        await update.message.reply_text(
            f"⏳ Бонус уже получен.\nСледующий через: <b>{hours}ч {mins}мин</b>",
            parse_mode="HTML"
        )


# ── Меню игр ─────────────────────────────────────────────────

async def games_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await ban_check(update):
        return
    await update.message.reply_text("🎮 <b>Выберите игру:</b>", reply_markup=GAMES_KB, parse_mode="HTML")


async def back_to_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏠 Главное меню", reply_markup=make_main_kb())


# ── Слоты ────────────────────────────────────────────────────

async def slots_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await ban_check(update):
        return
    user = update.effective_user
    if check_spam(user.id):
        return await update.message.reply_text("⛔ Слишком часто!")
    db.ensure_user(user.id, user.username or user.first_name)
    row = db.get_user(user.id)
    await update.message.reply_text(
        f"🎰 <b>Слоты</b>\n💰 Баланс: <b>{row['balance']}</b>\n\nВыберите ставку:",
        reply_markup=bet_keyboard("slots_bet_"), parse_mode="HTML"
    )


async def _slots_spin(update, user_id, bet):
    row = db.get_user(user_id)
    if row["balance"] < bet:
        msg = f"❌ Недостаточно монет. У вас: {row['balance']}"
        if update.callback_query:
            return await update.callback_query.edit_message_text(msg)
        return await update.message.reply_text(msg)

    reels, mult, jackpot = games.spin_slots()

    if mult > 0:
        win = bet * mult
        delta = win - bet
        db.update_balance(user_id, delta)
        db.record_game(user_id, True)
        result = f"🏆 ДЖЕКПОТ! +{delta} монет!" if jackpot else f"🎉 ВЫИГРЫШ! +{delta} монет (×{mult})"
    else:
        db.update_balance(user_id, -bet)
        db.record_game(user_id, False)
        result = f"😢 Не повезло. -{bet} монет"

    new_balance = db.get_user(user_id)["balance"]
    await give_xp_notify(update, user_id, config.XP_PER_GAME + (config.XP_PER_WIN if mult > 0 else 0))
    text = f"🎰 <b>Слоты</b>  |  Ставка: {bet}\n\n{games.format_slots(reels)}\n\n{result}\n💰 Баланс: <b>{new_balance}</b>"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Ещё раз", callback_data=f"slots_bet_{bet}")]])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def slots_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    if q.data == "slots_bet_custom":
        user_sessions[user_id] = {"type": "slots_custom_bet"}
        await q.edit_message_text("🎰 Введите вашу ставку числом:", parse_mode="HTML")
        return
    bet = int(q.data.split("_")[2])
    await _slots_spin(update, user_id, bet)


# ── Блэкджек ─────────────────────────────────────────────────

async def blackjack_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await ban_check(update):
        return
    user = update.effective_user
    if check_spam(user.id):
        return await update.message.reply_text("⛔ Слишком часто!")
    db.ensure_user(user.id, user.username or user.first_name)
    row = db.get_user(user.id)
    await update.message.reply_text(
        f"🃏 <b>Блэкджек</b>\n💰 Баланс: <b>{row['balance']}</b>\n\nВыберите ставку:",
        reply_markup=bet_keyboard("bj_start_"), parse_mode="HTML"
    )


async def _bj_deal(update, user_id, bet):
    row = db.get_user(user_id)
    if row["balance"] < bet:
        msg = f"❌ Недостаточно монет. У вас: {row['balance']}"
        if update.callback_query:
            return await update.callback_query.edit_message_text(msg)
        return await update.message.reply_text(msg)

    deck, player, dealer = games.deal_initial()
    user_sessions[user_id] = {"type": "blackjack", "deck": deck, "player": player, "dealer": dealer, "bet": bet}
    db.update_balance(user_id, -bet)
    pv = games.hand_value(player)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👊 Ещё", callback_data="bj_hit"),
         InlineKeyboardButton("✋ Хватит", callback_data="bj_stand")],
        [InlineKeyboardButton("💥 Удвоить", callback_data="bj_double")]
    ])
    text = (
        f"🃏 <b>Блэкджек</b>  |  Ставка: {bet}\n\n"
        f"👤 Ваши карты: {games.format_hand(player)} = {pv}\n"
        f"🤖 Дилер: {games.format_hand(dealer, hide_second=True)}\n"
    )
    if pv == 21:
        text += "\n🎉 <b>Блэкджек!</b>"
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML")
        else:
            await update.message.reply_text(text, parse_mode="HTML")
        return await _bj_stand_logic(update, user_id)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def _bj_stand_logic(update, user_id):
    session = user_sessions.pop(user_id, None)
    if not session:
        return
    deck, player, dealer, bet = session["deck"], session["player"], session["dealer"], session["bet"]

    while games.hand_value(dealer) < 17:
        dealer.append(deck.pop())

    pv, dv = games.hand_value(player), games.hand_value(dealer)

    if pv > 21:
        result, delta = "💥 Перебор! Вы проиграли.", -bet
        db.record_game(user_id, False)
    elif dv > 21 or pv > dv:
        result, delta = f"🎉 Победа! +{bet} монет", bet
        db.update_balance(user_id, bet * 2)
        db.record_game(user_id, True)
    elif pv == dv:
        result, delta = "🤝 Ничья! Ставка возвращена.", 0
        db.update_balance(user_id, bet)
        db.record_game(user_id, False)
    else:
        result, delta = f"😢 Дилер выиграл. -{bet} монет", -bet
        db.record_game(user_id, False)

    new_balance = db.get_user(user_id)["balance"]
    await give_xp_notify(update, user_id, config.XP_PER_GAME + (config.XP_PER_WIN if delta > 0 else 0))
    text = (
        f"🃏 <b>Блэкджек</b>  |  Ставка: {bet}\n\n"
        f"👤 Вы: {games.format_hand(player)} = {pv}\n"
        f"🤖 Дилер: {games.format_hand(dealer)} = {dv}\n\n"
        f"{result}\n💰 Баланс: <b>{new_balance}</b>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML")


async def bj_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    action = q.data

    if action.startswith("bj_start_"):
        part = action.split("_")[2]
        if part == "custom":
            user_sessions[user_id] = {"type": "bj_custom_bet"}
            await q.edit_message_text("🃏 Введите вашу ставку числом:")
            return
        bet = int(part)
        return await _bj_deal(update, user_id, bet)

    session = user_sessions.get(user_id)
    if not session or session.get("type") != "blackjack":
        await q.edit_message_text("❌ Сессия не найдена. Начните заново.")
        return

    deck, player, dealer, bet = session["deck"], session["player"], session["dealer"], session["bet"]

    if action == "bj_hit":
        player.append(deck.pop())
        pv = games.hand_value(player)
        if pv >= 21:
            return await _bj_stand_logic(update, user_id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👊 Ещё", callback_data="bj_hit"),
             InlineKeyboardButton("✋ Хватит", callback_data="bj_stand")]
        ])
        await q.edit_message_text(
            f"🃏 <b>Блэкджек</b>  |  Ставка: {bet}\n\n"
            f"👤 Ваши карты: {games.format_hand(player)} = {pv}\n"
            f"🤖 Дилер: {games.format_hand(dealer, hide_second=True)}",
            reply_markup=kb, parse_mode="HTML"
        )
    elif action == "bj_stand":
        await _bj_stand_logic(update, user_id)
    elif action == "bj_double":
        row = db.get_user(user_id)
        if row["balance"] < bet:
            await q.answer("Недостаточно монет для удвоения!", show_alert=True)
            return
        db.update_balance(user_id, -bet)
        session["bet"] = bet * 2
        player.append(deck.pop())
        await _bj_stand_logic(update, user_id)


# ── Рулетка ──────────────────────────────────────────────────

async def roulette_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await ban_check(update):
        return
    user = update.effective_user
    if check_spam(user.id):
        return await update.message.reply_text("⛔ Слишком часто!")
    db.ensure_user(user.id, user.username or user.first_name)
    row = db.get_user(user.id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Красное ×2",  callback_data="rl_type_red"),
         InlineKeyboardButton("⚫ Чёрное ×2",   callback_data="rl_type_black")],
        [InlineKeyboardButton("🟢 Зеро ×36",    callback_data="rl_type_zero"),
         InlineKeyboardButton("🔢 Число ×36",   callback_data="rl_type_number")],
        [InlineKeyboardButton("📊 Чёт ×2",      callback_data="rl_type_even"),
         InlineKeyboardButton("📊 Нечет ×2",    callback_data="rl_type_odd")],
        [InlineKeyboardButton("1️⃣ 1-12 ×3",     callback_data="rl_type_dozen1"),
         InlineKeyboardButton("2️⃣ 13-24 ×3",    callback_data="rl_type_dozen2"),
         InlineKeyboardButton("3️⃣ 25-36 ×3",    callback_data="rl_type_dozen3")],
    ])
    await update.message.reply_text(
        f"🎯 <b>Рулетка</b>\n💰 Баланс: <b>{row['balance']}</b>\n\nВыберите тип ставки:",
        reply_markup=kb, parse_mode="HTML"
    )


async def roulette_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    data = q.data

    if data.startswith("rl_type_"):
        bet_type = data[8:]
        if bet_type == "number":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(str(i), callback_data=f"rl_num_{i}") for i in range(j, min(j + 6, 37))]
                for j in range(0, 37, 6)
            ])
            await q.edit_message_text("🔢 Выберите число (0–36):", reply_markup=kb)
            return
        await q.edit_message_text(
            f"🎯 Ставка на <b>{bet_type}</b>. Выберите сумму:",
            reply_markup=bet_keyboard(f"rl_bet_{bet_type}_"), parse_mode="HTML"
        )

    elif data.startswith("rl_bet_"):
        parts = data.split("_")
        bet_type, amount_str = parts[2], parts[3]
        if amount_str == "custom":
            user_sessions[user_id] = {"type": "rl_custom_bet", "bet_type": bet_type}
            await q.edit_message_text("🎯 Введите сумму ставки числом:")
            return
        await _roulette_spin(update, user_id, int(amount_str), bet_type)


async def roulette_number_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    num = int(q.data.split("_")[2])
    await q.edit_message_text(
        f"🔢 Число <b>{num}</b>. Выберите ставку:",
        reply_markup=bet_keyboard(f"rl_bet_number_{num}_"), parse_mode="HTML"
    )


async def _roulette_spin(update, user_id, amount, bet_type, bet_val=None):
    row = db.get_user(user_id)
    if row["balance"] < amount:
        msg = f"❌ Недостаточно монет. У вас: {row['balance']}"
        if update.callback_query:
            return await update.callback_query.edit_message_text(msg)
        return await update.message.reply_text(msg)

    db.update_balance(user_id, -amount)
    result_num, color = games.spin_roulette()
    won, multiplier = games.check_roulette_win(result_num, color, bet_type, bet_val)

    if won:
        winnings = amount * multiplier
        db.update_balance(user_id, winnings)
        delta = winnings - amount
        result_text = f"🎉 Выигрыш! +{delta} монет (×{multiplier})"
        db.record_game(user_id, True)
    else:
        result_text = f"😢 Не повезло. -{amount} монет"
        db.record_game(user_id, False)

    new_balance = db.get_user(user_id)["balance"]
    await give_xp_notify(update, user_id, config.XP_PER_GAME + (config.XP_PER_WIN if won else 0))

    color_emoji = "🔴" if color == "red" else ("🟢" if color == "green" else "⚫")
    text = (
        f"🎯 <b>Рулетка</b>\n\n"
        f"🎡 Выпало: <b>{color_emoji} {result_num}</b>\n"
        f"🎲 Ваша ставка: {bet_type}{' — ' + str(bet_val) if bet_val is not None else ''}\n\n"
        f"{result_text}\n💰 Баланс: <b>{new_balance}</b>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML")


# ── Вывод ────────────────────────────────────────────────────

async def cmd_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await ban_check(update):
        return
    user = update.effective_user
    db.ensure_user(user.id, user.username or user.first_name)
    row = db.get_user(user.id)
    game_bal = row["game_balance"] if row["game_balance"] is not None else 0
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            f"🎮 <b>Вывод из игры в бот</b>\n\nБаланс игры: <b>{game_bal} монет</b>\n\n"
            f"Использование: <code>/withdraw &lt;сумма&gt;</code>",
            parse_mode="HTML"
        )
        return
    amount = int(args[0])
    ok, result = db.withdraw_from_game(user.id, amount)
    if ok:
        await update.message.reply_text(
            f"✅ Выведено <b>{amount} монет</b> из игры в бот!\n💰 Баланс бота: <b>{result}</b>",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(f"❌ {result}")


async def cmd_transfers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await ban_check(update):
        return
    user = update.effective_user
    db.ensure_user(user.id, user.username or user.first_name)
    history = db.get_trade_history(user.id, 15)
    if not history:
        await update.message.reply_text("📋 История переводов пуста.")
        return
    lines = ["📋 <b>История переводов:</b>\n"]
    for t in history:
        dt = datetime.datetime.fromtimestamp(t["created_at"]).strftime("%d.%m %H:%M")
        direction = t["direction"]
        if direction == "deposit":
            arrow = "➡️ В игру"
        elif direction == "withdraw":
            arrow = "⬅️ В бот"
        else:
            if t["from_id"] == user.id:
                to_name = t.get("to_username") or str(t["to_id"])
                arrow = f"📤 → {to_name}"
            else:
                from_name = t.get("from_username") or str(t["from_id"])
                arrow = f"📥 ← {from_name}"
        lines.append(f"{arrow} <b>{t['amount']}</b> монет — {dt}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── WebApp data ───────────────────────────────────────────────

async def webapp_data_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        data = json.loads(update.effective_message.web_app_data.data)
        user_id = update.effective_user.id
        db.ensure_user(user_id, update.effective_user.username or update.effective_user.first_name)
        action = data.get("action")
        # Безопасный парсинг amount — строка или число
        try:
            amount = int(str(data.get("amount", 0)).strip())
        except (ValueError, TypeError):
            amount = 0

        if action == "withdraw" and amount > 0:
            row = db.get_user(user_id)
            game_bal = row["game_balance"] if row and row["game_balance"] is not None else 0
            if amount > game_bal:
                await update.message.reply_text(
                    f"❌ <b>Недостаточно средств в игре!</b>\n"
                    f"🎮 Баланс игры: <b>{game_bal} монет</b>",
                    parse_mode="HTML"
                )
                return
            ok, result = db.withdraw_from_game(user_id, amount)
            if ok:
                row2 = db.get_user(user_id)
                game_bal2 = row2["game_balance"] if row2 and row2["game_balance"] is not None else 0
                await update.message.reply_text(
                    f"✅ <b>Получено {amount} монет из игры!</b>\n"
                    f"💰 Баланс бота: <b>{result} монет</b>\n"
                    f"🎮 Баланс игры: <b>{game_bal2} монет</b>",
                    parse_mode="HTML", reply_markup=make_main_kb()
                )

        elif action == "deposit" and amount > 0:
            # Депозит из бота в игру через WebApp
            ok, result = db.deposit_to_game(user_id, amount)
            if ok:
                await update.message.reply_text(
                    f"✅ <b>Переведено {amount} монет в игру!</b>\n"
                    f"🎮 Баланс игры: <b>{result} монет</b>",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text(f"❌ {result}")

        elif action == "casino" and amount > 0:
            # Казино через игру — списать из game_balance
            row = db.get_user(user_id)
            game_bal = row["game_balance"] if row and row["game_balance"] is not None else 0
            if amount > game_bal:
                await update.message.reply_text(
                    f"❌ Недостаточно монет в игре для казино!\n"
                    f"🎮 Баланс: <b>{game_bal}</b>",
                    parse_mode="HTML"
                )
                return
            # Списать
            conn = db.get_conn()
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET game_balance=game_balance-%s WHERE user_id=%s AND game_balance>=%s",
                (amount, user_id, amount)
            )
            affected = cur.rowcount
            conn.commit(); cur.close(); conn.close()
            if affected:
                await update.message.reply_text(
                    f"🎰 <b>Казино!</b> Списано <b>{amount} монет</b> из игры.",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text("❌ Ошибка списания.")

        elif action == "trade" and amount > 0:
            # Трейд между игроками (из игры)
            to_id = data.get("to_id")
            fee = int(data.get("fee", 0))
            if not to_id:
                return
            # Получить имя отправителя
            sender_row = db.get_user(user_id)
            sender_name = sender_row["username"] if sender_row and sender_row.get("username") else str(user_id)
            ok, result = db.trade_coins(user_id, int(to_id), amount, fee)
            if ok:
                # Уведомить получателя с именем отправителя
                to_row = db.get_user(int(to_id))
                if to_row:
                    try:
                        await ctx.bot.send_message(
                            chat_id=int(to_id),
                            text=f"💰 <b>Пополнение игрового баланса!</b>\n\n"
                                 f"От: <b>@{sender_name}</b>\n"
                                 f"Сумма: <b>+{amount} монет</b>\n\n"
                                 f"🎮 Монеты зачислены в ваш игровой баланс.",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                await update.message.reply_text(
                    f"✅ Трейд выполнен! Отправлено <b>{amount} монет</b> игроку <b>@{to_row['username'] if to_row else to_id}</b>.",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text(f"❌ {result}")

    except Exception as e:
        logger.error(f"webapp_data_handler error: {e}")


# ── Топ игроков ───────────────────────────────────────────────

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await ban_check(update):
        return
    rows = db.get_top_users(30)
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 27
    lines = ["🏆 <b>ТОП-30 игроков</b> (по игровому балансу)\n"]
    for i, row in enumerate(rows):
        medal = medals[i]
        raw = row["username"] or str(row["user_id"])
        game_bal = row["game_balance"] if row.get("game_balance") is not None else 0
        lines.append(f"{medal} <b>{raw}</b> — {game_bal} 🎮  Ур.{row['level']}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── /clan_top — Топ кланов ────────────────────────────────────

async def cmd_clan_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await ban_check(update):
        return
    clans = db.get_top_clans(20)
    if not clans:
        await update.message.reply_text("🏰 Кланов пока нет.")
        return
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 17
    lines = ["🏰 <b>ТОП-20 кланов</b> (по суммарному балансу)\n"]
    for i, c in enumerate(clans):
        medal = medals[i]
        lines.append(
            f"{medal} {c['emoji']} <b>{c['name']}</b> — "
            f"{c['total_balance']} 🎮  👥 {c['member_count']}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── /trade — Перевод монет ────────────────────────────────────

async def cmd_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Перевод монет другому игроку из бота.
    Использование: /trade @username <сумма>
    """
    if await ban_check(update):
        return
    user = update.effective_user
    db.ensure_user(user.id, user.username or user.first_name)
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "💸 <b>Перевод монет</b>\n\n"
            "Использование: <code>/trade @username сумма</code>\n"
            "Пример: <code>/trade @vasya 1000</code>",
            parse_mode="HTML"
        )
        return

    raw_username = args[0].lstrip("@").strip()
    amount_str = args[1].strip()

    if not amount_str.isdigit() or int(amount_str) <= 0:
        await update.message.reply_text("❌ Укажите корректную сумму перевода.")
        return

    amount = int(amount_str)

    # Поиск получателя — регистронезависимый
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, username FROM users WHERE LOWER(username)=LOWER(%s)",
        (raw_username,)
    )
    row = cur.fetchone()
    cur.close(); conn.close()

    if not row:
        await update.message.reply_text(
            f"❌ Пользователь <b>@{raw_username}</b> не найден.",
            parse_mode="HTML"
        )
        return

    to_id, to_username = row[0], row[1]

    if to_id == user.id:
        await update.message.reply_text("❌ Нельзя переводить самому себе.")
        return

    sender_row = db.get_user(user.id)
    if not sender_row or sender_row["balance"] < amount:
        bal = sender_row["balance"] if sender_row else 0
        await update.message.reply_text(
            f"❌ Недостаточно монет.\nВаш баланс: <b>{bal} монет</b>",
            parse_mode="HTML"
        )
        return

    # Списать у отправителя
    db.update_balance(user.id, -amount)
    # Зачислить получателю в бот-баланс
    db.update_balance(to_id, amount)
    # Записать в историю
    db.add_trade_history(user.id, to_id, amount, "trade")

    sender_name = user.username or user.first_name

    # Уведомить получателя
    try:
        await ctx.bot.send_message(
            chat_id=to_id,
            text=f"💰 <b>Вам переведены монеты!</b>\n\n"
                 f"От: <b>@{sender_name}</b>\n"
                 f"Сумма: <b>+{amount} монет</b>\n\n"
                 f"💰 Монеты зачислены на ваш баланс бота.",
            parse_mode="HTML"
        )
    except Exception:
        pass

    new_bal = db.get_user(user.id)["balance"]
    await update.message.reply_text(
        f"✅ <b>Перевод выполнен!</b>\n\n"
        f"📤 Отправлено: <b>{amount} монет</b> → <b>@{to_username}</b>\n"
        f"💰 Ваш баланс: <b>{new_bal} монет</b>",
        parse_mode="HTML"
    )


# ── 💣 Мины ───────────────────────────────────────────────────

async def mines_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await ban_check(update):
        return
    user = update.effective_user
    if check_spam(user.id):
        return await update.message.reply_text("⛔ Слишком часто!")
    db.ensure_user(user.id, user.username or user.first_name)
    # Если уже есть активная сессия — восстановить
    session = db.mines_load_session(user.id)
    if session:
        row = db.get_user(user.id)
        kb = _mines_keyboard_from_session(session)
        opened = session["opened_cells"]
        mult = games.mines_multiplier(session["mines"], len(opened))
        potential = int(session["bet"] * mult)
        await update.message.reply_text(
            f"💣 <b>Мины</b>  |  {session['mines']} мин  |  Ставка: {session['bet']}\n\n"
            f"✅ Открыто: {len(opened)}  |  Множитель: ×{mult}\n"
            f"💰 Потенциальный выигрыш: <b>{potential} монет</b>\n\n"
            f"Продолжайте игру или заберите:",
            reply_markup=kb, parse_mode="HTML"
        )
        return
    row = db.get_user(user.id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("3 мины", callback_data="mines_cnt_3"),
         InlineKeyboardButton("5 мин", callback_data="mines_cnt_5")],
        [InlineKeyboardButton("10 мин", callback_data="mines_cnt_10"),
         InlineKeyboardButton("15 мин", callback_data="mines_cnt_15")],
    ])
    await update.message.reply_text(
        f"💣 <b>Мины</b>\n💰 Баланс: <b>{row['balance']}</b>\n\nВыберите количество мин:",
        reply_markup=kb, parse_mode="HTML"
    )


async def mines_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    data = q.data

    # Нажатие на уже открытую клетку или мину после взрыва — игнорировать
    if data.startswith("mines_noop"):
        return

    # Выбор количества мин → выбор ставки
    if data.startswith("mines_cnt_"):
        # Если есть незавершённая игра — предупредить
        existing = db.mines_load_session(user_id)
        if existing:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Продолжить старую", callback_data="mines_resume")],
                [InlineKeyboardButton("❌ Сдаться (потерять ставку)", callback_data=f"mines_forfeit_{data.split('_')[2]}")],
            ])
            await q.edit_message_text(
                "⚠️ У вас есть незавершённая игра в Мины!\n\nВыберите действие:",
                reply_markup=kb
            )
            user_sessions[user_id] = {"type": "mines_pending_cnt", "new_cnt_data": data}
            return
        mines = int(data.split("_")[2])
        user_sessions[user_id] = {"type": "mines_bet", "mines": mines}
        await q.edit_message_text(
            f"💣 <b>Мины: {mines}</b>\n\nВыберите ставку:",
            reply_markup=bet_keyboard(f"mines_bet_{mines}_"), parse_mode="HTML"
        )
        return

    # Продолжить старую игру
    if data == "mines_resume":
        session = db.mines_load_session(user_id)
        if not session:
            await q.edit_message_text("❌ Сессия не найдена.")
            return
        user_sessions[user_id] = {
            "type": "mines_game",
            "mines": session["mines"], "bet": session["bet"],
            "mine_positions": session["mine_positions"],
            "opened": session["opened_cells"],
        }
        kb = _mines_keyboard_from_session(session)
        opened = session["opened_cells"]
        mult = games.mines_multiplier(session["mines"], len(opened))
        await q.edit_message_text(
            f"💣 <b>Мины</b>  |  {session['mines']} мин  |  Ставка: {session['bet']}\n\n"
            f"✅ Открыто: {len(opened)}  |  Множитель: ×{mult}\n"
            f"💰 Потенциальный выигрыш: <b>{int(session['bet']*mult)} монет</b>\n\n"
            f"Продолжайте или заберите:",
            reply_markup=kb, parse_mode="HTML"
        )
        return

    # Сдаться и начать новую
    if data.startswith("mines_forfeit_"):
        db.mines_delete_session(user_id)
        user_sessions.pop(user_id, None)
        mines = int(data.split("_")[2])
        user_sessions[user_id] = {"type": "mines_bet", "mines": mines}
        await q.edit_message_text(
            f"💣 <b>Мины: {mines}</b>\n\nВыберите ставку:",
            reply_markup=bet_keyboard(f"mines_bet_{mines}_"), parse_mode="HTML"
        )
        return

    # Выбор ставки → начать игру
    if data.startswith("mines_bet_"):
        parts = data.split("_")
        # mines_bet_<mines>_<amount>
        if len(parts) < 4:
            return
        mines = int(parts[2])
        amount_str = parts[3]
        if amount_str == "custom":
            user_sessions[user_id] = {"type": "mines_custom_bet", "mines": mines}
            await q.edit_message_text("💣 Введите вашу ставку числом:")
            return
        if not amount_str.isdigit():
            return
        await _mines_start_game(update, user_id, mines, int(amount_str))
        return

    # Открыть клетку
    if data.startswith("mines_open_"):
        cell = int(data.split("_")[2])
        await _mines_open_cell(update, user_id, cell, q)
        return

    # Забрать выигрыш
    if data == "mines_cashout":
        await _mines_cashout(update, user_id, q)
        return


async def _mines_start_game(update, user_id, mines, bet):
    row = db.get_user(user_id)
    if row["balance"] < bet:
        msg = f"❌ Недостаточно монет. У вас: {row['balance']}"
        if update.callback_query:
            return await update.callback_query.edit_message_text(msg)
        return await update.message.reply_text(msg)

    # Списать ставку
    db.update_balance(user_id, -bet)

    mine_positions = games.mines_place(mines)
    opened_cells   = []

    # Сохранить сессию в БД (персистентно!)
    db.mines_save_session(user_id, bet, mines, mine_positions, opened_cells)
    # Также в памяти для быстрого доступа
    user_sessions[user_id] = {
        "type": "mines_game",
        "mines": mines,
        "bet": bet,
        "mine_positions": mine_positions,
        "opened": opened_cells,
    }

    mult = games.mines_multiplier(mines, 0)
    text = (
        f"💣 <b>Мины</b>  |  {mines} мин  |  Ставка: {bet}\n\n"
        f"Множитель: ×{mult}  →  Выигрыш: {int(bet * mult)}\n\n"
        f"Открывайте клетки или заберите выигрыш:"
    )
    session_fake = {"mine_positions": mine_positions, "opened_cells": opened_cells, "mines": mines, "bet": bet}
    kb = _mines_keyboard_from_session(session_fake)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


def _mines_keyboard_from_session(session):
    """Строит клавиатуру поля из сессии (dict с opened_cells)."""
    opened = session.get("opened_cells", session.get("opened", []))
    mines  = session.get("mines", 3)
    bet    = session.get("bet", 0)
    rows   = []
    for r in range(5):
        row_btns = []
        for c in range(5):
            cell = r * 5 + c
            if cell in opened:
                row_btns.append(InlineKeyboardButton("✅", callback_data=f"mines_noop_{cell}"))
            else:
                row_btns.append(InlineKeyboardButton("⬜", callback_data=f"mines_open_{cell}"))
        rows.append(row_btns)
    mult      = games.mines_multiplier(mines, len(opened))
    potential = int(bet * mult)
    rows.append([InlineKeyboardButton(
        f"💰 Забрать {potential} монет (×{mult})",
        callback_data="mines_cashout"
    )])
    return InlineKeyboardMarkup(rows)


def _mines_keyboard(user_id):
    """Оставлен для совместимости — берёт данные из памяти."""
    session = user_sessions.get(user_id, {})
    fake = {
        "opened_cells": session.get("opened", []),
        "mines": session.get("mines", 3),
        "bet":   session.get("bet", 0),
    }
    return _mines_keyboard_from_session(fake)


def _mines_reveal_keyboard(session):
    """Клавиатура с раскрытым полем после взрыва."""
    mine_positions = session.get("mine_positions", session.get("mine_positions", []))
    opened = session.get("opened_cells", session.get("opened", []))
    rows = []
    for r in range(5):
        row_btns = []
        for c in range(5):
            cell = r * 5 + c
            if cell in mine_positions:
                row_btns.append(InlineKeyboardButton("💣", callback_data="mines_noop"))
            elif cell in opened:
                row_btns.append(InlineKeyboardButton("✅", callback_data="mines_noop"))
            else:
                row_btns.append(InlineKeyboardButton("⬜", callback_data="mines_noop"))
        rows.append(row_btns)
    return InlineKeyboardMarkup(rows)


async def _mines_open_cell(update, user_id, cell, q):
    # Загружаем из памяти, если нет — из БД
    mem = user_sessions.get(user_id)
    if mem and mem.get("type") == "mines_game":
        mine_positions = mem["mine_positions"]
        opened         = mem["opened"]
        bet            = mem["bet"]
        mines          = mem["mines"]
    else:
        session = db.mines_load_session(user_id)
        if not session:
            await q.edit_message_text("❌ Сессия не найдена. Начните новую игру /mines")
            return
        mine_positions = session["mine_positions"]
        opened         = session["opened_cells"]
        bet            = session["bet"]
        mines          = session["mines"]
        # Восстановить в памяти
        user_sessions[user_id] = {
            "type": "mines_game",
            "mines": mines, "bet": bet,
            "mine_positions": mine_positions,
            "opened": opened,
        }
        mem = user_sessions[user_id]

    if cell in opened:
        await q.answer("Уже открыта!", show_alert=False)
        return

    if games.mines_is_mine(mine_positions, cell):
        # Взрыв — удаляем сессию из памяти и БД
        user_sessions.pop(user_id, None)
        db.mines_delete_session(user_id)
        db.record_game(user_id, False)
        reveal_session = {"mine_positions": mine_positions, "opened_cells": opened}
        kb = _mines_reveal_keyboard(reveal_session)
        await q.edit_message_text(
            f"💥 <b>ВЗРЫВ!</b>  |  Мины: {mines}  |  Ставка: {bet}\n\n"
            f"😢 Вы потеряли <b>{bet} монет</b>\n\n"
            f"💣 Все мины раскрыты:",
            reply_markup=kb, parse_mode="HTML"
        )
        return

    # Безопасная клетка
    opened.append(cell)
    mem["opened"] = opened
    # Сохранить прогресс в БД
    db.mines_save_session(user_id, bet, mines, mine_positions, opened)

    mult      = games.mines_multiplier(mines, len(opened))
    potential = int(bet * mult)

    # Если открыты все безопасные — автовыплата
    safe_total = 25 - mines
    if len(opened) >= safe_total:
        await _mines_cashout(update, user_id, q, forced=True)
        return

    fake_session = {"opened_cells": opened, "mines": mines, "bet": bet}
    kb = _mines_keyboard_from_session(fake_session)
    await q.edit_message_text(
        f"💣 <b>Мины</b>  |  {mines} мин  |  Ставка: {bet}\n\n"
        f"✅ Открыто: {len(opened)}  |  Множитель: ×{mult}\n"
        f"💰 Потенциальный выигрыш: <b>{potential} монет</b>\n\n"
        f"Продолжайте или заберите:",
        reply_markup=kb, parse_mode="HTML"
    )


async def _mines_cashout(update, user_id, q, forced=False):
    # Загружаем сессию
    mem = user_sessions.pop(user_id, None)
    if mem and mem.get("type") == "mines_game":
        bet   = mem["bet"]
        mines = mem["mines"]
        opened = mem["opened"]
    else:
        session = db.mines_load_session(user_id)
        if not session:
            await q.edit_message_text("❌ Сессия не найдена.")
            return
        bet    = session["bet"]
        mines  = session["mines"]
        opened = session["opened_cells"]

    # Удалить из БД
    db.mines_delete_session(user_id)

    if not opened:
        # Ничего не открыто — вернуть ставку
        db.update_balance(user_id, bet)
        await q.edit_message_text(
            "💣 <b>Мины</b>\n\nВы ничего не открыли. Ставка возвращена.",
            parse_mode="HTML"
        )
        return

    mult     = games.mines_multiplier(mines, len(opened))
    winnings = int(bet * mult)
    db.update_balance(user_id, winnings)
    db.record_game(user_id, True)

    new_balance = db.get_user(user_id)["balance"]
    profit = winnings - bet

    title = "🏆 Все клетки открыты! МАКСИМАЛЬНЫЙ ВЫИГРЫШ!" if forced else "💰 Выигрыш забран!"

    await q.edit_message_text(
        f"💣 <b>Мины</b>  |  {mines} мин  |  Ставка: {bet}\n\n"
        f"{title}\n"
        f"Открыто клеток: {len(opened)}  |  ×{mult}\n"
        f"💰 Получено: <b>{winnings} монет</b> (+{profit})\n\n"
        f"💰 Баланс: <b>{new_balance}</b>",
        parse_mode="HTML"
    )



# ── /ocp — Панель администратора ─────────────────────────────

def _ocp_main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Найти пользователя", callback_data="ocp_find")],
        [InlineKeyboardButton("💰 Изменить баланс",    callback_data="ocp_setbal")],
        [InlineKeyboardButton("🎮 Изменить game_balance", callback_data="ocp_setgame")],
        [InlineKeyboardButton("🚫 Забанить пользователя", callback_data="ocp_ban")],
        [InlineKeyboardButton("✅ Разбанить пользователя", callback_data="ocp_unban")],
        [InlineKeyboardButton("📢 Рассылка",           callback_data="ocp_broadcast")],
        [InlineKeyboardButton("📊 Топ игроков",        callback_data="ocp_top")],
        [InlineKeyboardButton("📋 Список пользователей", callback_data="ocp_list")],
    ])


async def cmd_ocp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return
    await update.message.reply_text(
        "🔐 <b>Панель администратора</b>\n\nВыберите действие:",
        reply_markup=_ocp_main_kb(), parse_mode="HTML"
    )


async def ocp_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Нет доступа.", show_alert=True)
        return
    await q.answer()
    data = q.data
    uid = q.from_user.id

    if data == "ocp_find":
        user_sessions[uid] = {"type": "ocp_find"}
        await q.edit_message_text("🔍 Введите ID или @username пользователя:")

    elif data == "ocp_setbal":
        user_sessions[uid] = {"type": "ocp_setbal_id"}
        await q.edit_message_text("💰 Введите ID пользователя:")

    elif data == "ocp_setgame":
        user_sessions[uid] = {"type": "ocp_setgame_id"}
        await q.edit_message_text("🎮 Введите ID пользователя для изменения game_balance:")

    elif data == "ocp_ban":
        user_sessions[uid] = {"type": "ocp_ban_id"}
        await q.edit_message_text("🚫 Введите ID пользователя для бана:")

    elif data == "ocp_unban":
        user_sessions[uid] = {"type": "ocp_unban_id"}
        await q.edit_message_text("✅ Введите ID пользователя для разбана:")

    elif data == "ocp_broadcast":
        user_sessions[uid] = {"type": "ocp_broadcast"}
        await q.edit_message_text(
            "📢 <b>Рассылка</b>\n\nВведите текст сообщения (поддерживается HTML):\n\n"
            "Пример: <code>&lt;b&gt;Новости!&lt;/b&gt;\nТекст...</code>",
            parse_mode="HTML"
        )

    elif data == "ocp_top":
        rows = db.get_top_users(30)
        lines = ["📊 <b>Топ-30:</b>\n"]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. <code>{row['user_id']}</code> | {row['username']} | {row['balance']}💰 | Ур.{row['level']}")
        text = "\n".join(lines)
        # Telegram limit 4096
        if len(text) > 4000:
            text = text[:4000] + "\n..."
        await q.edit_message_text(text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ocp_back")]]))

    elif data == "ocp_list":
        conn = db.get_conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id, username, balance, level, is_banned, created_at FROM users ORDER BY created_at DESC LIMIT 20")
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        cur.close(); conn.close()
        lines = ["📋 <b>Последние 20 регистраций:</b>\n"]
        for row in rows:
            dt = datetime.datetime.fromtimestamp(row["created_at"]).strftime("%d.%m.%y") if row["created_at"] else "?"
            ban_mark = " 🚫" if row["is_banned"] else ""
            lines.append(f"<code>{row['user_id']}</code> | {row['username']}{ban_mark} | {row['balance']}💰 | {dt}")
        await q.edit_message_text("\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ocp_back")]]))

    elif data == "ocp_back":
        await q.edit_message_text("🔐 <b>Панель администратора</b>\n\nВыберите действие:",
            reply_markup=_ocp_main_kb(), parse_mode="HTML")

    elif data.startswith("ocp_info_"):
        target_id = int(data.split("_")[2])
        row = db.get_user(target_id)
        if not row:
            await q.answer("Пользователь не найден.", show_alert=True)
            return
        reg = datetime.datetime.fromtimestamp(row["created_at"]).strftime("%d.%m.%Y %H:%M") if row["created_at"] else "?"
        game_bal = row["game_balance"] if row["game_balance"] is not None else 0
        ban_status = "🚫 Забанен" if row.get("is_banned") else "✅ Активен"
        text = (
            f"👤 <b>Информация</b>\n\n"
            f"🪪 ID: <code>{row['user_id']}</code>\n"
            f"👤 Username: @{row['username']}\n"
            f"💰 Баланс (бот): <b>{row['balance']}</b>\n"
            f"🎮 Баланс (игра): <b>{game_bal}</b>\n"
            f"⭐ Уровень: {row['level']}  XP: {row['xp']}\n"
            f"🎮 Игр: {row['games_played']}  ✅ {row['wins']}  ❌ {row['losses']}\n"
            f"📅 Регистрация: {reg}\n"
            f"Статус: {ban_status}"
        )
        ban_btn = InlineKeyboardButton(
            "✅ Разбанить" if row.get("is_banned") else "🚫 Забанить",
            callback_data=f"ocp_toggleban_{target_id}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Изменить баланс", callback_data=f"ocp_editbal_{target_id}")],
            [InlineKeyboardButton("🎮 Изменить game_balance", callback_data=f"ocp_editgame_{target_id}")],
            [ban_btn],
            [InlineKeyboardButton("◀️ Назад", callback_data="ocp_back")],
        ])
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    elif data.startswith("ocp_toggleban_"):
        target_id = int(data.split("_")[2])
        row = db.get_user(target_id)
        if not row:
            await q.answer("Пользователь не найден.", show_alert=True)
            return
        new_ban = not bool(row.get("is_banned"))
        db.set_ban(target_id, new_ban)
        action_text = "🚫 Забанен" if new_ban else "✅ Разбанен"
        await q.answer(f"{action_text}: {row['username']}", show_alert=True)
        # Обновить карточку
        await ocp_callback(update, ctx)

    elif data.startswith("ocp_editbal_"):
        target_id = int(data.split("_")[2])
        user_sessions[uid] = {"type": "ocp_editbal_amount", "target_id": target_id}
        await q.edit_message_text(
            f"💰 Введите новый баланс для <code>{target_id}</code>\n(или +500 / -200 для изменения):",
            parse_mode="HTML"
        )

    elif data.startswith("ocp_editgame_"):
        target_id = int(data.split("_")[2])
        user_sessions[uid] = {"type": "ocp_editgame_amount", "target_id": target_id}
        await q.edit_message_text(
            f"🎮 Введите новый game_balance для <code>{target_id}</code>\n(или +500 / -200 для изменения):",
            parse_mode="HTML"
        )


# ── Обработка сессий OCP и deposit ───────────────────────────

async def _handle_ocp_sessions(update: Update, session: dict, txt: str, uid: int) -> bool:
    stype = session.get("type")

    # Вывод из игры: своя сумма
    if stype == "wgame_custom":
        del user_sessions[uid]
        if not txt.isdigit() or int(txt) <= 0:
            await update.message.reply_text("❌ Введите положительное число.")
            return True
        amount = int(txt)
        row = db.get_user(uid)
        game_bal = row["game_balance"] if row and row["game_balance"] is not None else 0
        if amount > game_bal:
            await update.message.reply_text(f"❌ Недостаточно средств. В игре: <b>{game_bal} монет</b>", parse_mode="HTML")
            return True
        ok, result = db.withdraw_from_game(uid, amount)
        if ok:
            await update.message.reply_text(f"✅ Выведено <b>{amount} монет</b> из игры в бот!\n💰 Баланс бота: <b>{result}</b>", parse_mode="HTML")
        else:
            await update.message.reply_text(f"❌ {result}")
        return True

    # Вывод в игру: своя сумма
    if stype == "deposit_custom":
        del user_sessions[uid]
        if not txt.isdigit() or int(txt) <= 0:
            await update.message.reply_text("❌ Введите положительное число.")
            return True
        amount = int(txt)
        ok, result = db.deposit_to_game(uid, amount)
        if ok:
            await update.message.reply_text(f"✅ Переведено <b>{amount} монет</b> в игру!\n🎮 Баланс игры: <b>{result}</b>", parse_mode="HTML")
        else:
            await update.message.reply_text(f"❌ {result}")
        return True

    # OCP: найти пользователя
    if stype == "ocp_find":
        del user_sessions[uid]
        target_id = None
        if txt.isdigit():
            target_id = int(txt)
        elif txt.startswith("@"):
            conn = db.get_conn()
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM users WHERE LOWER(username)=LOWER(%s)", (txt[1:],))
            row = cur.fetchone()
            cur.close(); conn.close()
            if row:
                target_id = row[0]
        if not target_id:
            await update.message.reply_text("❌ Пользователь не найден.")
            return True
        row = db.get_user(target_id)
        if not row:
            await update.message.reply_text("❌ Пользователь не найден в базе.")
            return True
        reg = datetime.datetime.fromtimestamp(row["created_at"]).strftime("%d.%m.%Y %H:%M") if row["created_at"] else "?"
        game_bal = row["game_balance"] if row["game_balance"] is not None else 0
        ban_status = "🚫 Забанен" if row.get("is_banned") else "✅ Активен"
        text = (
            f"👤 <b>Информация</b>\n\n"
            f"🪪 ID: <code>{row['user_id']}</code>\n"
            f"👤 Username: @{row['username']}\n"
            f"💰 Баланс (бот): <b>{row['balance']}</b>\n"
            f"🎮 Баланс (игра): <b>{game_bal}</b>\n"
            f"⭐ Уровень: {row['level']}  XP: {row['xp']}\n"
            f"🎮 Игр: {row['games_played']}  ✅ {row['wins']}  ❌ {row['losses']}\n"
            f"📅 Регистрация: {reg}\n"
            f"Статус: {ban_status}"
        )
        ban_btn = InlineKeyboardButton(
            "✅ Разбанить" if row.get("is_banned") else "🚫 Забанить",
            callback_data=f"ocp_toggleban_{target_id}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Изменить баланс", callback_data=f"ocp_editbal_{target_id}")],
            [InlineKeyboardButton("🎮 Изменить game_balance", callback_data=f"ocp_editgame_{target_id}")],
            [ban_btn],
            [InlineKeyboardButton("◀️ В меню", callback_data="ocp_back")],
        ])
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
        return True

    # OCP: бан
    if stype == "ocp_ban_id":
        del user_sessions[uid]
        if not txt.isdigit():
            await update.message.reply_text("❌ Введите числовой ID.")
            return True
        target_id = int(txt)
        row = db.get_user(target_id)
        if not row:
            await update.message.reply_text("❌ Пользователь не найден.")
            return True
        db.set_ban(target_id, True)
        await update.message.reply_text(f"🚫 Пользователь <code>{target_id}</code> ({row['username']}) забанен.", parse_mode="HTML")
        return True

    # OCP: разбан
    if stype == "ocp_unban_id":
        del user_sessions[uid]
        if not txt.isdigit():
            await update.message.reply_text("❌ Введите числовой ID.")
            return True
        target_id = int(txt)
        row = db.get_user(target_id)
        if not row:
            await update.message.reply_text("❌ Пользователь не найден.")
            return True
        db.set_ban(target_id, False)
        await update.message.reply_text(f"✅ Пользователь <code>{target_id}</code> ({row['username']}) разбанен.", parse_mode="HTML")
        return True

    # OCP: рассылка
    if stype == "ocp_broadcast":
        del user_sessions[uid]
        msg_text = txt
        all_ids = db.get_all_user_ids()
        await update.message.reply_text(
            f"📢 Начинаю рассылку для <b>{len(all_ids)}</b> пользователей...",
            parse_mode="HTML"
        )
        sent, failed = 0, 0
        for target_id in all_ids:
            try:
                await update.get_bot().send_message(
                    chat_id=target_id,
                    text=msg_text,
                    parse_mode="HTML"
                )
                sent += 1
                await asyncio.sleep(0.05)  # антифлуд
            except Exception:
                failed += 1
        await update.message.reply_text(
            f"✅ Рассылка завершена!\n📤 Отправлено: <b>{sent}</b>\n❌ Ошибок: <b>{failed}</b>",
            parse_mode="HTML"
        )
        return True

    # OCP: изменить баланс (шаг 1 — ID)
    if stype == "ocp_setbal_id":
        if not txt.isdigit():
            await update.message.reply_text("❌ Введите числовой ID.")
            return True
        user_sessions[uid] = {"type": "ocp_editbal_amount", "target_id": int(txt)}
        await update.message.reply_text(
            f"💰 Введите новый баланс для <code>{txt}</code>\n(или +500 / -200 для изменения):",
            parse_mode="HTML"
        )
        return True

    # OCP: изменить game_balance (шаг 1 — ID)
    if stype == "ocp_setgame_id":
        if not txt.isdigit():
            await update.message.reply_text("❌ Введите числовой ID.")
            return True
        user_sessions[uid] = {"type": "ocp_editgame_amount", "target_id": int(txt)}
        await update.message.reply_text(
            f"🎮 Введите новый game_balance для <code>{txt}</code>\n(или +500 / -200 для изменения):",
            parse_mode="HTML"
        )
        return True

    # OCP: применить изменение баланса
    if stype == "ocp_editbal_amount":
        del user_sessions[uid]
        target_id = session["target_id"]
        conn = db.get_conn()
        cur = conn.cursor()
        cur.execute("SELECT balance FROM users WHERE user_id=%s", (target_id,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            await update.message.reply_text("❌ Пользователь не найден.")
            return True
        bal = row[0]
        if txt.startswith("+") and txt[1:].isdigit():
            new_bal = bal + int(txt[1:])
        elif txt.startswith("-") and txt[1:].isdigit():
            new_bal = max(0, bal - int(txt[1:]))
        elif txt.isdigit():
            new_bal = int(txt)
        else:
            cur.close(); conn.close()
            await update.message.reply_text("❌ Неверный формат.")
            return True
        cur.execute("UPDATE users SET balance=%s WHERE user_id=%s", (new_bal, target_id))
        conn.commit(); cur.close(); conn.close()
        await update.message.reply_text(
            f"✅ Баланс <code>{target_id}</code> → <b>{new_bal} монет</b>.", parse_mode="HTML"
        )
        return True

    # OCP: применить изменение game_balance
    if stype == "ocp_editgame_amount":
        del user_sessions[uid]
        target_id = session["target_id"]
        conn = db.get_conn()
        cur = conn.cursor()
        cur.execute("SELECT game_balance FROM users WHERE user_id=%s", (target_id,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            await update.message.reply_text("❌ Пользователь не найден.")
            return True
        cur_val = row[0] if row[0] is not None else 0
        if txt.startswith("+") and txt[1:].isdigit():
            new_val = cur_val + int(txt[1:])
        elif txt.startswith("-") and txt[1:].isdigit():
            new_val = max(0, cur_val - int(txt[1:]))
        elif txt.isdigit():
            new_val = int(txt)
        else:
            cur.close(); conn.close()
            await update.message.reply_text("❌ Неверный формат.")
            return True
        cur.execute("UPDATE users SET game_balance=%s WHERE user_id=%s", (new_val, target_id))
        conn.commit(); cur.close(); conn.close()
        await update.message.reply_text(
            f"✅ game_balance <code>{target_id}</code> → <b>{new_val} монет</b>.", parse_mode="HTML"
        )
        return True

    return False


# ── Роутер текстовых сообщений ───────────────────────────────

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await ban_check(update):
        return
    txt = update.message.text
    user = update.effective_user

    session = user_sessions.get(user.id)
    if session:
        if await _handle_ocp_sessions(update, session, txt, user.id):
            return
        stype = session.get("type")

        if stype == "slots_custom_bet":
            del user_sessions[user.id]
            if not txt.isdigit() or int(txt) <= 0:
                return await update.message.reply_text("❌ Введите положительное число.")
            bet = int(txt)
            row = db.get_user(user.id)
            if row["balance"] < bet:
                return await update.message.reply_text(f"❌ Недостаточно монет. У вас: {row['balance']}")
            return await _slots_spin(update, user.id, bet)

        elif stype == "bj_custom_bet":
            del user_sessions[user.id]
            if not txt.isdigit() or int(txt) <= 0:
                return await update.message.reply_text("❌ Введите положительное число.")
            bet = int(txt)
            row = db.get_user(user.id)
            if row["balance"] < bet:
                return await update.message.reply_text(f"❌ Недостаточно монет. У вас: {row['balance']}")
            return await _bj_deal(update, user.id, bet)

        elif stype == "rl_custom_bet":
            del user_sessions[user.id]
            if not txt.isdigit() or int(txt) <= 0:
                return await update.message.reply_text("❌ Введите положительное число.")
            amount = int(txt)
            row = db.get_user(user.id)
            if row["balance"] < amount:
                return await update.message.reply_text(f"❌ Недостаточно монет. У вас: {row['balance']}")
            await _roulette_spin(update, user.id, amount, session["bet_type"], session.get("bet_val"))
            return

        elif stype == "mines_custom_bet":
            del user_sessions[user.id]
            if not txt.isdigit() or int(txt) <= 0:
                return await update.message.reply_text("❌ Введите положительное число.")
            bet = int(txt)
            mines = session.get("mines", 3)
            row = db.get_user(user.id)
            if row["balance"] < bet:
                return await update.message.reply_text(f"❌ Недостаточно монет. У вас: {row['balance']}")
            await _mines_start_game(update, user.id, mines, bet)
            return

    mapping = {
        "🎰 Игры":             games_menu,
        "📊 Профиль":          show_profile,
        "🎁 Ежедневный бонус": daily_bonus,
        "◀️ Назад":            back_to_main,
        "🎰 Слоты":            slots_start,
        "🃏 Блэкджек":         blackjack_start,
        "🎯 Рулетка":          roulette_start,
        "💣 Мины":             mines_start,
    }
    handler = mapping.get(txt)
    if handler:
        await handler(update, ctx)


# ── Сборка приложения ─────────────────────────────────────────

def main():
    db.init_db()
    app = Application.builder().token(config.BOT_TOKEN).build()

    for cmd, handler in [
        ("start",      cmd_start),
        ("profile",    show_profile),
        ("daily",      daily_bonus),
        ("slots",      slots_start),
        ("blackjack",  blackjack_start),
        ("bj",         blackjack_start),
        ("roulette",   roulette_start),
        ("mines",      mines_start),
        ("trade",      cmd_trade),
        ("withdraw",   cmd_withdraw),
        ("transfers",  cmd_transfers),
        ("top",        cmd_top),
        ("clantop",    cmd_clan_top),
        ("ocp",        cmd_ocp),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, webapp_data_handler))
    app.add_handler(CallbackQueryHandler(slots_callback,           pattern=r"^slots_"))
    app.add_handler(CallbackQueryHandler(bj_callback,              pattern=r"^bj_"))
    app.add_handler(CallbackQueryHandler(roulette_number_callback, pattern=r"^rl_num_"))
    app.add_handler(CallbackQueryHandler(roulette_callback,        pattern=r"^rl_"))
    app.add_handler(CallbackQueryHandler(mines_callback,           pattern=r"^mines_"))
    app.add_handler(CallbackQueryHandler(profile_callback,         pattern=r"^(profile_|deposit_|wgame_)"))
    app.add_handler(CallbackQueryHandler(ocp_callback,             pattern=r"^ocp_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logger.info("GOLDCLICK Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
