# main.py — GOLDCLICK Bot (python-telegram-bot 20.x)
# Убраны: экономика, магазин, квесты, достижения, топ, сообщество, клан, трейд
# Оставлены: /start, профиль, игры (слоты/блэкджек/рулетка), ежедневный бонус, кнопка открыть игру

import logging
from dotenv import load_dotenv
load_dotenv()
import time
import random

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

# ── URL мини-приложения ───────────────────────────────────────
# Замените на реальный HTTPS-адрес где хостится clicker-3.html
WEBAPP_URL = "https://incolex.github.io/bottgcasinoclicker/clicker.html"

# ── Клавиатуры ───────────────────────────────────────────────

def make_main_kb(game_balance: int = 0, user_id: int = 0):
    """Главная клавиатура с кнопкой WebApp. Передаёт tg_id и game_balance через URL."""
    url = f"{WEBAPP_URL}?tgid={user_id}&gb={game_balance}"
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🎮 Открыть игру", web_app=WebAppInfo(url=url))],
            ["🎰 Игры", "📊 Профиль"],
            ["🎁 Ежедневный бонус"],
        ],
        resize_keyboard=True
    )

GAMES_KB = ReplyKeyboardMarkup(
    [
        ["🎰 Слоты", "🃏 Блэкджек"],
        ["🎯 Рулетка"],
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


# ── /start ───────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        text += f"\n\n🎁 Реферальный бонус: +{config.REFERRAL_BONUS} монет!"

    await update.message.reply_text(text, reply_markup=make_main_kb(game_bal, user.id), parse_mode="HTML")


# ── Профиль ──────────────────────────────────────────────────

async def show_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.ensure_user(user.id, user.username or user.first_name)
    row = db.get_user(user.id)

    game_bal = row["game_balance"] if row["game_balance"] is not None else 0

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Вывести монеты в игру", callback_data="profile_deposit")],
        [InlineKeyboardButton("⬅️ Вывести из игры в бот", callback_data="profile_withdraw_game")],
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

    elif data.startswith("deposit_") and data != "deposit_custom" and data != "deposit_back":
        amount_str = data.split("_")[1]
        if not amount_str.isdigit():
            return
        amount = int(amount_str)
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
            [InlineKeyboardButton("⬅️ Вывести из игры в бот", callback_data="profile_withdraw_game")],
        ])
        await q.edit_message_text(
            f"👤 <b>{q.from_user.first_name}</b>\n"
            f"🪪 ID: <code>{user_id}</code>\n\n"
            f"💰 Баланс (бот): <b>{row['balance']} монет</b>\n"
            f"🎮 Баланс (игра): <b>{game_bal} монет</b>\n"
            f"⭐ Уровень: <b>{row['level']}</b>\n\n"
            f"🎮 Игр: {row['games_played']}  ✅ Побед: {row['wins']}  ❌ Поражений: {row['losses']}",
            reply_markup=kb, parse_mode="HTML"
        )


# ── Ежедневный бонус ─────────────────────────────────────────

async def daily_bonus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_text("🎮 <b>Выберите игру:</b>", reply_markup=GAMES_KB, parse_mode="HTML")

async def back_to_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏠 Главное меню", reply_markup=make_main_kb())


# ── Слоты ────────────────────────────────────────────────────

