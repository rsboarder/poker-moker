"""Confidence gate — scale exploitation by sample size."""

import logging

logger = logging.getLogger("agent")

# Adaptation curve thresholds (from research)
HANDS_NO_EXPLOIT = 20       # < 20 hands: play GTO, no adjustments
HANDS_SMALL_EXPLOIT = 100   # 20-100: small adjustments
HANDS_FULL_EXPLOIT = 500    # 100-500: active exploitation


def get_confidence(hands_seen: int) -> str:
    """Return confidence level based on number of hands observed.

    Returns: 'none', 'low', 'medium', 'high'
    """
    if hands_seen < HANDS_NO_EXPLOIT:
        return "none"
    if hands_seen < HANDS_SMALL_EXPLOIT:
        return "low"
    if hands_seen < HANDS_FULL_EXPLOIT:
        return "medium"
    return "high"


def exploit_weight(hands_seen: int) -> float:
    """Return 0.0-1.0 weight for how much to exploit.

    Used to scale adjustments: adjustment * exploit_weight(hands)
    0.0 = pure GTO baseline (no data)
    1.0 = full exploitation (lots of data)
    """
    if hands_seen < HANDS_NO_EXPLOIT:
        return 0.0
    if hands_seen < HANDS_SMALL_EXPLOIT:
        return 0.3
    if hands_seen < HANDS_FULL_EXPLOIT:
        return 0.7
    return 1.0


def should_exploit(hands_seen: int, min_confidence: str = "low") -> bool:
    """Check if we have enough data to exploit."""
    confidence = get_confidence(hands_seen)
    levels = {"none": 0, "low": 1, "medium": 2, "high": 3}
    return levels[confidence] >= levels[min_confidence]
