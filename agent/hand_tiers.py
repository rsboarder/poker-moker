"""Preflop hand ranking table — 169 starting hands mapped to 5 tiers.

Tier 1: Premium (always raise)
Tier 2: Strong (raise, 3-bet)
Tier 3: Playable (raise in position, call)
Tier 4: Marginal (play in late position only)
Tier 5: Trash (fold unless big blind special)

Based on Sklansky-Malmuth + Hellmuth groupings.
"""

# fmt: off
HAND_TIERS: dict[str, int] = {
    # Tier 1 — Premium
    "AA": 1, "KK": 1, "QQ": 1, "AKs": 1, "AKo": 1,

    # Tier 2 — Strong
    "JJ": 2, "TT": 2, "AQs": 2, "AQo": 2, "AJs": 2,
    "KQs": 2, "99": 2,

    # Tier 3 — Playable
    "ATs": 3, "AJo": 3, "KJs": 3, "KQo": 3, "QJs": 3,
    "JTs": 3, "88": 3, "77": 3, "ATo": 3, "A9s": 3,
    "KTs": 3, "QTs": 3, "T9s": 3, "98s": 3,

    # Tier 4 — Marginal (late position only)
    "A8s": 4, "A7s": 4, "A6s": 4, "A5s": 4, "A4s": 4,
    "A3s": 4, "A2s": 4, "KJo": 4, "K9s": 4, "K8s": 4,
    "QJo": 4, "Q9s": 4, "J9s": 4, "T8s": 4, "87s": 4,
    "76s": 4, "65s": 4, "66": 4, "55": 4, "44": 4,
    "33": 4, "22": 4, "JTo": 4,
}
# fmt: on

# Everything not listed is Tier 5 (trash)
DEFAULT_TIER = 5

RANKS = "23456789TJQKA"
RANK_VALUE = {r: i for i, r in enumerate(RANKS, 2)}

# Positions from earliest to latest
POSITIONS = ["UTG", "UTG1", "UTG2", "MP", "HJ", "CO", "BTN", "SB", "BB"]
LATE_POSITIONS = {"CO", "BTN"}
BLIND_POSITIONS = {"SB", "BB"}


def _normalize_hand(card1: str, card2: str) -> str:
    r1, s1 = card1[0], card1[1]
    r2, s2 = card2[0], card2[1]
    v1, v2 = RANK_VALUE[r1], RANK_VALUE[r2]

    if v1 < v2:
        r1, r2 = r2, r1
        s1, s2 = s2, s1

    if r1 == r2:
        return f"{r1}{r2}"
    suffix = "s" if s1 == s2 else "o"
    return f"{r1}{r2}{suffix}"


def get_hand_tier(card1: str, card2: str) -> int:
    hand = _normalize_hand(card1, card2)
    return HAND_TIERS.get(hand, DEFAULT_TIER)


def get_position_adjusted_tier(card1: str, card2: str, position: str) -> int:
    tier = get_hand_tier(card1, card2)
    pos = position.upper()

    if pos in LATE_POSITIONS:
        tier = max(1, tier - 1)
    elif pos in BLIND_POSITIONS:
        pass  # no adjustment — play standard
    else:
        # early/mid position — tighten up
        tier = min(5, tier + 1)

    return tier


def is_premium(card1: str, card2: str) -> bool:
    return get_hand_tier(card1, card2) <= 2


def is_trash(card1: str, card2: str, position: str = "MP") -> bool:
    return get_position_adjusted_tier(card1, card2, position) >= 5


# ── First-in open ranges by position (3-handed) ─────────────────────────────
# Returns: "raise", "call", or "fold" for unopened pot preflop

# Max tier to OPEN (raise first in) by position
OPEN_RAISE_MAX_TIER = {
    "UTG": 3,   "UTG1": 3,  "UTG2": 3,  "MP": 3,
    "HJ": 3,    "CO": 4,    "BTN": 4,
    "SB": 4,    "BB": 4,    # BB can open wider but not trash
}

# Max tier to CALL a raise by position
CALL_RAISE_MAX_TIER = {
    "UTG": 2,   "UTG1": 2,  "UTG2": 2,  "MP": 2,
    "HJ": 3,    "CO": 3,    "BTN": 3,
    "SB": 3,    "BB": 4,    # BB gets best price
}


def get_preflop_action(card1: str, card2: str, position: str,
                       facing_raise: bool = False) -> str:
    """Return preflop action based on position charts.

    Returns: 'raise', 'call', or 'fold'
    """
    tier = get_hand_tier(card1, card2)
    pos = position.upper() if position else "MP"

    if facing_raise:
        # Facing a raise — tighter
        max_tier = CALL_RAISE_MAX_TIER.get(pos, 3)
        if tier <= 2:
            return "raise"  # 3-bet with premium
        if tier <= max_tier:
            return "call"
        return "fold"
    else:
        # First in (unopened) — wider
        max_tier = OPEN_RAISE_MAX_TIER.get(pos, 3)
        if tier <= max_tier:
            return "raise"
        return "fold"
