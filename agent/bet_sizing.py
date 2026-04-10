"""Geometric bet sizing and SPR-aware strategy adjustments."""


def geometric_bet_size(pot: int, stack: int, streets_remaining: int) -> int:
    """Calculate geometric bet size — equal % of pot each street to go all-in by river.

    Formula: bet = pot * ((stack + pot) / pot) ^ (1/streets) - pot) / 2
    Simplified: find x such that betting x% of pot each street exhausts stack.
    """
    if streets_remaining <= 0 or stack <= 0:
        return 0

    if streets_remaining == 1 or stack <= pot:
        return stack  # just shove

    # Geometric ratio: r such that pot * r^n = pot + 2*stack
    # (pot grows by bet + call = 2*bet each street if called)
    # r = ((pot + 2*stack) / pot) ^ (1/n)
    # bet = pot * (r - 1) / 2  ... but simpler:
    # We want equal fraction f of current pot each street.
    # After n streets of betting f*pot (and being called), stack should be 0.
    # pot_after = pot * (1 + 2f)^n, total_bet = stack
    # Solving: (1 + 2f)^n = (pot + 2*stack) / pot
    # f = ((pot + 2*stack) / pot) ^ (1/n) / 2 - 0.5

    target_ratio = (pot + 2 * stack) / pot
    growth_per_street = target_ratio ** (1.0 / streets_remaining)
    fraction = (growth_per_street - 1.0) / 2.0

    bet = int(pot * fraction)
    bet = max(1, min(bet, stack))
    return bet


def calculate_spr(stack: int, pot: int) -> float:
    """Stack-to-pot ratio."""
    if pot <= 0:
        return 999.0
    return stack / pot


def get_spr_category(spr: float) -> str:
    """Categorize SPR into shallow/medium/deep."""
    if spr <= 3.0:
        return "shallow"
    if spr < 13.0:
        return "medium"
    return "deep"


def spr_adjust_action(
    base_action: str,
    equity: float,
    spr: float,
    can_raise: bool,
    min_raise: int | None,
) -> str:
    """Adjust action based on SPR.

    - Shallow (SPR ≤ 3): commit with decent hands, don't fold reasonable equity
    - Medium (SPR 3-13): standard play, no adjustment
    - Deep (SPR 13+): don't overcommit with marginal hands
    """
    category = get_spr_category(spr)
    parts = base_action.split()
    verb = parts[0]

    if category == "shallow":
        # Don't fold with reasonable equity when pot-committed
        if verb == "fold" and equity >= 0.33:
            return "call"
        # With strong equity, push
        if equity >= 0.55 and can_raise and min_raise:
            return f"raise {min_raise}"  # min-raise (likely all-in at low SPR)
        return base_action

    if category == "deep":
        # Don't overcommit with marginal hands
        if verb == "raise" and equity < 0.70:
            if len(parts) == 2:
                try:
                    amt = int(parts[1])
                    # Cap raise to 50% of pot equivalent
                    if equity < 0.65:
                        return "call" if equity >= 0.40 else base_action
                except ValueError:
                    pass
        # Very strong hands still raise
        return base_action

    # Medium — no adjustment
    return base_action
