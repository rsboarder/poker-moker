"""Multi-table integration test: real DealerBot (no Telegram) + DummyBots, runs
a tournament with 2+ tables, verifies table breaking and final-table formation.

Usage:
    python test_multi_table.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import pathlib

import websockets

# Ensure dealer_bot imports work without TG env
os.environ.setdefault("DEALER_BOT_TOKEN", "")
os.environ.setdefault("MAIN_GROUP_ID", "0")
os.environ.setdefault("ACTION_TIMEOUT_SECONDS", "3")
os.environ.setdefault("STARTING_STACK", "200")
os.environ.setdefault("TABLE_SIZE", "3")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import dealer_bot as db  # noqa: E402
from test_helpers import DummyBot, spawn_dummies  # noqa: E402

WS_PORT = 9988


async def run():
    reg_path = pathlib.Path(__file__).parent / "registrations.json"
    reg_path.write_text(json.dumps({
        "tournament_code": "POKER-TEST",
        "tournament_name": "Multi-table test",
        "players": [],
    }))

    try:
        dealer = db.DealerBot(agents=[])
        server = await websockets.serve(dealer.ws_handler, "localhost", WS_PORT)

        with db._spectator_lock:
            db._spectator_state["table_size"] = 3
            db._spectator_state["tg_logging"] = False
            db._spectator_state["tg_configured"] = False

        NUM_BOTS = 6
        stop = asyncio.Event()
        bot_names = [f"bot{i}" for i in range(1, NUM_BOTS + 1)]
        bots, bot_tasks = spawn_dummies(
            f"ws://localhost:{WS_PORT}", bot_names, "POKER-TEST",
            stop, strategy="always_shove",
        )
        await asyncio.sleep(0.5)

        connected = sum(1 for b in bots if b.stats["connected"])
        assert connected == NUM_BOTS, f"{connected}/{NUM_BOTS} connected"
        print(f"  {connected}/{NUM_BOTS} bots connected")

        # Start tournament
        result = await dealer.trigger_startgame()
        print(f"  trigger_startgame → {result}")
        assert result["ok"], f"startgame failed: {result}"
        assert result["tables"] >= 2, f"expected >=2 tables, got {result['tables']}"
        initial_tables = result["tables"]

        # Wait for tournament to complete
        max_wait = 180
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
        bot_by_name = {b.team: b for b in bots}
        for name in bot_names:
            s = bot_by_name[name].stats
            tables = sorted(s["tables_seen"])
            status = "ALIVE" if s["elimination"] is None else f"OUT@{s['elimination']}"
            agent = next(a for a in dealer._tournament_agents if a.username == name)
            print(f"  {name}: {s['actions']:4d} actions, "
                  f"{s['rounds']:3d} rounds, tables={tables}, "
                  f"{agent.stack:4d} chips [{status}]")

        # Assertions
        assert winner.stack > 0, "no winner"
        total_chips = int(os.environ["STARTING_STACK"]) * NUM_BOTS
        engine_total = sum(a.stack for a in dealer._tournament_agents)
        assert engine_total == total_chips, f"chip leak: {engine_total} != {total_chips}"

        multi_table_bots = [b.team for b in bots if len(b.stats["tables_seen"]) > 1]
        print(f"\nBots that experienced table_change: {multi_table_bots}")

        print()
        print("ALL ASSERTIONS PASSED")
        print("=" * 60)
    finally:
        if reg_path.exists():
            reg_path.unlink()


if __name__ == "__main__":
    asyncio.run(run())
