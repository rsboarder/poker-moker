"""3-layer decision engine: hand tiers → equity → LLM."""

import logging
import re
import time

from equity import (
    calculate_equity,
    calculate_pot_odds,
    categorize_equity,
    get_opponent_range,
)
from hand_tiers import get_hand_tier, get_position_adjusted_tier, get_preflop_action, is_premium, is_trash
from bet_sizing import geometric_bet_size, calculate_spr, get_spr_category, spr_adjust_action
from llm_engine import build_prompt, call_llm, check_reasoning_action_consistency
from opponent_tracker import OpponentTracker
from storage import GameStorage

logger = logging.getLogger("agent")

RE_TO_CALL = re.compile(r"call\s*:?\s*(\d+)", re.IGNORECASE)
RE_MIN_RAISE = re.compile(r"(?:min(?:imum)?(?:\s*raise)?|raise\s*\(min)\s*:?\s*(\d+)", re.IGNORECASE)

# Equity thresholds
EQUITY_STRONG = 0.70
EQUITY_GOOD = 0.55
EQUITY_GREY_UPPER = 0.55
EQUITY_GREY_LOWER = 0.30
EQUITY_TRASH = 0.25

# Stack thresholds (in big blinds)
PUSH_FOLD_BB = 10


def _parse_to_call(valid_line: str) -> int:
    if m := RE_TO_CALL.search(valid_line):
        return int(m.group(1))
    return 0


def _parse_min_raise(valid_line: str) -> int | None:
    if m := RE_MIN_RAISE.search(valid_line):
        return int(m.group(1))
    return None


def _can_check(valid_line: str) -> bool:
    return "/check" in valid_line.lower()


def _can_raise(valid_line: str) -> bool:
    return "/raise" in valid_line.lower()


