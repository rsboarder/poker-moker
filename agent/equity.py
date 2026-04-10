"""Monte Carlo equity calculator using eval7."""

import eval7

CARD_CACHE: dict[str, eval7.Card] = {}


def _card(s: str) -> eval7.Card:
    if s not in CARD_CACHE:
        CARD_CACHE[s] = eval7.Card(s)
    return CARD_CACHE[s]


# Default opponent range — top ~30% of hands
DEFAULT_RANGE = eval7.HandRange(
    "AA,KK,QQ,JJ,TT,99,88,77,66,55,44,33,22,"
    "AKs,AQs,AJs,ATs,A9s,A8s,A7s,A6s,A5s,A4s,A3s,A2s,"
    "AKo,AQo,AJo,ATo,"
    "KQs,KJs,KTs,K9s,KQo,"
    "QJs,QTs,Q9s,QJo,"
    "JTs,J9s,JTo,"
    "T9s,T8s,"
    "98s,87s,76s,65s,54s"
)

# Tighter range for aggressive opponents
TIGHT_RANGE = eval7.HandRange(
    "AA,KK,QQ,JJ,TT,99,"
    "AKs,AQs,AJs,ATs,"
    "AKo,AQo,"
    "KQs,KJs,"
    "QJs,JTs"
)

# Wider range for loose opponents
LOOSE_RANGE = eval7.HandRange(
    "AA,KK,QQ,JJ,TT,99,88,77,66,55,44,33,22,"
    "AKs,AQs,AJs,ATs,A9s,A8s,A7s,A6s,A5s,A4s,A3s,A2s,"
    "AKo,AQo,AJo,ATo,A9o,A8o,"
    "KQs,KJs,KTs,K9s,K8s,KQo,KJo,KTo,"
    "QJs,QTs,Q9s,Q8s,QJo,QTo,"
    "JTs,J9s,J8s,JTo,J9o,"
    "T9s,T8s,T7s,T9o,"
    "98s,97s,96s,98o,"
    "87s,86s,87o,"
    "76s,75s,76o,"
    "65s,64s,65o,"
    "54s,53s,54o,"
    "43s"
)


def get_opponent_range(vpip: float | None) -> eval7.HandRange:
    if vpip is None:
        return DEFAULT_RANGE
    if vpip < 0.22:
        return TIGHT_RANGE
    if vpip > 0.40:
        return LOOSE_RANGE
    return DEFAULT_RANGE


def calculate_equity(
    hole_cards: list[str],
    community_cards: list[str],
    opponent_range: eval7.HandRange | None = None,
    iterations: int = 5000,
) -> float:
    if len(hole_cards) != 2:
        return 0.0

    hand = [_card(c) for c in hole_cards]
    board = [_card(c) for c in community_cards]
    opp_range = opponent_range or DEFAULT_RANGE

    return eval7.py_hand_vs_range_monte_carlo(hand, opp_range, board, iterations)


def calculate_pot_odds(to_call: int, pot: int) -> float:
    if to_call <= 0:
        return 0.0
    return to_call / (pot + to_call)


def should_call_by_odds(equity: float, pot_odds: float, margin: float = 0.05) -> bool:
    return equity >= pot_odds + margin


def categorize_equity(equity: float) -> str:
    if equity >= 0.70:
        return "strong"
    if equity >= 0.55:
        return "good"
    if equity >= 0.40:
        return "marginal"
    if equity >= 0.25:
        return "weak"
    return "very_weak"
