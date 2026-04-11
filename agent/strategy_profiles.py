"""Configurable strategy profiles — different bot personalities."""

from dataclasses import dataclass, field


@dataclass
class StrategyProfile:
    name: str

    # Equity thresholds
    equity_strong: float = 0.70
    equity_good: float = 0.55
    equity_grey_lower: float = 0.30
    equity_trash: float = 0.25

    # Preflop open ranges (max tier to open by position category)
    open_early: int = 3       # UTG, MP
    open_late: int = 4        # CO, BTN
    open_blind: int = 4       # SB, BB

    # Bet sizing multipliers (relative to default)
    bet_size_mult: float = 1.0
    bluff_frequency: float = 0.0   # 0.0 = never bluff, 1.0 = max bluff

    # Exploit sensitivity (how aggressively to exploit)
    exploit_mult: float = 1.0

    # LLM personality hint (appended to prompt)
    personality: str = ""


PROFILES: dict[str, StrategyProfile] = {
    "tight": StrategyProfile(
        name="tight",
        equity_strong=0.70,
        equity_good=0.55,
        equity_grey_lower=0.30,
        equity_trash=0.25,
        open_early=2,
        open_late=3,
        open_blind=3,
        bet_size_mult=0.9,
        bluff_frequency=0.0,
        exploit_mult=0.5,
        personality="Play tight-aggressive. Fold marginal hands. Only bet with strong holdings.",
    ),
    "aggressive": StrategyProfile(
        name="aggressive",
        equity_strong=0.65,
        equity_good=0.50,
        equity_grey_lower=0.25,
        equity_trash=0.20,
        open_early=3,
        open_late=4,
        open_blind=4,
        bet_size_mult=1.2,
        bluff_frequency=0.3,
        exploit_mult=1.5,
        personality="Play loose-aggressive. Apply pressure with raises. Bluff occasionally when the board favors your range.",
    ),
    "gto": StrategyProfile(
        name="gto",
        equity_strong=0.70,
        equity_good=0.55,
        equity_grey_lower=0.30,
        equity_trash=0.25,
        open_early=3,
        open_late=4,
        open_blind=4,
        bet_size_mult=1.0,
        bluff_frequency=0.15,
        exploit_mult=1.0,
        personality="Play balanced GTO strategy. Mix value bets and bluffs at theoretically correct frequencies.",
    ),
    "maniac": StrategyProfile(
        name="maniac",
        equity_strong=0.60,
        equity_good=0.45,
        equity_grey_lower=0.20,
        equity_trash=0.15,
        open_early=4,
        open_late=5,
        open_blind=5,
        bet_size_mult=1.5,
        bluff_frequency=0.5,
        exploit_mult=2.0,
        personality="Play extremely aggressive. Raise most hands, apply maximum pressure, bluff frequently.",
    ),
    "passive": StrategyProfile(
        name="passive",
        equity_strong=0.75,
        equity_good=0.60,
        equity_grey_lower=0.35,
        equity_trash=0.30,
        open_early=2,
        open_late=3,
        open_blind=3,
        bet_size_mult=0.7,
        bluff_frequency=0.0,
        exploit_mult=0.3,
        personality="Play tight-passive. Check and call with decent hands. Rarely raise without the nuts.",
    ),
}


def get_profile(name: str) -> StrategyProfile:
    return PROFILES.get(name, PROFILES["gto"])
