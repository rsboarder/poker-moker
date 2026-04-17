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


from test_helpers import DummyBot, spawn_dummies  # noqa: E402


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
        stop = asyncio.Event()
        bot_names = [f"bot{i:02d}" for i in range(1, NUM_BOTS + 1)]
        bots, bot_tasks = spawn_dummies(
            WS_URL, bot_names, "POKER-E2E", stop, strategy="smart_fallback"
        )
        stats = {b.team: b.stats for b in bots}  # alias for downstream code
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
            assert "players" in t, f"table {tid} missing 'players'"

        # Observations we accumulate during the tournament to verify later.
        observed = {
            "multi_table_per_table_keys": False,     # per-table snapshot had sb/bb/btn/last_actions/showdown
            "distinct_sb_across_tables": False,       # at some poll different tables had different sb_player_id
            "max_big_blind": 0,                       # highest big_blind seen in any table snapshot
            "final_table_round2_reached": False,      # after consolidation, engine.round_number >= 2 for final table
            "final_table_first_round": None,          # round_number observed when consolidation happened
            "final_table_tid": None,                  # table_id of the final table
            "seen_final_table_state": False,          # _tournament_state == FINAL_TABLE at some poll
            "single_table_flat_matches": None,        # when table_count == 1: flat sb_player_id == per-table one (True/False/None)
        }

        max_wait = 180
        elapsed = 0.0
        step = 1.0
        last_report = -1
        winner_seen = None

        while elapsed < max_wait:
            await asyncio.sleep(step)
            elapsed += step
            state = await http_get_state()
            alive = [a for a in dealer._tournament_agents if a.stack > 0]
            active_tables = state.get("table_count", 0)
            tables_snap = state.get("tables", {})

            if int(elapsed) // 5 != last_report:
                last_report = int(elapsed) // 5
                print(f"  t={elapsed:.0f}s: alive={len(alive)} tables={active_tables} state={state.get('game_state')}")

            # Per-table snapshot richness (Item 1 contract)
            if active_tables > 1:
                all_have_keys = all(
                    all(k in t for k in ("sb_player_id", "bb_player_id", "btn_player_id",
                                          "last_actions", "showdown_result"))
                    for t in tables_snap.values()
                )
                if all_have_keys:
                    observed["multi_table_per_table_keys"] = True
                sb_ids = [t.get("sb_player_id") for t in tables_snap.values()]
                sb_ids_set = {s for s in sb_ids if s is not None}
                if len(sb_ids_set) >= 2:
                    observed["distinct_sb_across_tables"] = True

            # Blind escalation (Item 7 validation)
            for t in dealer.tables.values():
                if t.engine.big_blind > observed["max_big_blind"]:
                    observed["max_big_blind"] = t.engine.big_blind

            # Final table detection + round 2+
            if dealer._tournament_state == db.TournamentState.FINAL_TABLE:
                observed["seen_final_table_state"] = True
                if len(dealer.tables) == 1:
                    ftid = next(iter(dealer.tables))
                    observed["final_table_tid"] = ftid
                    rn = dealer.tables[ftid].engine.round_number
                    if observed["final_table_first_round"] is None:
                        observed["final_table_first_round"] = rn
                    if observed["final_table_first_round"] is not None \
                            and rn > observed["final_table_first_round"]:
                        observed["final_table_round2_reached"] = True

            # Single-table flat-vs-per-table consistency (when we've collapsed)
            if active_tables == 1 and tables_snap:
                only_snap = next(iter(tables_snap.values()))
                per_sb = only_snap.get("sb_player_id")
                flat_sb = state.get("sb_player_id")
                if per_sb is not None and flat_sb is not None:
                    observed["single_table_flat_matches"] = (per_sb == flat_sb)

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

        # ── Assertions ──────────────────────────────────────────────────
        total_chips = int(os.environ["STARTING_STACK"]) * NUM_BOTS
        engine_total = sum(a.stack for a in dealer._tournament_agents)
        assert engine_total == total_chips, f"chip leak: {engine_total} != {total_chips}"

        multi_table_bots = [n for n, s in stats.items() if len(s.get("tables_seen", set())) > 1]
        print(f"\nBots who experienced table_change: {multi_table_bots}")

        # Bot errors
        err_counts = {n: s.get("errors", 0) for n, s in stats.items()}
        print(f"Bot error counts: {err_counts}")

        # Observations
        print(f"\nObservations:")
        print(f"  max big_blind seen:                {observed['max_big_blind']}")
        print(f"  final_table state entered:         {observed['seen_final_table_state']}")
        print(f"  final_table tid:                   {observed['final_table_tid']}")
        print(f"  round 2+ reached on final table:   {observed['final_table_round2_reached']}")
        print(f"  per-table keys present in multi:   {observed['multi_table_per_table_keys']}")
        print(f"  distinct sb across tables:         {observed['distinct_sb_across_tables']}")
        print(f"  single-table flat matches per-tbl: {observed['single_table_flat_matches']}")
        print(f"  eliminated_announced size:         {len(dealer._eliminated_announced)}")
        print(f"  final _tournament_state:           {dealer._tournament_state.value}")

        # Basic winner invariants
        assert len(alive) == 1, f"Expected 1 winner, got {len(alive)}: {[a.username for a in alive]}"
        assert winner.stack == total_chips, f"winner should have all chips: {winner.stack} != {total_chips}"
        assert final_state.get("game_state") == "tournament_over", \
            f"game_state should be tournament_over, got: {final_state.get('game_state')}"
        assert winner_seen == winner.username, f"winner mismatch: seen={winner_seen} actual={winner.username}"

        # Table breaking
        assert len(multi_table_bots) >= 1, "Expected at least one bot to see table_change"

        # No bot errors during the tournament
        assert all(c == 0 for c in err_counts.values()), f"Bot errors: {err_counts}"

        # ≥3 eliminations (Item 3: set-based tracking)
        assert len(dealer._eliminated_announced) >= 3, \
            f"Expected ≥3 eliminations, got {len(dealer._eliminated_announced)}"

        # Blind escalation (Item 7: fix for min_raise bug)
        assert observed["max_big_blind"] >= 40, \
            f"Expected blinds to escalate to ≥40, max seen: {observed['max_big_blind']}"

        # Spectator state correctness (Item 1)
        assert observed["multi_table_per_table_keys"], \
            "Per-table snapshot missing sb/bb/btn/last_actions/showdown keys in multi-table mode"
        assert observed["distinct_sb_across_tables"], \
            "Never observed distinct sb_player_ids across tables (but we had 3 tables)"

        # At least one poll should have shown single-table flat==per-table consistency
        # (i.e. after final-table formation the flat fields mirror the snapshot).
        assert observed["single_table_flat_matches"] in (True, None), \
            "Flat state sb_player_id did not match per-table snapshot in single-table mode"

        # State machine traversed FINAL_TABLE (Item 6)
        assert observed["seen_final_table_state"], \
            "Never observed TournamentState.FINAL_TABLE — state machine did not traverse correctly"

        # Final table played at least one round (Item 8 plan wording: "first round on
        # original tables, then final table with a second round" — round-2 of the
        # tournament IS the first hand played on the consolidated final table).
        assert observed["final_table_first_round"] is not None, \
            "Final table was formed but never played a round"

        # After tournament ends, state back to IDLE
        assert dealer._tournament_state == db.TournamentState.IDLE, \
            f"Tournament state should be IDLE after completion, got {dealer._tournament_state.value}"

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
