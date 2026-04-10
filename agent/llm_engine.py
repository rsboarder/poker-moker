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
Street: {street} | Pot: {pot} | Your stack: {stack}
Equity vs opponent range: {equity:.1%}
Pot odds to call: {pot_odds:.1%}
Equity category: {equity_category}
Position: {position}
</game_state>

<opponent_stats>
{opponent_stats}
</opponent_stats>

<valid_actions>
{valid_line}
</valid_actions>

<instructions>
Think step by step inside <think> tags, then give your final action inside <answer> tags.

In your analysis, consider:
1. Your hand strength (tier {hand_tier}) and how it connects with the board
2. Equity ({equity:.1%}) vs pot odds ({pot_odds:.1%}) — is calling profitable?
3. Opponent tendencies — are they loose/tight, passive/aggressive?
4. Position — do you act last (advantage) or first?
5. Stack-to-pot ratio — how committed are you?

IMPORTANT: Inside <answer> tags, write ONLY the action. No explanation.
Valid formats: fold | check | call | raise <amount>
Example: <answer>raise 150</answer>
</instructions>
"""

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
) -> str:
    return PROMPT_TEMPLATE.format(
        hole_cards=" ".join(hole_cards) if hole_cards else "unknown",
        community=" ".join(community_cards) if community_cards else "none (preflop)",
        street=street,
        pot=pot,
        stack=stack,
        equity=equity,
        pot_odds=pot_odds,
        equity_category=equity_category,
        hand_tier=hand_tier,
        tier_label=TIER_LABELS.get(hand_tier, "unknown"),
        position=position or "unknown",
        opponent_stats=opponent_stats,
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
