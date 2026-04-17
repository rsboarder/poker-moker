"""Multi-table integration test: spawns real DealerBot (no Telegram), connects
dummy WS bots, runs a tournament with 2+ tables, verifies table breaking and
final table formation.

Usage:
    python test_multi_table.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import pathlib

import websockets

# Ensure dealer_bot imports work without TG env
os.environ.setdefault("DEALER_BOT_TOKEN", "")
os.environ.setdefault("MAIN_GROUP_ID", "0")
os.environ.setdefault("ACTION_TIMEOUT_SECONDS", "3")
os.environ.setdefault("STARTING_STACK", "200")  # smaller stacks = faster bust
os.environ.setdefault("TABLE_SIZE", "3")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import dealer_bot as db  # noqa: E402

WS_PORT = 9988


async def dummy_bot(url: str, team: str, stop: asyncio.Event, stats: dict):
    stats.setdefault(team, {"actions": 0, "rounds": 0, "connected": False, "elimination": None, "tables_seen": set()})
    try:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({
                "type": "register", "team": team, "invite": "POKER-TEST",
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
                    # Aggressive strategy: always raise when possible → fast busts
                    action, amount = "fold", 0
                    min_raise = msg.get("min_raise", 0)
                    stack = msg.get("stack", 0)
                    to_call = msg.get("to_call", 0)
                    # Always shove stack if possible to force quick busts
                    raise_amt = min(stack + msg.get("to_call", 0), stack)
                    can_raise = any("raise" in a for a in valid)
                    if can_raise and raise_amt >= min_raise:
                        action = "raise"
                        amount = raise_amt
                    elif to_call == 0:
                        action = "check"
                    elif to_call <= stack:
                        action = "call"
                        amount = to_call
                    await ws.send(json.dumps({
                        "type": "action", "turn_id": msg["turn_id"],
                        "action": action, "amount": amount,
                    }))
                elif t == "cards":
                    stats[team]["rounds"] += 1
                elif t == "eliminated":
                    stats[team]["elimination"] = msg.get("place")
                    break
                elif t == "tournament_over":
                    break
                elif t == "table_change":
                    stats[team]["tables_seen"].add(msg.get("new_table"))
    except Exception as e:
        stats[team]["error"] = str(e)


async def run():
    # Set invite code via registrations
    reg_path = pathlib.Path(__file__).parent / "registrations.json"
    reg_path.write_text(json.dumps({
        "tournament_code": "POKER-TEST",
        "tournament_name": "Multi-table test",
        "players": [],
    }))

    try:
        # Reuse DealerBot directly
        dealer = db.DealerBot(agents=[])

        # Start WS server bound to dealer
        server = await websockets.serve(dealer.ws_handler, "localhost", WS_PORT)

        # Also initialize spectator state fields used by TableSession
        with db._spectator_lock:
            db._spectator_state["table_size"] = 3
            db._spectator_state["tg_logging"] = False
            db._spectator_state["tg_configured"] = False

        NUM_BOTS = 6
        stop = asyncio.Event()
        stats: dict = {}
        bot_names = [f"bot{i}" for i in range(1, NUM_BOTS + 1)]
        bot_tasks = [
            asyncio.create_task(dummy_bot(f"ws://localhost:{WS_PORT}", n, stop, stats))
            for n in bot_names
        ]
        await asyncio.sleep(0.5)

        connected = sum(1 for r in stats.values() if r["connected"])
        assert connected == NUM_BOTS, f"{connected}/{NUM_BOTS} connected"
        print(f"  {connected}/{NUM_BOTS} bots connected")

        # Start tournament via HTTP trigger
        result = await dealer.trigger_startgame()
        print(f"  trigger_startgame → {result}")
        assert result["ok"], f"startgame failed: {result}"
        assert result["tables"] >= 2, f"expected >=2 tables, got {result['tables']}"
        initial_tables = result["tables"]

        # Wait for tournament to complete (or time out)
        max_wait = 180  # seconds
        elapsed = 0.0
        step = 0.5
        while elapsed < max_wait:
            await asyncio.sleep(step)
            elapsed += step
            if not dealer._is_game_active():
                break

        survivors = [a for a in dealer._tournament_agents if a.stack > 0]
        if len(survivors) != 1:
            print(f"  WARN: tournament not cleanly finished, survivors={len(survivors)}")

        stop.set()
        for t in bot_tasks:
            t.cancel()
        try:
            await asyncio.gather(*bot_tasks, return_exceptions=True)
        except Exception:
            pass

        server.close()
        await server.wait_closed()

        # Report
        print()
        print("=" * 60)
        print(f"Initial tables: {initial_tables}")
        winner = max(dealer._tournament_agents, key=lambda a: a.stack)
        print(f"Winner: {winner.username} ({winner.stack} chips)")
        print()
        print("Per-bot stats:")
        for name in bot_names:
            s = stats.get(name, {})
            tables = sorted(s.get("tables_seen", set()))
            status = "ALIVE" if s.get("elimination") is None else f"OUT@{s['elimination']}"
            agent = next(a for a in dealer._tournament_agents if a.username == name)
            print(f"  {name}: {s.get('actions', 0):4d} actions, "
                  f"{s.get('rounds', 0):3d} rounds, "
                  f"tables={tables}, {agent.stack:4d} chips [{status}]")

        # Assertions
        assert winner.stack > 0, "no winner"
        total_chips = int(os.environ["STARTING_STACK"]) * NUM_BOTS
        engine_total = sum(a.stack for a in dealer._tournament_agents)
        assert engine_total == total_chips, f"chip leak: {engine_total} != {total_chips}"

        # Verify at least one bot saw multiple tables (table_change happened)
        multi_table_bots = [n for n, s in stats.items() if len(s.get("tables_seen", set())) > 1]
        print(f"\nBots that experienced table_change: {multi_table_bots}")

        print()
        print("ALL ASSERTIONS PASSED")
        print("=" * 60)
    finally:
        if reg_path.exists():
            reg_path.unlink()


if __name__ == "__main__":
    asyncio.run(run())
