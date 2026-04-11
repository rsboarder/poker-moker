"""ToolPoker-lite: structured LLM prompt with equity injection and <think>/<answer> parsing.

Supports two backends:
  - CODEX_PATH=api   → direct Anthropic API call (fast, recommended)
  - CODEX_PATH=claude → Claude Code CLI subprocess
  - CODEX_PATH=codex  → Codex CLI subprocess
"""

import logging
import os
import re
import subprocess
import time

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("agent")

AI_CLI_PATH = os.getenv("CODEX_PATH", "claude")
AI_CLI_TIMEOUT = int(os.getenv("CODEX_TIMEOUT", "50"))
AI_CLI_MODEL = os.getenv("CODEX_MODEL", "haiku")
AI_USE_API = AI_CLI_PATH.lower() == "api"
AI_USE_WARM = AI_CLI_PATH.lower() == "claude"  # warm process for Claude CLI
AI_CLI_IS_CLAUDE = "claude" in AI_CLI_PATH.lower()

RE_ANSWER = re.compile(r"<answer>\s*(.*?)\s*</answer>", re.DOTALL | re.IGNORECASE)
RE_THINK = re.compile(r"<think>\s*(.*?)\s*</think>", re.DOTALL | re.IGNORECASE)
RE_ACTION = re.compile(r"/?(fold|check|call|raise\s+\d+)", re.IGNORECASE)

PROMPT_TEMPLATE = """\
You are an expert Texas Hold'em poker player in a tournament against other AI bots.
Analyze the situation and make ONE decision.

<game_state>
Your hole cards: {hole_cards} (Tier {hand_tier} — {tier_label})
Community cards: {community}
Board texture: {board_texture}
Street: {street} | Pot: {pot} | Your stack: {stack}
SPR (stack-to-pot): {spr:.1f} ({spr_category})
Equity vs opponent range: {equity:.1%}
Pot odds to call: {pot_odds:.1%}
Equity category: {equity_category}
Position: {position}
</game_state>

<solver_recommendation>
{solver_recommendation}
</solver_recommendation>

<opponent_stats>
{opponent_stats}
</opponent_stats>

<valid_actions>
{valid_line}
</valid_actions>

<expert_examples>
{few_shot_examples}
</expert_examples>

<instructions>
Think step by step inside <think> tags, then give your final action inside <answer> tags.

In your analysis, consider:
1. Your hand strength (tier {hand_tier}) and how it connects with the {board_texture} board
2. Equity ({equity:.1%}) vs pot odds ({pot_odds:.1%}) — is calling profitable?
3. The solver recommendation above — do you agree or see a reason to deviate?
4. Opponent tendencies — are they loose/tight, passive/aggressive?
5. SPR {spr:.1f} — at low SPR commit with decent hands, at high SPR be cautious
6. Position — do you act last (advantage) or first?

IMPORTANT: Inside <answer> tags, write ONLY the action. No explanation.
Valid formats: fold | check | call | raise <amount>
Example: <answer>raise 150</answer>
</instructions>
"""

# ── Few-shot expert examples by situation ────────────────────────────────────

FEW_SHOT_EXAMPLES = {
    "preflop_marginal": (
        "Example: Hero has Js Th in CO, facing raise to 3BB. Equity 42%, pot odds 30%.\n"
        "Expert decision: call. Suited broadway connector has good playability postflop."
    ),
    "postflop_value": (
        "Example: Hero has Ah Kd on board Ac 7d 3s (flop). Equity 85%, checked to us.\n"
        "Expert decision: raise 60% pot. Top pair top kicker — value bet, don't slow play."
    ),
    "postflop_draw": (
        "Example: Hero has Jh Th on board 9h 5h Kc (flop). Equity 48%, facing bet.\n"
        "Expert decision: call. Flush draw + gutshot = 12 outs, good implied odds."
    ),
    "river_bluff": (
        "Example: Hero has 7h 6h on board 2h 5h Kc 3d 8s (river). Equity 7%, checked to us.\n"
        "Expert decision: check. Missed draw, no fold equity vs calling station."
    ),
    "shallow_spr": (
        "Example: Hero has As Qd, SPR 1.5, top pair on flop. Equity 60%.\n"
        "Expert decision: raise all-in. At low SPR, commit with any top pair or better."
    ),
}


