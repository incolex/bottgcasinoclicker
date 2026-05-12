# ============================================================
#  games.py — Логика всех мини-игр (ИСПРАВЛЕНО)
# ============================================================

import random


# ── 🎰 СЛОТЫ ─────────────────────────────────────────────────

SLOT_SYMBOLS = ["🍒", "🍋", "🍊", "🍇", "⭐", "💎", "7️⃣", "🎰"]
SLOT_WEIGHTS  = [30,   25,   20,   15,    6,    3,    1,    0.5]

SLOT_MULTIPLIERS = {
    "🍒": 2,   "🍋": 3,   "🍊": 4,   "🍇": 5,
    "⭐": 10,  "💎": 20,  "7️⃣": 50,  "🎰": 100,
}


def spin_slots():
    reels = random.choices(SLOT_SYMBOLS, weights=SLOT_WEIGHTS, k=3)
    if reels[0] == reels[1] == reels[2]:
        mult = SLOT_MULTIPLIERS[reels[0]]
        return reels, mult, True
    if reels[0] == reels[1] or reels[1] == reels[2]:
        symbol = reels[1]
        mult = max(1, SLOT_MULTIPLIERS[symbol] // 3)
        return reels, mult, False
    return reels, 0, False


def format_slots(reels: list) -> str:
    return f"┃ {reels[0]} ┃ {reels[1]} ┃ {reels[2]} ┃"


# ── 🃏 БЛЭКДЖЕК ──────────────────────────────────────────────

CARD_SUITS  = ["♠️", "♥️", "♦️", "♣️"]
CARD_VALUES = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def make_deck():
    return [(v, s) for s in CARD_SUITS for v in CARD_VALUES]


def card_value(card: tuple) -> int:
    v = card[0]
    if v in ("J", "Q", "K"):
        return 10
    if v == "A":
        return 11
    return int(v)


def hand_value(hand: list) -> int:
    total = sum(card_value(c) for c in hand)
    aces  = sum(1 for c in hand if c[0] == "A")
    while total > 21 and aces:
        total -= 10
        aces  -= 1
    return total


def format_hand(hand: list, hide_second: bool = False) -> str:
    if hide_second and len(hand) >= 2:
        return f"{hand[0][0]}{hand[0][1]} 🂠"
    return " ".join(f"{c[0]}{c[1]}" for c in hand)


def dealer_play(deck: list, dealer_hand: list):
    while hand_value(dealer_hand) < 17:
        dealer_hand.append(deck.pop())
    return dealer_hand


def deal_initial():
    deck = make_deck()
    random.shuffle(deck)
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    return deck, player, dealer


# ── 🎯 РУЛЕТКА ───────────────────────────────────────────────

ROULETTE_RED   = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
ROULETTE_BLACK = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}


# ИСПРАВЛЕНО: теперь возвращает (number, color) — два значения как ожидает main.py
def spin_roulette():
    number = random.randint(0, 36)
    if number == 0:
        color = "green"
    elif number in ROULETTE_RED:
        color = "red"
    else:
        color = "black"
    return number, color


# ИСПРАВЛЕНО: добавлена отсутствующая функция check_roulette_win
def check_roulette_win(result_num: int, color: str, bet_type: str, bet_val=None):
    """
    Проверяет выигрыш в рулетке.
    Возвращает (won: bool, multiplier: int)
    """
    if result_num == 0 and bet_type != "zero":
        return False, 0

    if bet_type == "red":
        return color == "red", 2
    if bet_type == "black":
        return color == "black", 2
    if bet_type == "zero":
        return result_num == 0, 36
    if bet_type == "number":
        try:
            target = int(bet_val)
            return result_num == target, 36
        except (TypeError, ValueError):
            return False, 0
    if bet_type == "even":
        return result_num != 0 and result_num % 2 == 0, 2
    if bet_type == "odd":
        return result_num % 2 != 0, 2
    if bet_type == "dozen1":
        return 1 <= result_num <= 12, 3
    if bet_type == "dozen2":
        return 13 <= result_num <= 24, 3
    if bet_type == "dozen3":
        return 25 <= result_num <= 36, 3

    return False, 0


