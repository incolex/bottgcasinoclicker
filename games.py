# games.py — Логика мини-игр

import random


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


def deal_initial():
    deck = make_deck()
    random.shuffle(deck)
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    return deck, player, dealer


# ── 🎯 РУЛЕТКА ───────────────────────────────────────────────

ROULETTE_RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}


def spin_roulette():
    number = random.randint(0, 36)
    if number == 0:
        color = "green"
    elif number in ROULETTE_RED:
        color = "red"
    else:
        color = "black"
    return number, color


def check_roulette_win(result_num: int, color: str, bet_type: str, bet_val=None):
    """Возвращает (won: bool, multiplier: int)"""
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
            return result_num == int(bet_val), 36
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


# ── 🪙 МОНЕТКА ───────────────────────────────────────────────

def flip_coin() -> str:
    return random.choice(["heads", "tails"])


def coin_result_emoji(side: str) -> str:
    return "👑" if side == "heads" else "🦅"


# ── 💣 МИНЫ ──────────────────────────────────────────────────

MINES_GRID_SIZE = 25  # 5×5


def mines_multiplier(mines: int, opened: int) -> float:
    """House-edge 0.85 (казино забирает 15%)."""
    if opened == 0:
        return 1.0
    total = MINES_GRID_SIZE
    safe  = total - mines
    prob  = 1.0
    for i in range(opened):
        if (total - i) == 0:
            return 0.0
        prob *= (safe - i) / (total - i)
    if prob <= 0:
        return 0.0
    return round(0.85 / prob, 2)


def mines_place(mines: int) -> list:
    return random.sample(range(MINES_GRID_SIZE), mines)


def mines_is_mine(mine_positions: list, cell: int) -> bool:
    return cell in mine_positions