def _get_few_shot(street: str, equity: float, spr: float, to_call: int) -> str:
    examples = []
    if street == "preflop" and 0.30 <= equity <= 0.55:
        examples.append(FEW_SHOT_EXAMPLES["preflop_marginal"])
    if street != "preflop" and equity >= 0.60 and to_call == 0:
        examples.append(FEW_SHOT_EXAMPLES["postflop_value"])
    if street != "preflop" and 0.35 <= equity <= 0.55:
        examples.append(FEW_SHOT_EXAMPLES["postflop_draw"])
    if street == "river" and equity < 0.20:
        examples.append(FEW_SHOT_EXAMPLES["river_bluff"])
    if spr <= 3.0 and equity >= 0.45:
        examples.append(FEW_SHOT_EXAMPLES["shallow_spr"])
    return "\n".join(examples) if examples else "(no similar examples)"


def _get_solver_recommendation(equity: float, pot_odds: float, to_call: int,
                                can_check: bool, can_raise: bool) -> str:
    if to_call == 0 and can_check:
        if equity >= 0.60 and can_raise:
            return f"Math says: BET for value (equity {equity:.0%} is strong, checked to you)"
        return f"Math says: CHECK is free (equity {equity:.0%})"
    if equity > pot_odds + 0.10:
        return f"Math says: CALL is +EV (equity {equity:.0%} > pot odds {pot_odds:.0%} + margin)"
    if equity < pot_odds:
        return f"Math says: FOLD is correct (equity {equity:.0%} < pot odds {pot_odds:.0%})"
    return f"Math says: BORDERLINE (equity {equity:.0%} ≈ pot odds {pot_odds:.0%}, use judgment)"

TIER_LABELS = {
    1: "premium",
    2: "strong",
    3: "playable",
    4: "marginal",
    5: "trash",
}


def build_prompt(
    hole_cards: list[str],
    community_cards: list[str],
    street: str,
    pot: int,
    stack: int,
    equity: float,
    pot_odds: float,
    equity_category: str,
    hand_tier: int,
    position: str,
    opponent_stats: str,
    valid_line: str,
    board_texture: str = "none",
    spr: float = 10.0,
    to_call: int = 0,
    can_check: bool = False,
    can_raise: bool = False,
) -> str:
    from bet_sizing import get_spr_category
    spr_category = get_spr_category(spr)

    solver_rec = _get_solver_recommendation(equity, pot_odds, to_call, can_check, can_raise)
    few_shot = _get_few_shot(street, equity, spr, to_call)

    return PROMPT_TEMPLATE.format(
        hole_cards=" ".join(hole_cards) if hole_cards else "unknown",
        community=" ".join(community_cards) if community_cards else "none (preflop)",
        board_texture=board_texture,
        street=street,
        pot=pot,
        stack=stack,
        spr=spr,
        spr_category=spr_category,
        equity=equity,
        pot_odds=pot_odds,
        equity_category=equity_category,
        hand_tier=hand_tier,
        tier_label=TIER_LABELS.get(hand_tier, "unknown"),
        position=position or "unknown",
        solver_recommendation=solver_rec,
        opponent_stats=opponent_stats,
        few_shot_examples=few_shot,
        valid_line=valid_line,
    )


def parse_llm_response(raw: str) -> tuple[str, str | None]:
    """Parse LLM response. Returns (action, reasoning)."""
    reasoning = None
    if m := RE_THINK.search(raw):
        reasoning = m.group(1).strip()

    # Try <answer> tags first
    if m := RE_ANSWER.search(raw):
        answer = m.group(1).strip()
        if am := RE_ACTION.search(answer):
            return am.group(1).lower(), reasoning

    # Fallback: scan lines
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if m := RE_ACTION.search(line):
            return m.group(1).lower(), reasoning

    # Final fallback
    if m := RE_ACTION.search(raw):
        return m.group(1).lower(), reasoning

    return "fold", reasoning


