"""Local simulator — test bot decisions without Telegram."""

import sys
from collections import deque

from hand_tiers import get_hand_tier
from llm_engine import TIER_LABELS
from equity import calculate_equity, calculate_pot_odds, categorize_equity
from opponent_tracker import OpponentTracker
from storage import GameStorage
from strategy import make_decision

storage = GameStorage()
tracker = OpponentTracker(storage, "test_bot")

# ── Predefined scenarios ─────────────────────────────────────────────────────

SCENARIOS = [
    {
        "name": "Premium preflop — AA on BTN",
        "hole": ["Ah", "As"],
        "community": [],
        "street": "preflop",
        "pot": 30,
        "stack": 1000,
        "position": "BTN",
        "valid": "Valid: /fold /call /raise (min: 40)",
    },
    {
        "name": "Trash preflop — 72o UTG",
        "hole": ["7h", "2d"],
        "community": [],
        "street": "preflop",
        "pot": 30,
        "stack": 1000,
        "position": "UTG",
        "valid": "Valid: /fold /call /raise (min: 40)",
    },
    {
        "name": "Strong flop — top pair top kicker",
        "hole": ["Ah", "Kd"],
        "community": ["Ac", "7d", "3s"],
        "street": "flop",
        "pot": 120,
        "stack": 880,
        "position": "CO",
        "valid": "Valid: /fold /check /raise (min: 60)",
    },
    {
        "name": "Marginal flop — middle pair",
        "hole": ["Ts", "9s"],
        "community": ["Kh", "Td", "4c"],
        "street": "flop",
        "pot": 200,
        "stack": 800,
        "position": "BTN",
        "valid": "Valid: /fold /call 80 /raise (min: 160)",
        "expected_layer": "llm or equity",
    },
    {
        "name": "River bluff spot — missed flush draw",
        "hole": ["Jh", "Th"],
        "community": ["2h", "5h", "Kc", "3d", "8s"],
        "street": "river",
        "pot": 400,
        "stack": 600,
        "position": "BTN",
        "valid": "Valid: /fold /check /raise (min: 100)",
    },
    {
        "name": "Short stack push-or-fold",
        "hole": ["Qs", "Jd"],
        "community": [],
        "street": "preflop",
        "pot": 30,
        "stack": 180,
        "position": "CO",
        "valid": "Valid: /fold /call /raise (min: 40)",
    },
    {
        "name": "Nuts on river — full house",
        "hole": ["Kh", "Kd"],
        "community": ["Kc", "7d", "7s", "2h", "9c"],
        "street": "river",
        "pot": 500,
        "stack": 500,
        "position": "MP",
        "valid": "Valid: /fold /check /raise (min: 100)",
    },
    {
        "name": "Free flop from BB",
        "hole": ["6d", "3c"],
        "community": [],
        "street": "preflop",
        "pot": 20,
        "stack": 980,
        "position": "BB",
        "valid": "Valid: /fold /check /raise (min: 40)",
    },
]


def run_scenario(scenario: dict, index: int):
    print(f"\n{'='*60}")
    print(f"Scenario {index + 1}: {scenario['name']}")
    print(f"{'='*60}")

    hole = scenario["hole"]
    community = scenario["community"]
    street = scenario["street"]
    pot = scenario["pot"]
    stk = scenario["stack"]
    pos = scenario["position"]
    valid = scenario["valid"]

    tier = get_hand_tier(hole[0], hole[1])
    eq = calculate_equity(hole, community)
    to_call_str = ""
    import re
    m = re.search(r"call\s*(\d+)?", valid)
    to_call = int(m.group(1)) if m and m.group(1) else 0
    pot_odds = calculate_pot_odds(to_call, pot)

    print(f"  Cards:     {' '.join(hole)} | Board: {' '.join(community) or '—'}")
    print(f"  Street:    {street} | Pot: {pot} | Stack: {stk} | Position: {pos}")
    print(f"  Tier:      {tier} ({TIER_LABELS.get(tier, '?')})")
    print(f"  Equity:    {eq:.1%} ({categorize_equity(eq)})")
    print(f"  Pot odds:  {pot_odds:.1%} (to call: {to_call})")
    print(f"  Valid:     {valid}")
    print()

    tracker.reset_round()
    action = make_decision(
        hole_cards=hole,
        community_cards=community,
        street=street,
        pot=pot,
        stack=stk,
        position=pos,
        valid_line=valid,
        opponent_tracker=tracker,
        storage=storage,
        round_num=f"test_{index + 1}",
    )

    print(f"  >>> DECISION: /{action}")
    print()


def interactive_mode():
    print("\n" + "="*60)
    print("INTERACTIVE MODE — enter your own scenario")
    print("="*60)
    print("Enter cards as: Ah Kd (rank + suit)")
    print("Ranks: 2-9, T, J, Q, K, A")
    print("Suits: h(hearts) d(diamonds) c(clubs) s(spades)")
    print()

    while True:
        try:
            raw = input("Hole cards (e.g. Ah Kd) or 'q' to quit: ").strip()
            if raw.lower() == 'q':
                break
            parts = raw.split()
            if len(parts) != 2:
                print("  Need exactly 2 cards")
                continue
            hole = parts

            raw = input("Community cards (e.g. 2h 7d Jc) or empty: ").strip()
            community = raw.split() if raw else []

            street = "preflop" if not community else (
                "flop" if len(community) == 3 else
                "turn" if len(community) == 4 else "river"
            )

            pot = int(input(f"Pot size [{200}]: ").strip() or 200)
            stk = int(input(f"Your stack [{1000}]: ").strip() or 1000)
            pos = input(f"Position [BTN]: ").strip().upper() or "BTN"
            to_call = int(input(f"To call [{0}]: ").strip() or 0)

            if to_call > 0:
                min_raise = to_call * 2
                valid = f"Valid: /fold /call {to_call} /raise (min: {min_raise})"
            else:
                valid = "Valid: /fold /check /raise (min: 40)"

            tier = get_hand_tier(hole[0], hole[1])
            eq = calculate_equity(hole, community)
            pot_odds = calculate_pot_odds(to_call, pot)

            print(f"\n  Tier: {tier} ({TIER_LABELS.get(tier, '?')}) | "
                  f"Equity: {eq:.1%} | Pot odds: {pot_odds:.1%}")

            tracker.reset_round()
            action = make_decision(
                hole_cards=hole,
                community_cards=community,
                street=street,
                pot=pot,
                stack=stk,
                position=pos,
                valid_line=valid,
                opponent_tracker=tracker,
                storage=storage,
                round_num="interactive",
            )
            print(f"  >>> DECISION: /{action}\n")

        except (KeyboardInterrupt, EOFError):
            break
        except Exception as e:
            print(f"  Error: {e}\n")


def main():
    print("Poker Bot — Local Test")
    print("=" * 60)

    if "--interactive" in sys.argv or "-i" in sys.argv:
        interactive_mode()
    else:
        for i, scenario in enumerate(SCENARIOS):
            run_scenario(scenario, i)

        print("\n" + "="*60)
        print("Run with --interactive (-i) for custom scenarios")
        print("="*60)

    storage.close()


if __name__ == "__main__":
    main()
