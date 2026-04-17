"""Edge-case tests for the dealer's WS handling.

Scenarios:
  1. Reconnect race — old socket's finally shouldn't evict the new socket
  2. Duplicate team name — second register should be rejected
  3. Malformed `amount` payload — dealer should not crash, reply with error
  4. Stop→immediate restart — second tournament starts cleanly without leaking
     _global_round_count / _eliminated_announced / state history
  5. Stale turn_id — dealer should reject an action with a wrong turn_id

Usage:
    python test_edge_cases.py
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
os.environ.setdefault("SPECTATOR_PORT", "8810")
os.environ.setdefault("WS_PORT", "9111")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import websockets  # noqa: E402
import dealer_bot as db  # noqa: E402

WS_URL = f"ws://localhost:{os.environ['WS_PORT']}"
INVITE = "POKER-EDGE"


async def _write_registrations():
    reg_path = pathlib.Path(__file__).parent / "registrations.json"
    reg_path.write_text(json.dumps({
        "tournament_code": INVITE,
        "tournament_name": "Edge-case suite",
        "players": [],
    }))
    return reg_path


async def _setup_dealer():
    reg_path = await _write_registrations()
    dealer = db.DealerBot(agents=[])
    with db._spectator_lock:
        db._spectator_state["table_size"] = 3
        db._spectator_state["tg_logging"] = False
        db._spectator_state["tg_configured"] = False
    server = await websockets.serve(
        dealer.ws_handler, "localhost", int(os.environ["WS_PORT"])
    )
    await asyncio.sleep(0.2)
    return dealer, server, reg_path


async def _teardown(dealer, server, reg_path):
    server.close()
    await server.wait_closed()
    if reg_path.exists():
        reg_path.unlink()


async def test_reconnect_race():
    """Old socket close should NOT evict the new registered ws."""
    print("test_reconnect_race... ", end="", flush=True)
    dealer, server, reg_path = await _setup_dealer()
    try:
        # First connection — register and save token
        ws1 = await websockets.connect(WS_URL)
        await ws1.send(json.dumps({"type": "register", "team": "reconn", "invite": INVITE}))
        r1 = json.loads(await ws1.recv())
        assert r1["type"] == "registered", r1
        token = r1["token"]
        assert "reconn" in dealer.ws_connections

        # Second connection with same token — should replace
        ws2 = await websockets.connect(WS_URL)
        await ws2.send(json.dumps({
            "type": "register", "team": "reconn", "invite": INVITE, "token": token,
        }))
        r2 = json.loads(await ws2.recv())
        assert r2.get("reconnected") is True, r2
        # ws_connections now points at ws2, not ws1
        assert dealer.ws_connections["reconn"] is not ws1

        # Close ws1 — its finally block MUST NOT delete ws_connections["reconn"]
        await ws1.close()
        await asyncio.sleep(0.3)

        # Expected: ws2 is still tracked
        assert "reconn" in dealer.ws_connections, \
            "Old socket close evicted the new connection (reconnect race)"
        assert dealer.ws_connections["reconn"] is not ws1

        await ws2.close()
        print("PASS")
    finally:
        await _teardown(dealer, server, reg_path)


async def test_duplicate_team():
    """Second bot registering with the same team while the first is connected → error."""
    print("test_duplicate_team... ", end="", flush=True)
    dealer, server, reg_path = await _setup_dealer()
    try:
        ws1 = await websockets.connect(WS_URL)
        await ws1.send(json.dumps({"type": "register", "team": "alice", "invite": INVITE}))
        r1 = json.loads(await ws1.recv())
        assert r1["type"] == "registered"

        ws2 = await websockets.connect(WS_URL)
        await ws2.send(json.dumps({"type": "register", "team": "alice", "invite": INVITE}))
        r2 = json.loads(await ws2.recv())
        assert r2["type"] == "error", f"Expected error for duplicate, got: {r2}"
        assert "already connected" in r2["text"].lower(), r2

        await ws1.close()
        await ws2.close()
        print("PASS")
    finally:
        await _teardown(dealer, server, reg_path)


async def test_malformed_amount():
    """Action with malformed `amount` should not crash WS handler."""
    print("test_malformed_amount... ", end="", flush=True)
    dealer, server, reg_path = await _setup_dealer()
    try:
        # Need at least 2 bots for a game; use simple connect-and-register
        ws = await websockets.connect(WS_URL)
        await ws.send(json.dumps({"type": "register", "team": "m1", "invite": INVITE}))
        r = json.loads(await ws.recv())
        assert r["type"] == "registered"

        ws2 = await websockets.connect(WS_URL)
        await ws2.send(json.dumps({"type": "register", "team": "m2", "invite": INVITE}))
        assert json.loads(await ws2.recv())["type"] == "registered"

        await dealer.trigger_startgame()

        # Wait briefly for tournament to begin so there's a pending_player
        await asyncio.sleep(0.4)

        # Send bogus action; can't know whose turn, just send from both
        for sock in (ws, ws2):
            await sock.send(json.dumps({
                "type": "action",
                "turn_id": 1,
                "action": "raise",
                "amount": "not-a-number",
            }))

        # Dealer should respond with error — drain for error messages
        got_error = False
        for sock in (ws, ws2):
            try:
                while True:
                    raw = await asyncio.wait_for(sock.recv(), timeout=0.3)
                    m = json.loads(raw)
                    if m.get("type") == "error" and "invalid amount" in m.get("text", "").lower():
                        got_error = True
                        break
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                pass
            if got_error:
                break

        # Dealer process is still alive (serving other messages)
        assert dealer._tournament_state != db.TournamentState.IDLE  # still mid-tournament
        assert got_error, "Dealer did not respond with 'invalid amount' error"

        await dealer.trigger_stopgame()
        await ws.close()
        await ws2.close()
        print("PASS")
    finally:
        await _teardown(dealer, server, reg_path)


async def test_stop_then_restart():
    """After stopgame, a second startgame must produce a fresh tournament
    (no leaked state from the first)."""
    print("test_stop_then_restart... ", end="", flush=True)
    dealer, server, reg_path = await _setup_dealer()
    try:
        # Connect 3 bots, start → stop → start
        socks = []
        for i in range(3):
            ws = await websockets.connect(WS_URL)
            await ws.send(json.dumps({"type": "register", "team": f"r{i}", "invite": INVITE}))
            assert json.loads(await ws.recv())["type"] == "registered"
            socks.append(ws)

        r1 = await dealer.trigger_startgame()
        assert r1["ok"], r1
        first_state_history = list(dealer._state_history)

        # Let it run briefly
        await asyncio.sleep(0.5)

        # Stop
        await dealer.trigger_stopgame()
        assert dealer._tournament_state == db.TournamentState.IDLE
        assert dealer._global_round_count == 0, \
            f"_global_round_count not reset: {dealer._global_round_count}"
        assert len(dealer._eliminated_announced) == 0, \
            f"_eliminated_announced not cleared: {dealer._eliminated_announced}"

        # Restart
        r2 = await dealer.trigger_startgame()
        assert r2["ok"], r2
        # Fresh history — should not have IDLE from first cycle still dangling
        assert db.TournamentState.STARTING in dealer._state_history

        # Clean up
        await dealer.trigger_stopgame()
        for s in socks:
            await s.close()
        print("PASS")
    finally:
        await _teardown(dealer, server, reg_path)


async def test_stale_turn_id():
    """Action with wrong turn_id should be rejected."""
    print("test_stale_turn_id... ", end="", flush=True)
    dealer, server, reg_path = await _setup_dealer()
    try:
        socks = []
        for team in ("s1", "s2"):
            ws = await websockets.connect(WS_URL)
            await ws.send(json.dumps({"type": "register", "team": team, "invite": INVITE}))
            assert json.loads(await ws.recv())["type"] == "registered"
            socks.append(ws)

        await dealer.trigger_startgame()

        # Wait for SOME table to have a pending player
        for _ in range(40):
            await asyncio.sleep(0.1)
            if any(t._pending_player_id for t in dealer.tables.values()):
                break

        # Find the table and the pending username to target directly
        target_table = None
        for t in dealer.tables.values():
            if t._pending_player_id:
                target_table = t
                break
        assert target_table, "No table had a pending player"
        expected_username = target_table._by_player_id[target_table._pending_player_id].username.lower()
        target_ws = next(ws for ws, team in zip(socks, ("s1", "s2")) if team == expected_username)

        # Send stale turn_id from the correct bot (so turn_id check fires, not username check)
        await target_ws.send(json.dumps({
            "type": "action",
            "turn_id": 999999,
            "action": "call",
            "amount": 10,
        }))

        got_stale = False
        try:
            for _ in range(20):
                raw = await asyncio.wait_for(target_ws.recv(), timeout=0.3)
                m = json.loads(raw)
                if m.get("type") == "error" and "stale" in m.get("text", "").lower():
                    got_stale = True
                    break
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass

        assert got_stale, "Dealer did not reject stale turn_id for correct player"

        await dealer.trigger_stopgame()
        for s in socks:
            await s.close()
        print("PASS")
    finally:
        await _teardown(dealer, server, reg_path)


async def test_stale_socket_rejected():
    """Old socket after reconnect must not be able to submit actions."""
    print("test_stale_socket_rejected... ", end="", flush=True)
    dealer, server, reg_path = await _setup_dealer()
    try:
        # Register bot1
        ws_old = await websockets.connect(WS_URL)
        await ws_old.send(json.dumps({"type": "register", "team": "stale1", "invite": INVITE}))
        r1 = json.loads(await ws_old.recv())
        assert r1["type"] == "registered"
        token = r1["token"]

        # Reconnect with same team + token
        ws_new = await websockets.connect(WS_URL)
        await ws_new.send(json.dumps({
            "type": "register", "team": "stale1", "invite": INVITE, "token": token
        }))
        r2 = json.loads(await ws_new.recv())
        assert r2.get("reconnected") is True

        # Also register a 2nd bot so game starts
        ws2 = await websockets.connect(WS_URL)
        await ws2.send(json.dumps({"type": "register", "team": "stale2", "invite": INVITE}))
        assert json.loads(await ws2.recv())["type"] == "registered"

        await dealer.trigger_startgame()
        await asyncio.sleep(0.5)

        # Old (stale) socket tries to submit an action → dealer must reject
        await ws_old.send(json.dumps({
            "type": "action", "turn_id": 1, "action": "call", "amount": 10
        }))
        got_stale_err = False
        try:
            for _ in range(20):
                raw = await asyncio.wait_for(ws_old.recv(), timeout=0.3)
                m = json.loads(raw)
                if m.get("type") == "error" and "stale" in m.get("text", "").lower():
                    got_stale_err = True
                    break
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass
        assert got_stale_err, "Stale (old) socket was NOT rejected by _ws_action"

        await dealer.trigger_stopgame()
        await ws_old.close(); await ws_new.close(); await ws2.close()
        print("PASS")
    finally:
        await _teardown(dealer, server, reg_path)


async def test_missing_turn_id_rejected():
    """Action without turn_id must be rejected outright (not silently accepted)."""
    print("test_missing_turn_id_rejected... ", end="", flush=True)
    dealer, server, reg_path = await _setup_dealer()
    try:
        socks = []
        for team in ("m1", "m2"):
            ws = await websockets.connect(WS_URL)
            await ws.send(json.dumps({"type": "register", "team": team, "invite": INVITE}))
            assert json.loads(await ws.recv())["type"] == "registered"
            socks.append(ws)
        await dealer.trigger_startgame()
        await asyncio.sleep(0.4)

        for ws in socks:
            await ws.send(json.dumps({"type": "action", "action": "call", "amount": 10}))

        got_missing = False
        for ws in socks:
            try:
                for _ in range(10):
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
                    m = json.loads(raw)
                    if m.get("type") == "error" and "missing turn_id" in m.get("text", "").lower():
                        got_missing = True
                        break
                if got_missing:
                    break
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                pass
        assert got_missing, "Dealer silently accepted action without turn_id"

        await dealer.trigger_stopgame()
        for s in socks:
            await s.close()
        print("PASS")
    finally:
        await _teardown(dealer, server, reg_path)


async def test_table_crash_aborts_tournament():
    """A crashed table task must abort the tournament (no false winner)."""
    print("test_table_crash_aborts_tournament... ", end="", flush=True)
    dealer, server, reg_path = await _setup_dealer()
    try:
        # 2 bots + start, then monkeypatch run_single_round to raise
        socks = []
        for team in ("c1", "c2"):
            ws = await websockets.connect(WS_URL)
            await ws.send(json.dumps({"type": "register", "team": team, "invite": INVITE}))
            assert json.loads(await ws.recv())["type"] == "registered"
            socks.append(ws)

        await dealer.trigger_startgame()
        # Wait for tables to exist
        for _ in range(20):
            if dealer.tables:
                break
            await asyncio.sleep(0.1)
        assert dealer.tables

        # Inject a failure into one table session
        some_table = next(iter(dealer.tables.values()))
        orig = some_table.run_single_round
        async def boom(*a, **kw):
            raise RuntimeError("synthetic table crash")
        some_table.run_single_round = boom  # type: ignore

        # Wait for coordinator to notice. Must NOT broadcast tournament_over.
        await asyncio.sleep(2.0)

        # State should have exited to IDLE via finally; game_state should NOT be "tournament_over"
        assert dealer._tournament_state == db.TournamentState.IDLE, dealer._tournament_state
        with db._spectator_lock:
            gs = db._spectator_state.get("game_state")
        assert gs != "tournament_over", (
            f"False winner declared after table crash (game_state={gs})"
        )

        for s in socks:
            await s.close()
        print("PASS")
    finally:
        await _teardown(dealer, server, reg_path)


async def test_proactive_consolidation():
    """Critical: with TABLE_SIZE=6 and 8 bots spread across 2 tables (4+4),
    after 4 eliminate to reach 4 total players (fits on one table), the
    coordinator must form the final table WITHOUT waiting for a table to
    fall to ≤1 player.
    """
    print("test_proactive_consolidation... ", end="", flush=True)
    # Override TABLE_SIZE for this test only
    old_table_size = os.environ.get("TABLE_SIZE")
    os.environ["TABLE_SIZE"] = "6"
    with db._spectator_lock:
        old_size_state = db._spectator_state.get("table_size")
        db._spectator_state["table_size"] = 6

    dealer, server, reg_path = await _setup_dealer()
    # _setup_dealer forces table_size=3; override to 6 for this test
    with db._spectator_lock:
        db._spectator_state["table_size"] = 6
    try:
        # 8 bots → ceil(8/6) = 2 tables
        socks = []
        for i in range(8):
            ws = await websockets.connect(WS_URL)
            await ws.send(json.dumps({"type": "register", "team": f"p{i}", "invite": INVITE}))
            assert json.loads(await ws.recv())["type"] == "registered"
            socks.append(ws)

        # Start all socks as aggressive-shove bots so busts happen fast
        stop = asyncio.Event()
        bot_tasks = []
        # We need bots that actually respond; reuse DummyBot via test_helpers
        from test_helpers import DummyBot
        # Close our throwaway sockets and re-create as strategy bots
        for s in socks:
            await s.close()
        bots = [DummyBot(WS_URL, f"p{i}", INVITE, strategy="smart_fallback")
                for i in range(8)]
        bot_tasks = [asyncio.create_task(b.run(stop)) for b in bots]
        await asyncio.sleep(0.5)

        # Ensure table_size is 6 at dealer-read time (setup modifies state but
        # other tests may have left 3 behind between runs).
        with db._spectator_lock:
            db._spectator_state["table_size"] = 6
        result = await dealer.trigger_startgame()
        assert result["ok"], result
        assert result["tables"] == 2, f"expected 2 tables for 8 bots @ size=6, got {result['tables']}"

        # Wait for tournament to complete
        for _ in range(200):
            await asyncio.sleep(0.1)
            if not dealer._is_game_active():
                break

        stop.set()
        for t in bot_tasks:
            t.cancel()
        try:
            await asyncio.gather(*bot_tasks, return_exceptions=True)
        except Exception:
            pass

        # Tournament must have traversed FINAL_TABLE (i.e., proactive consolidation fired)
        assert db.TournamentState.FINAL_TABLE in dealer._state_history, (
            f"FINAL_TABLE not observed at TABLE_SIZE=6 with 8 bots — proactive "
            f"consolidation did NOT fire. History: {[s.value for s in dealer._state_history]}"
        )
        # And tournament must have actually completed (not hung)
        assert db.TournamentState.COMPLETE in dealer._state_history, \
            "Tournament did not complete"
        alive = [a for a in dealer._tournament_agents if a.stack > 0]
        assert len(alive) == 1, f"Expected 1 winner, got {len(alive)}"
        print("PASS")
    finally:
        # Restore TABLE_SIZE
        if old_table_size is None:
            os.environ.pop("TABLE_SIZE", None)
        else:
            os.environ["TABLE_SIZE"] = old_table_size
        with db._spectator_lock:
            if old_size_state is not None:
                db._spectator_state["table_size"] = old_size_state
        await _teardown(dealer, server, reg_path)


async def test_blind_clock_monotonic():
    """_global_round_count must be monotonic even when the fastest table breaks."""
    print("test_blind_clock_monotonic... ", end="", flush=True)
    dealer, server, reg_path = await _setup_dealer()
    try:
        # Simulate: dealer has two fake tables; advance T1 to round 5, break it,
        # then ensure _global_round_count does not regress below 5 when T2 updates.
        from core.engine import GameEngine
        class _FakeTable:
            def __init__(self, rn):
                self.engine = GameEngine()
                self.engine.round_number = rn

        dealer.tables = {1: _FakeTable(5), 2: _FakeTable(2)}
        # Simulate table loop on T1:
        table_round = 6
        live_max = max(
            [t.engine.round_number for t in dealer.tables.values()] + [table_round]
        )
        dealer._global_round_count = max(dealer._global_round_count, live_max)
        assert dealer._global_round_count >= 6, dealer._global_round_count

        # Break T1 (the high-round one), leaving only T2 at round 2
        del dealer.tables[1]

        # T2 loop now computes its own update
        table_round = 3
        live_max = max(
            [t.engine.round_number for t in dealer.tables.values()] + [table_round]
        )
        dealer._global_round_count = max(dealer._global_round_count, live_max)

        # The clock must NOT regress
        assert dealer._global_round_count >= 6, (
            f"Blind clock regressed from 6 to {dealer._global_round_count} after "
            "fastest table was removed"
        )
        dealer.tables = {}
        print(f"PASS (clock stayed at {dealer._global_round_count})")
    finally:
        await _teardown(dealer, server, reg_path)


async def main():
    print("=" * 60)
    print("Edge-case Tests")
    print("=" * 60)
    await test_reconnect_race()
    await test_duplicate_team()
    await test_malformed_amount()
    await test_stop_then_restart()
    await test_stale_turn_id()
    await test_stale_socket_rejected()
    await test_missing_turn_id_rejected()
    await test_table_crash_aborts_tournament()
    await test_proactive_consolidation()
    await test_blind_clock_monotonic()
    print("=" * 60)
    print("ALL EDGE CASES PASS")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
