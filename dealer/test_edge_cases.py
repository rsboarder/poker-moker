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


async def main():
    print("=" * 60)
    print("Edge-case Tests")
    print("=" * 60)
    await test_reconnect_race()
    await test_duplicate_team()
    await test_malformed_amount()
    await test_stop_then_restart()
    await test_stale_turn_id()
    print("=" * 60)
    print("ALL EDGE CASES PASS")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
