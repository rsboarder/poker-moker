"""Board texture classification — dry/wet/monotone for bet sizing adjustments."""

import logging
import re

logger = logging.getLogger("agent")

RANK_VALUES = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8,
               "9": 9, "T": 10, "J": 11, "Q": 12, "K": 13, "A": 14}


def classify_board(community_cards: list[str]) -> str:
    """Classify board texture as 'dry', 'wet', or 'monotone'.

    Dry: rainbow, unconnected, paired (e.g. K-7-2 rainbow)
    Wet: flush draws, straight draws, connected (e.g. J-T-9 two-tone)
    Monotone: 3+ cards same suit (flush possible)
    """
    if len(community_cards) < 3:
        return "none"

    suits = [c[1] for c in community_cards]
    ranks = [RANK_VALUES.get(c[0], 0) for c in community_cards]

    # Monotone: 3+ same suit
    suit_counts = {}
    for s in suits:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    max_suited = max(suit_counts.values())
    if max_suited >= 3:
        return "monotone"

    # Count flush draws (2 of same suit)
    has_flush_draw = max_suited >= 2

    # Count straight connectivity
    sorted_ranks = sorted(set(ranks))
    connectivity = 0
    for i in range(len(sorted_ranks) - 1):
        gap = sorted_ranks[i + 1] - sorted_ranks[i]
        if gap <= 2:  # connected or one-gapper
            connectivity += 1

    # Paired board
    is_paired = len(set(ranks)) < len(ranks)

    # High cards (broadways) increase wetness
    high_cards = sum(1 for r in ranks if r >= 10)

    # Score wetness
    wetness = 0
    if has_flush_draw:
        wetness += 2
    wetness += connectivity
    if high_cards >= 2:
        wetness += 1
    if is_paired:
        wetness -= 1  # paired boards are drier

    if wetness >= 3:
        return "wet"
    if wetness <= 1:
        return "dry"
    return "wet"  # default to wet for borderline (bet bigger to protect)


def texture_bet_multiplier(texture: str) -> float:
    """Return bet size multiplier based on board texture.

    Dry boards: bet small (0.33x pot)
    Wet boards: bet large (0.66x pot)
    Monotone: bet large (0.75x pot) — charge draws heavily
    """
    return {"dry": 0.33, "wet": 0.66, "monotone": 0.75, "none": 0.50}[texture]