# ── Knowing-doing gap fix ────────────────────────────────────────────────────

RE_PROFITABLE = re.compile(
    r"(profitable|positive.*(?:ev|expected value)|equity.*(?:exceed|above|greater|>).*(?:pot odds|odds))",
    re.IGNORECASE,
)
RE_SHOULD_CALL = re.compile(
    r"(should call|i should call|calling is|call is.*profitable|must call)",
    re.IGNORECASE,
)


def check_reasoning_action_consistency(
    reasoning: str | None,
    action: str,
    equity: float,
    pot_odds: float,
) -> str:
    """Fix knowing-doing gap: if reasoning says profitable but action is fold, override.

    Only overrides fold→call when BOTH:
    1. Reasoning text indicates profitability
    2. Math confirms equity > pot_odds
    """
    if not reasoning:
        return action

    if action != "fold":
        return action

    # Check if reasoning says it's profitable
    reasoning_says_profitable = bool(
        RE_PROFITABLE.search(reasoning) or RE_SHOULD_CALL.search(reasoning)
    )

    math_says_call = equity > pot_odds + 0.05

    if reasoning_says_profitable and math_says_call:
        logger.warning(
            "Knowing-doing fix: LLM said fold but reasoning + math say call "
            "(eq=%.1f%% > odds=%.1f%%)", equity * 100, pot_odds * 100
        )
        return "call"

    return action


# ── API backend ──────────────────────────────────────────────────────────────

def _call_api(prompt: str) -> tuple[str, float]:
    """Call Anthropic API directly. Returns (raw_response, latency_ms)."""
    import anthropic

    client = anthropic.Anthropic()
    start = time.perf_counter()
    response = client.messages.create(
        model=AI_CLI_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    raw = response.content[0].text
    return raw, elapsed_ms


# ── CLI backend ──────────────────────────────────────────────────────────────

def _call_cli(prompt: str) -> tuple[str, float]:
    """Call AI CLI subprocess. Returns (raw_response, latency_ms)."""
    if AI_CLI_IS_CLAUDE:
        cmd = [AI_CLI_PATH, "-p", "--no-session-persistence",
               "--model", AI_CLI_MODEL, prompt]
        input_text = None
    else:
        cmd = [AI_CLI_PATH, "exec"]
        input_text = prompt

    start = time.perf_counter()
    result = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=AI_CLI_TIMEOUT,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    return result.stdout.strip(), elapsed_ms


# ── Main entry point ─────────────────────────────────────────────────────────

def call_llm(prompt: str) -> tuple[str, str | None, float, bool]:
    """Call LLM backend.

    Returns (action, reasoning, latency_ms, timed_out).
    """
    backend = "api" if AI_USE_API else ("warm" if AI_USE_WARM else "cli")
    logger.info("LLM call [backend=%s model=%s] prompt=%d chars",
                backend, AI_CLI_MODEL, len(prompt))
    try:
        if AI_USE_API:
            raw, elapsed_ms = _call_api(prompt)
        elif AI_USE_WARM:
            from llm_warm import get_warm_claude
            warm = get_warm_claude(model=AI_CLI_MODEL, timeout=AI_CLI_TIMEOUT)
            raw, elapsed_ms = warm.call(prompt)
        else:
            raw, elapsed_ms = _call_cli(prompt)

        logger.info("LLM response (%.0fms): %.200s", elapsed_ms, raw)
        action, reasoning = parse_llm_response(raw)
        return action, reasoning, elapsed_ms, False

    except subprocess.TimeoutExpired:
        elapsed_ms = AI_CLI_TIMEOUT * 1000
        logger.warning("LLM timed out after %.0fms — folding", elapsed_ms)
        return "fold", None, elapsed_ms, True

    except Exception as e:
        logger.error("LLM call failed: %s — folding", e)
        return "fold", None, 0, False