def roulette_payout(bet_type: str, bet_value, result: int) -> int:
    """Совместимость со старым кодом."""
    if result == 0:
        return 0
    if bet_type == "number":
        return 36 if int(bet_value) == result else 0
    if bet_type == "red":
        return 2 if result in ROULETTE_RED else 0
    if bet_type == "black":
        return 2 if result in ROULETTE_BLACK else 0
    if bet_type == "even":
        return 2 if result % 2 == 0 else 0
    if bet_type == "odd":
        return 2 if result % 2 != 0 else 0
    if bet_type == "low":
        return 2 if 1 <= result <= 18 else 0
    if bet_type == "high":
        return 2 if 19 <= result <= 36 else 0
    return 0


def roulette_color(number: int) -> str:
    if number == 0:
        return "🟢"
    return "🔴" if number in ROULETTE_RED else "⚫"


# ── ⚔️ ДУЭЛЬ ─────────────────────────────────────────────────

def duel_roll():
    return random.randint(1, 100)


def resolve_duel_game(challenger_id: int, opponent_id: int):
    roll_c = duel_roll()
    roll_o = duel_roll()
    if roll_c == roll_o:
        roll_c += random.randint(1, 5)
    winner = challenger_id if roll_c > roll_o else opponent_id
    loser  = opponent_id  if winner == challenger_id else challenger_id
    return winner, loser, roll_c, roll_o


# ── 🎲 КОСТИ ─────────────────────────────────────────────────

def roll_dice():
    d1, d2 = random.randint(1, 6), random.randint(1, 6)
    total = d1 + d2
    return d1, d2, total


DICE_FACES = {1:"1️⃣", 2:"2️⃣", 3:"3️⃣", 4:"4️⃣", 5:"5️⃣", 6:"6️⃣"}


def dice_payout(bet_type: str, total: int) -> int:
    """bet_type: 'high'(8-12)=2x, 'low'(2-6)=2x, 'exact_N'=5x, 'lucky7'=4x"""
    if bet_type == "high":
        return 2 if total >= 8 else 0
    if bet_type == "low":
        return 2 if total <= 6 else 0
    if bet_type == "lucky7":
        return 4 if total == 7 else 0
    if bet_type.startswith("exact_"):
        n = int(bet_type.split("_")[1])
        return 5 if total == n else 0
    return 0


# ── 🪙 МОНЕТКА ───────────────────────────────────────────────

def flip_coin() -> str:
    return random.choice(["heads", "tails"])


def coin_result_emoji(side: str) -> str:
    return "👑" if side == "heads" else "🦅"


# ── 🔮 ГАДАНИЕ ───────────────────────────────────────────────

FORTUNE_TEXTS = [
    "✨ Сегодня удача на вашей стороне — рискните немного больше!",
    "🌑 Звёзды советуют беречь монеты и не рисковать сегодня.",
    "🎯 Вас ждёт неожиданная прибыль в самый обычный момент.",
    "⚡ Энергия сегодня бурлит — выигрыш придёт быстро и неожиданно.",
    "🌺 Цветок удачи распустился для вас. Действуйте смело!",
    "🐉 Дракон удачи охраняет вас. Победы ждут!",
    "💫 Ваша интуиция сегодня сильнее разума — доверьтесь ей.",
    "🌊 Волна удачи приходит. Будьте готовы поймать её.",
    "🕯️ Тихая сила таится в вас сегодня. Наблюдайте и выбирайте момент.",
    "🦋 Перемены к лучшему уже на пороге.",
    "🎪 День полон сюрпризов — некоторые очень приятные!",
    "🏔️ Стойкость вознаграждается. Продолжайте — успех близок.",
]


def get_fortune() -> str:
    return random.choice(FORTUNE_TEXTS)


# ── 💣 МИНЫ ───────────────────────────────────────────────────

MINES_GRID_SIZE = 25  # 5×5


def mines_multiplier(mines: int, opened: int) -> float:
    """
    Расчёт множителя на основе вероятности выживания.
    House-edge 0.75 (казино забирает 25% — честный баланс).
    """
    if opened == 0:
        return 1.0
    total = MINES_GRID_SIZE  # 25
    safe = total - mines
    prob = 1.0
    for i in range(opened):
        if (total - i) == 0:
            return 0.0
        prob *= (safe - i) / (total - i)
    if prob <= 0:
        return 0.0
    return round(0.75 / prob, 2)


def mines_place(mines: int) -> list:
    """Случайно расставить мины, вернуть список индексов (0–24)."""
    return random.sample(range(MINES_GRID_SIZE), mines)


def mines_is_mine(mine_positions: list, cell: int) -> bool:
    return cell in mine_positions