async def slots_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if check_spam(user.id):
        return await update.message.reply_text("⛔ Слишком часто!")
    db.ensure_user(user.id, user.username or user.first_name)
    row = db.get_user(user.id)
    args = ctx.args or []
    if args and args[0].isdigit():
        bet = int(args[0])
        if bet <= 0:
            return await update.message.reply_text("❌ Ставка должна быть больше 0.")
        if row["balance"] < bet:
            return await update.message.reply_text(f"❌ Недостаточно монет. У вас: {row['balance']}")
        return await _slots_spin(update, user.id, bet)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("50",   callback_data="slots_bet_50"),
         InlineKeyboardButton("100",  callback_data="slots_bet_100"),
         InlineKeyboardButton("200",  callback_data="slots_bet_200")],
        [InlineKeyboardButton("500",  callback_data="slots_bet_500"),
         InlineKeyboardButton("1000", callback_data="slots_bet_1000"),
         InlineKeyboardButton("2000", callback_data="slots_bet_2000")],
        [InlineKeyboardButton("✏️ Своя ставка", callback_data="slots_bet_custom")],
    ])
    await update.message.reply_text(
        f"🎰 <b>Слоты</b>\n💰 Баланс: <b>{row['balance']}</b>\n\nВыберите ставку:",
        reply_markup=kb, parse_mode="HTML"
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
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML")

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
    user = update.effective_user
    if check_spam(user.id):
        return await update.message.reply_text("⛔ Слишком часто!")
    db.ensure_user(user.id, user.username or user.first_name)
    row = db.get_user(user.id)
    args = ctx.args or []
    if args and args[0].isdigit():
        bet = int(args[0])
        if bet <= 0:
            return await update.message.reply_text("❌ Ставка должна быть больше 0.")
        if row["balance"] < bet:
            return await update.message.reply_text(f"❌ Недостаточно монет. У вас: {row['balance']}")
        return await _bj_deal(update, user.id, bet)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("50",   callback_data="bj_start_50"),
         InlineKeyboardButton("100",  callback_data="bj_start_100"),
         InlineKeyboardButton("200",  callback_data="bj_start_200")],
        [InlineKeyboardButton("500",  callback_data="bj_start_500"),
         InlineKeyboardButton("1000", callback_data="bj_start_1000"),
         InlineKeyboardButton("2000", callback_data="bj_start_2000")],
        [InlineKeyboardButton("✏️ Своя ставка", callback_data="bj_start_custom")],
    ])
    await update.message.reply_text(
        f"🃏 <b>Блэкджек</b>\n💰 Баланс: <b>{row['balance']}</b>\n\nВыберите ставку:",
        reply_markup=kb, parse_mode="HTML"
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
                [InlineKeyboardButton(str(i), callback_data=f"rl_num_{i}") for i in range(j, min(j+6, 37))]
                for j in range(0, 37, 6)
            ])
            await q.edit_message_text("🔢 Выберите число (0–36):", reply_markup=kb)
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("50",   callback_data=f"rl_bet_{bet_type}_50"),
             InlineKeyboardButton("100",  callback_data=f"rl_bet_{bet_type}_100"),
             InlineKeyboardButton("200",  callback_data=f"rl_bet_{bet_type}_200")],
            [InlineKeyboardButton("500",  callback_data=f"rl_bet_{bet_type}_500"),
             InlineKeyboardButton("1000", callback_data=f"rl_bet_{bet_type}_1000")],
            [InlineKeyboardButton("✏️ Своя", callback_data=f"rl_bet_{bet_type}_custom")],
        ])
        await q.edit_message_text(f"🎯 Ставка на <b>{bet_type}</b>. Выберите сумму:", reply_markup=kb, parse_mode="HTML")

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
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("50",   callback_data=f"rl_bet_number_50_{num}"),
         InlineKeyboardButton("100",  callback_data=f"rl_bet_number_100_{num}"),
         InlineKeyboardButton("500",  callback_data=f"rl_bet_number_500_{num}")],
        [InlineKeyboardButton("✏️ Своя", callback_data=f"rl_bet_number_custom_{num}")],
    ])
    await q.edit_message_text(f"🔢 Число <b>{num}</b>. Выберите ставку:", reply_markup=kb, parse_mode="HTML")

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
        delta = -amount
        result_text = f"😢 Не повезло. -{amount} монет"
        db.record_game(user_id, False)

    new_balance = db.get_user(user_id)["balance"]
    await give_xp_notify(update, user_id, config.XP_PER_GAME + (config.XP_PER_WIN if won else 0))

    color_emoji = "🔴" if color == "red" else ("🟢" if color == "green" else "⚫")
    text = (
        f"🎯 <b>Рулетка</b>\n\n"
        f"🎡 Выпало: <b>{color_emoji} {result_num}</b>\n"
        f"🎲 Ваша ставка: {bet_type}{' — '+str(bet_val) if bet_val is not None else ''}\n\n"
        f"{result_text}\n💰 Баланс: <b>{new_balance}</b>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML")