def make_decision(
    hole_cards: list[str],
    community_cards: list[str],
    street: str,
    pot: int,
    stack: int,
    position: str,
    valid_line: str,
    opponent_tracker: OpponentTracker,
    storage: GameStorage,
    round_num: str = "",
) -> str:
    """Main decision function. Returns action string (e.g. 'fold', 'call', 'raise 200')."""
    start = time.perf_counter()

    to_call = _parse_to_call(valid_line)
    min_raise = _parse_min_raise(valid_line)
    can_check = _can_check(valid_line)
    can_raise = _can_raise(valid_line)

    # ── Hand tier ────────────────────────────────────────────
    hand_tier = get_hand_tier(hole_cards[0], hole_cards[1]) if len(hole_cards) == 2 else 5
    # If position unknown, use raw tier (no tightening)
    if position and len(hole_cards) == 2:
        adj_tier = get_position_adjusted_tier(hole_cards[0], hole_cards[1], position)
    else:
        adj_tier = hand_tier

    # ── Equity calculation ───────────────────────────────────
    avg_vpip = opponent_tracker.get_avg_vpip()
    opp_range = get_opponent_range(avg_vpip)
    eq = calculate_equity(hole_cards, community_cards, opp_range)
    pot_odds = calculate_pot_odds(to_call, pot)
    eq_cat = categorize_equity(eq)

    # ── SPR + geometric sizing context ───────────────────────
    spr = calculate_spr(stack, pot)
    streets_map = {"preflop": 3, "flop": 2, "turn": 1, "river": 0}
    streets_remaining = streets_map.get(street, 1)
    geo_size = geometric_bet_size(pot, stack, streets_remaining) if streets_remaining > 0 else 0

    logger.info(
        "Strategy input — tier=%d adj_tier=%d equity=%.1f%% pot_odds=%.1f%% "
        "to_call=%d can_check=%s street=%s spr=%.1f geo=%d",
        hand_tier, adj_tier, eq * 100, pot_odds * 100, to_call, can_check, street,
        spr, geo_size,
    )

    # ── Layer 1: Preflop charts (instant) ──────────────────────
    action = None
    reasoning = None

    if street == "preflop" and len(hole_cards) == 2:
        facing_raise = to_call > 0
        preflop_action = get_preflop_action(
            hole_cards[0], hole_cards[1], position, facing_raise
        )

        if to_call == 0 and can_check and preflop_action == "fold":
            action = "check"
            source = "rule"
        elif preflop_action == "raise" and can_raise and min_raise:
            if hand_tier <= 1:
                raise_amt = min(min_raise * 3, stack)
            else:
                raise_amt = min(min_raise * 2, stack)
            raise_amt = max(raise_amt, min_raise)
            action = f"raise {raise_amt}"
            source = "rule"
        elif preflop_action == "call" and to_call > 0:
            action = "call"
            source = "rule"
        elif preflop_action == "fold" and to_call > 0 and eq < pot_odds:
            action = "fold"
            source = "rule"
        # else: action stays None → fall through to Layer 2

    # ── Layer 2: Equity-based (fast) ─────────────────────────
    # Determine if we're "checked to" (no cost, can bet) or "facing bet" (must pay)
    checked_to_us = to_call == 0 and can_raise

    if action is None and eq >= EQUITY_STRONG and can_raise and min_raise:
        # Very strong — raise with geometric sizing
        raise_amt = geo_size if geo_size >= min_raise else min(int(pot * 0.75), stack)
        raise_amt = max(raise_amt, min_raise)
        raise_amt = min(raise_amt, stack)
        action = f"raise {raise_amt}"
        source = "equity"
    elif action is None and checked_to_us and eq >= EQUITY_GOOD and min_raise:
        # Checked to us with good hand — value bet with geometric sizing
        raise_amt = geo_size if geo_size >= min_raise else min(int(pot * 0.5), stack)
        raise_amt = max(raise_amt, min_raise)
        raise_amt = min(raise_amt, stack)
        action = f"raise {raise_amt}"
        source = "equity"
    elif action is None and eq < EQUITY_TRASH and to_call > 0:
        action = "fold"
        source = "equity"
    elif action is None and eq < EQUITY_TRASH and can_check:
        action = "check"
        source = "equity"
    elif action is None and to_call == 0 and can_check and eq < EQUITY_GREY_LOWER:
        # Free check, weak hand — just check
        action = "check"
        source = "equity"
    elif action is None and to_call > 0 and eq >= EQUITY_GOOD and eq >= pot_odds + 0.10:
        # Facing bet, good hand, profitable call
        action = "call"
        source = "equity"
    elif action is None and to_call > 0 and eq < pot_odds:
        action = "fold"
        source = "equity"

    # ── Layer 2b: Opponent-conditional adjustments ───────────
    if action is None and checked_to_us and can_raise and min_raise:
        avg_vpip = opponent_tracker.get_avg_vpip()
        if avg_vpip is not None and avg_vpip > 0.40 and eq >= 0.45:
            # Loose-passive opponents — value bet wider (council fix E)
            raise_amt = min(int(pot * 0.6), stack)
            raise_amt = max(raise_amt, min_raise)
            action = f"raise {raise_amt}"
            source = "equity"

    # ── Layer 3: LLM for grey zone ───────────────────────────
    if action is None:
        source = "llm"
        opp_stats_summary = opponent_tracker.get_stats_summary()
        prompt = build_prompt(
            hole_cards=hole_cards,
            community_cards=community_cards,
            street=street,
            pot=pot,
            stack=stack,
            equity=eq,
            pot_odds=pot_odds,
            equity_category=eq_cat,
            hand_tier=hand_tier,
            position=position,
            opponent_stats=opp_stats_summary,
            valid_line=valid_line,
        )

        action, reasoning, latency_ms, timed_out = call_llm(prompt)

        # Log LLM call
        storage.save_llm_call(
            hand_id=None,  # will be linked after hand save
            prompt=prompt,
            raw_response=reasoning or action,
            parsed_action=action,
            model_name=AI_CLI_PATH if not hasattr(call_llm, '_model') else "unknown",
            latency_ms=latency_ms,
            timed_out=timed_out,
        )

        if timed_out:
            # Fallback: use equity-based decision
            if can_check:
                action = "check"
            elif eq >= pot_odds + 0.05:
                action = "call"
            else:
                action = "fold"
            source = "equity_fallback"
        else:
            # Knowing-doing gap fix: check if reasoning contradicts action
            action = check_reasoning_action_consistency(reasoning, action, eq, pot_odds)

    elapsed_ms = (time.perf_counter() - start) * 1000

    # ── SPR adjustment ───────────────────────────────────────
    action = spr_adjust_action(action, eq, spr, can_raise, min_raise)

    # ── Validate action against valid_line ────────────────────
    action = _validate_action(action, valid_line, can_check, to_call, min_raise, stack)

    # ── HARD GUARDRAIL: never fold when check is free ────────
    if action == "fold" and can_check:
        logger.warning("Guardrail: converted fold→check (check is free)")
        action = "check"

    logger.info("Decision: %s (source=%s, %.0fms)", action, source, elapsed_ms)

    # ── Save to storage ──────────────────────────────────────
    storage.save_hand(
        round_num=round_num,
        hole_cards=hole_cards,
        community_cards=community_cards,
        street=street,
        pot=pot,
        stack=stack,
        position=position,
        equity=eq,
        pot_odds=pot_odds,
        hand_tier=hand_tier,
        equity_category=eq_cat,
        llm_reasoning=reasoning if source == "llm" else None,
        decision=action,
        decision_source=source,
        response_time_ms=elapsed_ms,
    )

    return action


# Keep module-level ref for import in llm_engine
AI_CLI_PATH = __import__("os").getenv("CODEX_PATH", "codex")


def _validate_action(
    action: str, valid_line: str, can_check: bool, to_call: int,
    min_raise: int | None, stack: int,
) -> str:
    """Ensure action is legal given valid_line."""
    parts = action.split()
    verb = parts[0]

    vl = valid_line.lower()

    if verb == "check" and "/check" not in vl:
        return "call" if "/call" in vl else "fold"

    if verb == "call" and "/call" not in vl:
        return "check" if can_check else "fold"

    if verb == "raise":
        if "/raise" not in vl:
            return "call" if "/call" in vl else ("check" if can_check else "fold")
        if len(parts) == 2:
            try:
                amt = int(parts[1])
                if min_raise and amt < min_raise:
                    amt = min_raise
                amt = min(amt, stack)
                return f"raise {amt}"
            except ValueError:
                return "call" if "/call" in vl else "fold"
        return "call" if "/call" in vl else "fold"

    if verb == "fold":
        if can_check:
            return "check"
        return "fold"

    return action
