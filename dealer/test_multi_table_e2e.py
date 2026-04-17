"""Full E2E multi-table test: real dealer, real HTTP endpoints, real WS bots.

Spawns the actual DealerBot with HTTP + WS server (no Telegram),
connects 9 dummy bots, triggers tournament via HTTP /startgame,
monitors /state endpoint, verifies:

  - Multiple tables created (table_count > 1)
  - state["tables"] dict populated correctly
  - Eliminations happen, table breaking triggers
  - Final table forms
  - Chip conservation
  - Winner reported in state["winner"]

Usage:
    python test_multi_table_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import urllib.request
import pathlib

os.environ.setdefault("DEALER_BOT_TOKEN", "")
os.environ.setdefault("MAIN_GROUP_ID", "0")
os.environ.setdefault("ACTION_TIMEOUT_SECONDS", "3")
os.environ.setdefault("STARTING_STACK", "200")
os.environ.setdefault("TABLE_SIZE", "3")
os.environ.setdefault("SPECTATOR_PORT", "8799")
os.environ.setdefault("WS_PORT", "9099")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import websockets  # noqa: E402
import dealer_bot as db  # noqa: E402


WS_URL = f"ws://localhost:{os.environ['WS_PORT']}"
HTTP_URL = f"http://localhost:{os.environ['SPECTATOR_PORT']}"


def _http_get_state_sync() -> dict:
    with urllib.request.urlopen(f"{HTTP_URL}/state", timeout=2) as r:
        return json.loads(r.read())


def _http_post_sync(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{HTTP_URL}{path}",
        method="POST",
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


async def http_get_state() -> dict:
    return await asyncio.to_thread(_http_get_state_sync)


async def http_post(path: str, body: dict | None = None) -> dict:
    return await asyncio.to_thread(_http_post_sync, path, body)


async def dummy_bot(team: str, stop: asyncio.Event, stats: dict):
    stats[team] = {"actions": 0, "rounds": 0, "tables_seen": set(), "connected": False, "elimination": None, "errors": 0}
    last_turn_id = None
    retry_count = 0
    try:
        async with websockets.connect(WS_URL) as ws:
            await ws.send(json.dumps({
                "type": "register", "team": team, "invite": "POKER-E2E",
            }))
            reply = json.loads(await ws.recv())
            if reply.get("type") != "registered":
                return
            stats[team]["connected"] = True

            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    break
                msg = json.loads(raw)
                t = msg.get("type")

                if t == "turn":
                    stats[team]["actions"] += 1
                    stats[team]["tables_seen"].add(msg.get("table_id"))
                    valid = msg.get("valid_actions", [])
                    stack = msg.get("stack", 0)
                    to_call = msg.get("to_call", 0)
                    min_raise = msg.get("min_raise", 0)
                    turn_id = msg["turn_id"]

                    # If we're spamming retries on the same turn, fall back
                    # Note: dealer increments turn_id on each re-prompt, so detect
                    # retries by tracking prior turn_ids we've answered for.
                    if last_turn_id is not None and turn_id == last_turn_id + 1:
                        retry_count += 1
                    else:
                        retry_count = 0
                    last_turn_id = turn_id

                    # Aggressive: shove all-in when possible
                    raise_amt = stack + to_call
                    can_raise = any("raise" in a for a in valid)

                    if retry_count >= 2:
                        # Fallback: plain call/check/fold
                        if to_call == 0:
                            action, amount = "check", 0
                        elif to_call <= stack:
                            action, amount = "call", to_call
                        else:
                            action, amount = "fold", 0
                    elif can_raise and raise_amt >= min_raise:
                        action, amount = "raise", raise_amt
                    elif to_call == 0:
                        action, amount = "check", 0
                    elif to_call <= stack:
                        action, amount = "call", to_call
                    else:
                        action, amount = "fold", 0

                    await ws.send(json.dumps({
                        "type": "action", "turn_id": turn_id,
                        "action": action, "amount": amount,
                    }))
                elif t == "cards":
                    stats[team]["rounds"] += 1
                    last_turn_id = None
                    retry_count = 0
                elif t == "eliminated":
                    stats[team]["elimination"] = msg.get("place")
                elif t == "table_change":
                    stats[team]["tables_seen"].add(msg.get("new_table"))
                elif t == "error":
                    stats[team]["errors"] += 1
                elif t == "tournament_over":
                    break
    except Exception as e:
        stats[team]["error"] = str(e)


async def run():
    # Setup tournament code
    reg_path = pathlib.Path(__file__).parent / "registrations.json"
    reg_path.write_text(json.dumps({
        "tournament_code": "POKER-E2E",
        "tournament_name": "Multi-table E2E",
        "players": [],
    }))

    try:
        # Start the dealer via its own async_main path — but we need a more isolated setup
        # since the real async_main tries to build a TG Application. We'll instantiate
        # the pieces manually.
        dealer = db.DealerBot(agents=[])

        # Initialize spectator state
        with db._spectator_lock:
            db._spectator_state["table_size"] = int(os.environ["TABLE_SIZE"])
            db._spectator_state["tg_configured"] = False
            db._spectator_state["tg_logging"] = False

        # Set module-level refs so HTTP handler can reach the dealer
        db._dealer_ref = dealer
        db._loop_ref = asyncio.get_running_loop()

        # Start the spectator HTTP server (runs in its own thread)
        db._start_spectator_server()

        # Start WS server
        ws_server = await websockets.serve(dealer.ws_handler, "localhost", int(os.environ["WS_PORT"]))

        # Give server a moment
        await asyncio.sleep(0.3)

        # Check initial state via HTTP
        state = await http_get_state()
        print(f"  Initial state: game_state={state['game_state']} table_count={state.get('table_count', 0)} table_size={state.get('table_size')}")
        assert state["game_state"] == "waiting"
        assert state["table_count"] == 0

        # Connect 9 bots (expect 3 tables × 3)
        NUM_BOTS = 9
        stats: dict = {}
        stop = asyncio.Event()
        bot_names = [f"bot{i:02d}" for i in range(1, NUM_BOTS + 1)]
        bot_tasks = [
            asyncio.create_task(dummy_bot(n, stop, stats))
            for n in bot_names
        ]
        await asyncio.sleep(0.8)

        connected = sum(1 for s in stats.values() if s["connected"])
        print(f"  {connected}/{NUM_BOTS} bots connected")
        assert connected == NUM_BOTS

        state = await http_get_state()
        assert len(state["ws_players"]) == NUM_BOTS

        # Start game via HTTP
        start_result = await http_post("/startgame")
        print(f"  startgame → ok={start_result['ok']} tables={start_result.get('tables')} size={start_result.get('table_size')}")
        assert start_result["ok"]
        assert start_result["tables"] == 3
        assert start_result["table_size"] == 3

        # Wait for first state update with multi-table info
        await asyncio.sleep(0.5)
        state = await http_get_state()
        print(f"  After start: table_count={state['table_count']}, tables present: {sorted(state.get('tables', {}).keys())}")
        assert state["table_count"] == 3
        assert len(state["tables"]) == 3

        # Verify each table entry has expected fields
        for tid, t in state["tables"].items():
            # Initial state may just have {players: [...]} or full snapshot
            assert "players" in t, f"table {tid} missing 'players'"

        # Monitor tournament progression
        max_wait = 180
        elapsed = 0.0
        step = 2.0
        last_report = -1
        winner_seen = None

        while elapsed < max_wait:
            await asyncio.sleep(step)
            elapsed += step
            state = await http_get_state()
            alive = [a for a in dealer._tournament_agents if a.stack > 0]
            active_tables = state.get("table_count", 0)

            if int(elapsed) // 5 != last_report:
                last_report = int(elapsed) // 5
                print(f"  t={elapsed:.0f}s: alive={len(alive)} tables={active_tables} state={state.get('game_state')}")

            if state.get("game_state") == "tournament_over":
                winner_seen = state.get("winner")
                break
            if not dealer._is_game_active() and len(alive) <= 1:
                break

        stop.set()
        for t in bot_tasks:
            t.cancel()
        try:
            await asyncio.gather(*bot_tasks, return_exceptions=True)
        except Exception:
            pass

        # Final state check
        final_state = await http_get_state()
        alive = [a for a in dealer._tournament_agents if a.stack > 0]
        winner = max(dealer._tournament_agents, key=lambda a: a.stack)

        # Cleanup
        ws_server.close()
        await ws_server.wait_closed()

        # Report
        print()
        print("=" * 70)
        print(f"Final state:    game_state={final_state.get('game_state')}, winner={final_state.get('winner')}")
        print(f"Winner:         @{winner.username} ({winner.stack} chips)")
        print(f"Survivors:      {len(alive)}")
        print()
        print("Per-bot:")
        for name in bot_names:
            s = stats.get(name, {})
            tables = sorted(s.get("tables_seen", set()))
            agent = next(a for a in dealer._tournament_agents if a.username == name)
            status = "ALIVE" if agent.stack > 0 else f"OUT@{s.get('elimination', '?')}"
            print(f"  {name}: actions={s.get('actions', 0):3d} rounds={s.get('rounds', 0):2d} "
                  f"tables={tables} stack={agent.stack:4d} [{status}]")

        # Assertions
        total_chips = int(os.environ["STARTING_STACK"]) * NUM_BOTS
        engine_total = sum(a.stack for a in dealer._tournament_agents)
        assert engine_total == total_chips, f"chip leak: {engine_total} != {total_chips}"

        multi_table_bots = [n for n, s in stats.items() if len(s.get("tables_seen", set())) > 1]
        print(f"\nBots who experienced table_change: {multi_table_bots}")

        # Expect exactly 1 winner
        assert len(alive) == 1, f"Expected 1 winner, got {len(alive)}: {[a.username for a in alive]}"
        assert winner.stack == total_chips, f"winner should have all chips: {winner.stack} != {total_chips}"

        # Tournament should have finished cleanly
        assert final_state.get("game_state") == "tournament_over", \
            f"game_state should be tournament_over, got: {final_state.get('game_state')}"
        assert winner_seen == winner.username, f"winner mismatch: seen={winner_seen} actual={winner.username}"

        # Table breaking must have happened (9 → 1 requires multi-table collapse)
        assert len(multi_table_bots) >= 1, "Expected at least one bot to see table_change"

        print()
        print("ALL ASSERTIONS PASSED")
        print("=" * 70)
    finally:
        if reg_path.exists():
            reg_path.unlink()
        db._dealer_ref = None
        db._loop_ref = None


if __name__ == "__main__":
    asyncio.run(run())