# ── Роутер текстовых сообщений ───────────────────────────────

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    user = update.effective_user

    # Обработка сессий
    session = user_sessions.get(user.id)
    if session:
        # Сначала пробуем OCP и deposit сессии
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

    mapping = {
        "🎰 Игры":             games_menu,
        "📊 Профиль":          show_profile,
        "🎁 Ежедневный бонус": daily_bonus,
        "◀️ Назад":            back_to_main,
        "🎰 Слоты":            slots_start,
        "🃏 Блэкджек":         blackjack_start,
        "🎯 Рулетка":          roulette_start,
    }
    handler = mapping.get(txt)
    if handler:
        await handler(update, ctx)


# ── Вывод / история переводов (заглушки если нет) ────────────

async def cmd_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.ensure_user(user.id, user.username or user.first_name)
    row = db.get_user(user.id)
    game_bal = row["game_balance"] if row["game_balance"] is not None else 0
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            f"🎮 <b>Вывод из игры в бот</b>\n\n"
            f"Баланс игры: <b>{game_bal} монет</b>\n\n"
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
    user = update.effective_user
    db.ensure_user(user.id, user.username or user.first_name)
    history = db.get_transfer_history(user.id, 10)
    if not history:
        await update.message.reply_text("📋 История переводов пуста.")
        return
    lines = ["📋 <b>История переводов:</b>\n"]
    for t in history:
        arrow = "➡️ В игру" if t["direction"] == "deposit" else "⬅️ В бот"
        import datetime
        dt = datetime.datetime.fromtimestamp(t["created_at"]).strftime("%d.%m %H:%M")
        lines.append(f"{arrow} <b>{t['amount']}</b> монет — {dt}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def webapp_data_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Получение данных из WebApp (кликер)."""
    try:
        data = json.loads(update.effective_message.web_app_data.data)
        user_id = update.effective_user.id
        db.ensure_user(user_id, update.effective_user.username or update.effective_user.first_name)
        action = data.get("action")
        amount = int(data.get("amount", 0))

        if action == "withdraw" and amount > 0:
            # Кликер отправляет монеты в бот: game_balance → balance
            row = db.get_user(user_id)
            game_bal = row["game_balance"] if row and row["game_balance"] is not None else 0
            if amount > game_bal:
                await update.message.reply_text(
                    f"❌ <b>Недостаточно средств в игре!</b>\n"
                    f"🎮 Баланс игры: <b>{game_bal} монет</b>\n"
                    f"Запрошено: <b>{amount} монет</b>",
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
                    parse_mode="HTML",
                    reply_markup=make_main_kb(game_bal2)
                )
            else:
                await update.message.reply_text(f"❌ {result}")

        elif action == "deposit" and amount > 0:
            # Кликер забирает монеты из бота: balance → game_balance
            row = db.get_user(user_id)
            if not row or row["balance"] < amount:
                bal = row["balance"] if row else 0
                await update.message.reply_text(
                    f"❌ <b>Недостаточно монет в боте!</b>\n"
                    f"💰 Баланс бота: <b>{bal} монет</b>\n"
                    f"Запрошено: <b>{amount} монет</b>",
                    parse_mode="HTML"
                )
                return
            ok, result = db.deposit_to_game(user_id, amount)
            if ok:
                row2 = db.get_user(user_id)
                game_bal2 = row2["game_balance"] if row2 and row2["game_balance"] is not None else 0
                await update.message.reply_text(
                    f"✅ <b>Переведено {amount} монет в игру!</b>\n"
                    f"🎮 Баланс игры: <b>{game_bal2} монет</b>\n"
                    f"💰 Баланс бота: <b>{row2['balance']} монет</b>",
                    parse_mode="HTML",
                    reply_markup=make_main_kb(game_bal2)
                )

        elif action == "sync_balance":
            # Кликер запрашивает актуальный game_balance при старте (на случай если нужно)
            row = db.get_user(user_id)
            game_bal = row["game_balance"] if row and row["game_balance"] is not None else 0
            await update.message.reply_text(
                f"🎮 <b>Баланс игры синхронизирован</b>\n"
                f"🎮 Баланс игры: <b>{game_bal} монет</b>",
                parse_mode="HTML",
                reply_markup=make_main_kb(game_bal)
            )

    except Exception as e:
        logger.error(f"webapp_data_handler error: {e}")


# ── Топ игроков ───────────────────────────────────────────────

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db.get_top_users(10)
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = ["🏆 <b>ТОП-10 игроков</b>\n"]
    for i, row in enumerate(rows):
        medal = medals[i]
        raw = row["username"] or str(row["user_id"])
        # username в БД может быть first_name (без @) или логин
        # Если начинается без пробела и нет спецсимволов — считаем логином
        has_username = raw and " " not in raw and len(raw) <= 32
        display_name = raw
        username_line = f"\n   <i>@{raw}</i>" if has_username else ""
        lines.append(
            f"{medal} <b>{display_name}</b> — {row['balance']} 💰  Ур.{row['level']}{username_line}"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


# ── /ocp — Секретная панель администратора ────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in _admin_ids


async def cmd_ocp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return  # Молча игнорируем

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Найти пользователя", callback_data="ocp_find")],
        [InlineKeyboardButton("💰 Изменить баланс",    callback_data="ocp_setbal")],
        [InlineKeyboardButton("🎮 Изменить game_balance", callback_data="ocp_setgame")],
        [InlineKeyboardButton("📊 Топ игроков (все)",  callback_data="ocp_top")],
        [InlineKeyboardButton("📋 Список пользователей", callback_data="ocp_list")],
    ])
    await update.message.reply_text(
        "🔐 <b>Панель администратора</b>\n\nВыберите действие:",
        reply_markup=kb, parse_mode="HTML"
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

    elif data == "ocp_top":
        rows = db.get_top_users(20)
        lines = ["📊 <b>Топ-20 (все):</b>\n"]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. <code>{row['user_id']}</code> | {row['username']} | {row['balance']}💰 | Ур.{row['level']}")
        await q.edit_message_text("\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ocp_back")]]))

    elif data == "ocp_list":
        conn = db.get_conn()
        rows = conn.execute("SELECT user_id, username, balance, level, games_played, created_at FROM users ORDER BY created_at DESC LIMIT 20").fetchall()
        conn.close()
        import datetime
        lines = ["📋 <b>Последние 20 регистраций:</b>\n"]
        for row in rows:
            dt = datetime.datetime.fromtimestamp(row["created_at"]).strftime("%d.%m.%y") if row["created_at"] else "?"
            lines.append(f"<code>{row['user_id']}</code> | {row['username']} | {row['balance']}💰 | {dt}")
        await q.edit_message_text("\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ocp_back")]]))

    elif data == "ocp_back":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Найти пользователя", callback_data="ocp_find")],
            [InlineKeyboardButton("💰 Изменить баланс",    callback_data="ocp_setbal")],
            [InlineKeyboardButton("🎮 Изменить game_balance", callback_data="ocp_setgame")],
            [InlineKeyboardButton("📊 Топ игроков (все)",  callback_data="ocp_top")],
            [InlineKeyboardButton("📋 Список пользователей", callback_data="ocp_list")],
        ])
        await q.edit_message_text("🔐 <b>Панель администратора</b>\n\nВыберите действие:",
            reply_markup=kb, parse_mode="HTML")

    elif data.startswith("ocp_info_"):
        target_id = int(data.split("_")[2])
        row = db.get_user(target_id)
        if not row:
            await q.answer("Пользователь не найден.", show_alert=True)
            return
        import datetime
        reg = datetime.datetime.fromtimestamp(row["created_at"]).strftime("%d.%m.%Y %H:%M") if row["created_at"] else "?"
        game_bal = row["game_balance"] if row["game_balance"] is not None else 0
        text = (
            f"👤 <b>Информация о пользователе</b>\n\n"
            f"🪪 ID: <code>{row['user_id']}</code>\n"
            f"👤 Username: @{row['username']}\n"
            f"💰 Баланс (бот): <b>{row['balance']}</b>\n"
            f"🎮 Баланс (игра): <b>{game_bal}</b>\n"
            f"⭐ Уровень: {row['level']}  XP: {row['xp']}\n"
            f"🎮 Игр: {row['games_played']}  ✅ {row['wins']}  ❌ {row['losses']}\n"
            f"📅 Регистрация: {reg}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Изменить баланс", callback_data=f"ocp_editbal_{target_id}")],
            [InlineKeyboardButton("🎮 Изменить game_balance", callback_data=f"ocp_editgame_{target_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="ocp_back")],
        ])
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

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


