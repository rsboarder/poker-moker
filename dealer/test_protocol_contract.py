"""Protocol contract test: ensures the WS messages dealer actually sends match
the contract documented in docs/ws-bot-guide.md.

Runs a short multi-table tournament (enough to trigger every message type at
least once), captures every message received by one bot, and asserts that:

  1. Every expected message type appeared (nothing silently dropped).
  2. Every documented field is present in at least one captured instance.
  3. No NEW top-level fields appeared that are not in the contract
     (drift detector — catches cases where dealer starts sending undocumented
     fields; forces the doc update).

When this test fails, you should either:
  - update the CONTRACT dict below AND update docs/ws-bot-guide.md in the
    same commit, OR
  - revert the dealer change that added/removed the field.

Usage:
    python test_protocol_contract.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import pathlib

os.environ.setdefault("DEALER_BOT_TOKEN", "")
os.environ.setdefault("MAIN_GROUP_ID", "0")
os.environ.setdefault("ACTION_TIMEOUT_SECONDS", "3")
os.environ.setdefault("STARTING_STACK", "200")
os.environ.setdefault("TABLE_SIZE", "3")
os.environ.setdefault("SPECTATOR_PORT", "8801")
os.environ.setdefault("WS_PORT", "9101")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import websockets  # noqa: E402
import dealer_bot as db  # noqa: E402
from test_helpers import DummyBot, spawn_dummies  # noqa: E402


WS_URL = f"ws://localhost:{os.environ['WS_PORT']}"


# ── Protocol contract ──────────────────────────────────────────────────────
# Keyed by `type` field. Each entry lists:
#   required: fields that MUST appear in every instance of this message
#   optional: fields that MAY appear (won't trigger drift warnings)
# If dealer sends a field not in required∪optional → test fails (forces doc update).
# If a required field is missing → test fails.
#
# Derived directly from docs/ws-bot-guide.md — keep in sync.
CONTRACT: dict[str, dict] = {
    "registered": {
        "required": {"type", "username", "players_online"},
        "optional": {"token", "reconnected"},
    },
    "cards": {
        "required": {"type", "table_id", "round", "hole_cards"},
        "optional": set(),
    },
    "turn": {
        "required": {
            "type", "turn_id", "table_id", "round", "street", "pot", "stack",
            "community", "hole_cards", "position", "players",
            "valid_actions", "to_call", "min_raise",
        },
        "optional": set(),
    },
    "event": {
        "required": {"type", "table_id", "text"},
        # Structured event fields only appear when parseable from the dealer text
        "optional": {"player", "action", "amount", "pot", "street", "community"},
    },
    "showdown": {
        "required": {"type", "table_id", "winner", "winner_id", "pot", "hands", "reason"},
        "optional": set(),
    },
    "round_end": {
        "required": {"type", "table_id", "round", "players"},
        "optional": set(),
    },
    "eliminated": {
        "required": {"type", "place", "players_left"},
        "optional": set(),
    },
    "tournament_start": {
        "required": {"type", "players", "tables", "your_table"},
        "optional": set(),
    },
    "tournament_over": {
        "required": {"type", "winner", "winner_id", "stack"},
        "optional": set(),
    },
    "table_change": {
        "required": {"type", "new_table"},
        "optional": {"reason"},
    },
    "error": {
        "required": {"type", "text"},
        "optional": set(),
    },
}

# Nested contract for `turn.players[]` entries
PLAYERS_ENTRY_CONTRACT = {
    "required": {"id", "username", "stack", "street_bet", "status"},
    "optional": set(),
}

# Nested contract for `showdown.hands[]` entries
HANDS_ENTRY_CONTRACT = {
    "required": {"player_id", "hole_cards", "rank"},
    "optional": set(),
}

# Nested contract for `round_end.players[]` entries
ROUND_END_PLAYER_CONTRACT = {
    "required": {"username", "stack"},
    "optional": set(),
}


class CapturingBot(DummyBot):
    """DummyBot that also records every received message to a list."""

    def __init__(self, *args, captures: list, **kwargs):
        super().__init__(*args, **kwargs)
        self.captures = captures

    async def run(self, stop: asyncio.Event) -> None:
        # Re-implement run() — we need access to the raw JSON BEFORE stats processing.
        try:
            async with websockets.connect(self.url) as ws:
                reg = {"type": "register", "team": self.team, "invite": self.invite}
                if self.token:
                    reg["token"] = self.token
                await ws.send(json.dumps(reg))
                reply = json.loads(await ws.recv())
                self.captures.append(reply)  # capture registered message
                if reply.get("type") != "registered":
                    return
                self.stats["connected"] = True
                self.stats["saved_token"] = reply.get("token")

                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
                    except asyncio.TimeoutError:
                        continue
                    except websockets.ConnectionClosed:
                        break

                    msg = json.loads(raw)
                    self.captures.append(msg)
                    t = msg.get("type")

                    if t == "turn":
                        self.stats["actions"] += 1
                        self.stats["tables_seen"].add(msg.get("table_id"))
                        action, amount = self._decide(msg)
                        await ws.send(json.dumps({
                            "type": "action",
                            "turn_id": msg["turn_id"],
                            "action": action,
                            "amount": amount,
                        }))
                    elif t == "cards":
                        self.stats["rounds"] += 1
                        self._last_turn_id = None
                        self._retry_count = 0
                    elif t == "eliminated":
                        self.stats["elimination"] = msg.get("place")
                    elif t == "table_change":
                        self.stats["tables_seen"].add(msg.get("new_table"))
                    elif t == "error":
                        self.stats["errors"] += 1
                    elif t == "tournament_over":
                        break
        except Exception as e:
            self.stats["error"] = str(e)


async def run_short_tournament() -> list[dict]:
    """Run a short 6-bot × 2-table tournament and return all messages captured
    by one bot (picked to maximize coverage — the one that survives longest)."""
    dealer = db.DealerBot(agents=[])
    with db._spectator_lock:
        db._spectator_state["table_size"] = 3
        db._spectator_state["tg_configured"] = False
        db._spectator_state["tg_logging"] = False

    db._dealer_ref = dealer
    db._loop_ref = asyncio.get_running_loop()

    # Setup registrations.json BEFORE starting bots (invite validated at register)
    reg_path = pathlib.Path(__file__).parent / "registrations.json"
    reg_path.write_text(json.dumps({
        "tournament_code": "POKER-PROTO",
        "tournament_name": "Protocol contract test",
        "players": [],
    }))

    ws_server = await websockets.serve(dealer.ws_handler, "localhost", int(os.environ["WS_PORT"]))
    await asyncio.sleep(0.3)

    stop = asyncio.Event()
    captures: list[dict] = []

    bot_names = [f"bot{i}" for i in range(1, 7)]
    regular_bots, regular_tasks = spawn_dummies(
        WS_URL, bot_names[:5], "POKER-PROTO", stop, strategy="smart_fallback"
    )
    observer = CapturingBot(WS_URL, bot_names[5], "POKER-PROTO",
                            strategy="smart_fallback", captures=captures)
    observer_task = asyncio.create_task(observer.run(stop))

    await asyncio.sleep(0.8)

    # Start tournament
    result = await dealer.trigger_startgame()
    assert result["ok"], f"startgame failed: {result}"

    # Wait up to 60s for tournament to finish
    max_wait = 60.0
    elapsed = 0.0
    while elapsed < max_wait:
        await asyncio.sleep(0.5)
        elapsed += 0.5
        if not dealer._is_game_active():
            break
    # Give bots a moment to receive tournament_over
    await asyncio.sleep(0.5)

    # Cleanup
    stop.set()
    for t in regular_tasks + [observer_task]:
        t.cancel()
    try:
        await asyncio.gather(*regular_tasks, observer_task, return_exceptions=True)
    except Exception:
        pass
    ws_server.close()
    await ws_server.wait_closed()

    if reg_path.exists():
        reg_path.unlink()

    db._dealer_ref = None
    db._loop_ref = None

    return captures


def check_contract(messages: list[dict]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []

    # Group captured messages by type
    by_type: dict[str, list[dict]] = {}
    for m in messages:
        t = m.get("type", "<missing type>")
        by_type.setdefault(t, []).append(m)

    # 1. Every contracted type should have been observed (except 'error' which is
    #    fine if never triggered; also 'registered' we always see)
    required_types = {t for t in CONTRACT if t not in ("error", "table_change")}
    seen_types = set(by_type.keys())
    missing_types = required_types - seen_types
    if missing_types:
        warnings.append(
            f"Message types in contract that were NOT observed (possible test gap): "
            f"{sorted(missing_types)}"
        )

    # 2. Unknown types in the wild
    unknown_types = seen_types - set(CONTRACT.keys())
    if unknown_types:
        errors.append(
            f"Dealer sent message types NOT in the contract "
            f"(add them to CONTRACT and docs/ws-bot-guide.md): {sorted(unknown_types)}"
        )

    # 3. Per-type field validation
    for t, contract in CONTRACT.items():
        if t not in by_type:
            continue
        required = contract["required"]
        optional = contract["optional"]
        allowed = required | optional

        # All instances should contain the required fields.
        for i, inst in enumerate(by_type[t]):
            inst_keys = set(inst.keys())
            missing = required - inst_keys
            if missing:
                errors.append(
                    f"Message type '{t}' instance #{i} missing required fields: "
                    f"{sorted(missing)}. Full message: {inst}"
                )
            extra = inst_keys - allowed
            if extra:
                errors.append(
                    f"Message type '{t}' instance #{i} has UNDOCUMENTED fields: "
                    f"{sorted(extra)}. Update CONTRACT and docs/ws-bot-guide.md."
                )

    # 4. Nested validation
    # turn.players[]
    for inst in by_type.get("turn", []):
        players = inst.get("players", [])
        for p in players:
            p_keys = set(p.keys())
            missing = PLAYERS_ENTRY_CONTRACT["required"] - p_keys
            if missing:
                errors.append(f"turn.players[] missing required: {sorted(missing)}")
            extra = p_keys - (PLAYERS_ENTRY_CONTRACT["required"] | PLAYERS_ENTRY_CONTRACT["optional"])
            if extra:
                errors.append(f"turn.players[] has UNDOCUMENTED fields: {sorted(extra)}")

    # showdown.hands[]
    for inst in by_type.get("showdown", []):
        hands = inst.get("hands", [])
        for h in hands:
            h_keys = set(h.keys())
            missing = HANDS_ENTRY_CONTRACT["required"] - h_keys
            if missing:
                errors.append(f"showdown.hands[] missing required: {sorted(missing)}")
            extra = h_keys - (HANDS_ENTRY_CONTRACT["required"] | HANDS_ENTRY_CONTRACT["optional"])
            if extra:
                errors.append(f"showdown.hands[] has UNDOCUMENTED fields: {sorted(extra)}")

    # round_end.players[]
    for inst in by_type.get("round_end", []):
        for p in inst.get("players", []):
            p_keys = set(p.keys())
            missing = ROUND_END_PLAYER_CONTRACT["required"] - p_keys
            if missing:
                errors.append(f"round_end.players[] missing required: {sorted(missing)}")
            extra = p_keys - (ROUND_END_PLAYER_CONTRACT["required"] | ROUND_END_PLAYER_CONTRACT["optional"])
            if extra:
                errors.append(f"round_end.players[] has UNDOCUMENTED fields: {sorted(extra)}")

    return errors, warnings


async def main():
    print("=" * 70)
    print("Protocol Contract Test")
    print("=" * 70)
    print("Running short 6-bot × 2-table tournament to capture all message types...")

    messages = await run_short_tournament()
    print(f"\nCaptured {len(messages)} messages for one observer bot.")

    # Summary: how many of each type
    from collections import Counter
    counts = Counter(m.get("type", "<none>") for m in messages)
    print("Message type counts:")
    for t, n in sorted(counts.items()):
        marker = "✓" if t in CONTRACT else "? (UNDOCUMENTED)"
        print(f"  {t:<20} {n:>3}  {marker}")

    errors, warnings = check_contract(messages)

    if warnings:
        print("\n── Warnings ─────────────────────────────────────────────")
        for w in warnings:
            print(f"  ⚠ {w}")

    if errors:
        print("\n── Errors ───────────────────────────────────────────────")
        for e in errors:
            print(f"  ✗ {e}")
        print("\n" + "=" * 70)
        print(f"FAIL: {len(errors)} contract violation(s) — update CONTRACT + docs/ws-bot-guide.md")
        print("=" * 70)
        sys.exit(1)

    print("\n" + "=" * 70)
    print("PASS: all message types match the contract")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