# ── Расширенный text_router для сессий OCP / deposit ─────────

async def _handle_ocp_sessions(update: Update, session: dict, txt: str, uid: int) -> bool:
    """Обрабатывает OCP и deposit сессии. Возвращает True если обработано."""
    stype = session.get("type")

    # ── Вывод из игры: своя сумма ─────────────────────────────
    if stype == "wgame_custom":
        del user_sessions[uid]
        if not txt.isdigit() or int(txt) <= 0:
            await update.message.reply_text("❌ Введите положительное число.")
            return True
        amount = int(txt)
        row = db.get_user(uid)
        game_bal = row["game_balance"] if row and row["game_balance"] is not None else 0
        if amount > game_bal:
            await update.message.reply_text(
                f"❌ Недостаточно средств. В игре: <b>{game_bal} монет</b>",
                parse_mode="HTML"
            )
            return True
        ok, result = db.withdraw_from_game(uid, amount)
        if ok:
            await update.message.reply_text(
                f"✅ Выведено <b>{amount} монет</b> из игры в бот!\n💰 Баланс бота: <b>{result}</b>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(f"❌ {result}")
        return True

    # ── Вывод в игру: своя сумма ──────────────────────────────
    if stype == "deposit_custom":
        del user_sessions[uid]
        if not txt.isdigit() or int(txt) <= 0:
            await update.message.reply_text("❌ Введите положительное число.")
            return True
        amount = int(txt)
        ok, result = db.deposit_to_game(uid, amount)
        if ok:
            await update.message.reply_text(
                f"✅ Переведено <b>{amount} монет</b> в игру!\n🎮 Баланс игры: <b>{result}</b>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(f"❌ {result}")
        return True

    # ── OCP: поиск пользователя ───────────────────────────────
    if stype == "ocp_find":
        del user_sessions[uid]
        target_id = None
        if txt.isdigit():
            target_id = int(txt)
        elif txt.startswith("@"):
            conn = db.get_conn()
            row = conn.execute("SELECT user_id FROM users WHERE username=?", (txt[1:],)).fetchone()
            conn.close()
            if row:
                target_id = row["user_id"]
        if not target_id:
            await update.message.reply_text("❌ Пользователь не найден.")
            return True
        row = db.get_user(target_id)
        if not row:
            await update.message.reply_text("❌ Пользователь не найден в базе.")
            return True
        import datetime
        reg = datetime.datetime.fromtimestamp(row["created_at"]).strftime("%d.%m.%Y %H:%M") if row["created_at"] else "?"
        game_bal = row["game_balance"] if row["game_balance"] is not None else 0
        text = (
            f"👤 <b>Информация</b>\n\n"
            f"🪪 ID: <code>{row['user_id']}</code>\n"
            f"👤 Username: @{row['username']}\n"
            f"💰 Баланс (бот): <b>{row['balance']}</b>\n"
            f"🎮 Баланс (игра): <b>{game_bal}</b>\n"
            f"⭐ Уровень: {row['level']}  XP: {row['xp']}\n"
            f"🎮 Игр: {row['games_played']}  ✅ {row['wins']}  ❌ {row['losses']}\n"
            f"📅 Регистрация: {reg}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Изменить баланс",      callback_data=f"ocp_editbal_{target_id}")],
            [InlineKeyboardButton("🎮 Изменить game_balance", callback_data=f"ocp_editgame_{target_id}")],
            [InlineKeyboardButton("◀️ В меню",               callback_data="ocp_back")],
        ])
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
        return True

    # ── OCP: установить баланс (шаг 1 — ID) ──────────────────
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

    # ── OCP: установить game_balance (шаг 1 — ID) ────────────
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

    # ── OCP: применить изменение баланса ─────────────────────
    if stype == "ocp_editbal_amount":
        del user_sessions[uid]
        target_id = session["target_id"]
        conn = db.get_conn()
        row = conn.execute("SELECT balance FROM users WHERE user_id=?", (target_id,)).fetchone()
        if not row:
            conn.close()
            await update.message.reply_text("❌ Пользователь не найден.")
            return True
        if txt.startswith("+") and txt[1:].isdigit():
            new_bal = row["balance"] + int(txt[1:])
        elif txt.startswith("-") and txt[1:].isdigit():
            new_bal = max(0, row["balance"] - int(txt[1:]))
        elif txt.isdigit():
            new_bal = int(txt)
        else:
            conn.close()
            await update.message.reply_text("❌ Неверный формат. Используйте число, +100 или -100.")
            return True
        conn.execute("UPDATE users SET balance=? WHERE user_id=?", (new_bal, target_id))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ Баланс пользователя <code>{target_id}</code> изменён на <b>{new_bal} монет</b>.",
            parse_mode="HTML"
        )
        return True

    # ── OCP: применить изменение game_balance ─────────────────
    if stype == "ocp_editgame_amount":
        del user_sessions[uid]
        target_id = session["target_id"]
        conn = db.get_conn()
        row = conn.execute("SELECT game_balance FROM users WHERE user_id=?", (target_id,)).fetchone()
        if not row:
            conn.close()
            await update.message.reply_text("❌ Пользователь не найден.")
            return True
        cur = row["game_balance"] if row["game_balance"] is not None else 0
        if txt.startswith("+") and txt[1:].isdigit():
            new_val = cur + int(txt[1:])
        elif txt.startswith("-") and txt[1:].isdigit():
            new_val = max(0, cur - int(txt[1:]))
        elif txt.isdigit():
            new_val = int(txt)
        else:
            conn.close()
            await update.message.reply_text("❌ Неверный формат. Используйте число, +100 или -100.")
            return True
        conn.execute("UPDATE users SET game_balance=? WHERE user_id=?", (new_val, target_id))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ game_balance пользователя <code>{target_id}</code> изменён на <b>{new_val} монет</b>.",
            parse_mode="HTML"
        )
        return True

    return False


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
        ("withdraw",   cmd_withdraw),
        ("transfers",  cmd_transfers),
        ("top",        cmd_top),
        ("ocp",        cmd_ocp),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, webapp_data_handler))
    app.add_handler(CallbackQueryHandler(slots_callback,           pattern=r"^slots_"))
    app.add_handler(CallbackQueryHandler(bj_callback,              pattern=r"^bj_"))
    app.add_handler(CallbackQueryHandler(roulette_number_callback, pattern=r"^rl_num_"))
    app.add_handler(CallbackQueryHandler(roulette_callback,        pattern=r"^rl_"))
    app.add_handler(CallbackQueryHandler(profile_callback,         pattern=r"^(profile_|deposit_|wgame_)"))
    app.add_handler(CallbackQueryHandler(ocp_callback,             pattern=r"^ocp_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logger.info("GOLDCLICK Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
