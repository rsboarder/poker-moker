"""End-to-end tournament test: real GameEngine + WS server + dummy bots.

Tests the full flow without Telegram:
- WS server accepts bot connections
- Bots register with invite code
- Multiple rounds play to completion
- Chip conservation verified
- Someone wins the tournament

Usage:
    python test_e2e_tournament.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import pathlib

import websockets

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from core.engine import GameEngine, GameState

WS_PORT = 9877
INVITE_CODE = "E2E-TEST"
NUM_BOTS = 6
STARTING_STACK = 500


class E2EDealer:
    """Full dealer with real engine, WS server, multi-round tournament."""

    def __init__(self):
        self.engine = GameEngine()
        self.ws_connections: dict[str, websockets.WebSocketServerProtocol] = {}
        self._pending_username: str | None = None
        self._action_event = asyncio.Event()
        self._received_action: tuple[str, int] | None = None
        self._turn_id = 0
        self.round_count = 0

    async def ws_handler(self, ws):
        username = None
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg["type"] == "register":
                    username = msg["team"].lower()
                    self.ws_connections[username] = ws
                    await ws.send(json.dumps({
                        "type": "registered",
                        "username": username,
                        "players_online": len(self.ws_connections),
                    }))
                elif msg["type"] == "action":
                    if username == self._pending_username:
                        action = msg.get("action", "fold").lower()
                        amount = int(msg.get("amount", 0))
                        self._received_action = (action, amount)
                        self._action_event.set()
        except websockets.ConnectionClosed:
            pass
        finally:
            if username and username in self.ws_connections:
                del self.ws_connections[username]

    async def send_turn(self, username: str, player) -> tuple[str, int]:
        self._turn_id += 1
        self._pending_username = username
        self._action_event.clear()
        self._received_action = None

        from core.evaluator import cards_to_str
        community = cards_to_str(self.engine.community) if self.engine.community else []
        to_call = self.engine._to_call(player)
        max_bet = max(p.street_bet for p in self.engine.players)
        min_raise = max_bet + self.engine.big_blind

        if to_call > 0:
            valid = [f"fold", f"call {to_call}", f"raise {min_raise}-{player.stack + player.street_bet}"]
        else:
            valid = [f"check", f"raise {min_raise}-{player.stack + player.street_bet}"]

        ws = self.ws_connections.get(username)
        if not ws:
            return ("check", 0) if to_call == 0 else ("fold", 0)

        await ws.send(json.dumps({
            "type": "turn",
            "turn_id": self._turn_id,
            "round": self.round_count,
            "street": self.engine.state.value,
            "pot": self.engine.pot,
            "stack": player.stack,
            "community": community,
            "valid_actions": valid,
            "to_call": to_call,
            "min_raise": min_raise,
        }))

        try:
            await asyncio.wait_for(self._action_event.wait(), timeout=3.0)
            return self._received_action or ("fold", 0)
        except asyncio.TimeoutError:
            return ("check", 0) if to_call == 0 else ("fold", 0)

    async def deal_cards(self):
        from core.evaluator import cards_to_str
        for p in self.engine.players:
            ws = self.ws_connections.get(p.name.lower())
            if ws:
                try:
                    await ws.send(json.dumps({
                        "type": "cards",
                        "round": self.round_count,
                        "hole_cards": cards_to_str(p.hole_cards),
                    }))
                except websockets.ConnectionClosed:
                    pass

    async def broadcast(self, msg: dict):
        raw = json.dumps(msg)
        for ws in self.ws_connections.values():
            try:
                await ws.send(raw)
            except websockets.ConnectionClosed:
                pass


async def dummy_bot(url: str, team: str, results: dict, stop: asyncio.Event):
    """Bot that always calls or checks. Tracks rounds played."""
    results[team] = {"rounds": 0, "actions": 0, "connected": False}
    try:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({
                "type": "register", "team": team, "invite": INVITE_CODE,
            }))
            reply = json.loads(await ws.recv())
            if reply["type"] != "registered":
                return
            results[team]["connected"] = True

            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    break

                msg = json.loads(raw)
                if msg["type"] == "turn":
                    valid = msg.get("valid_actions", [])
                    action, amount = "fold", 0
                    for a in valid:
                        if "call" in a:
                            action = "call"
                            m = re.search(r'(\d+)', a)
                            amount = int(m.group(1)) if m else 0
                            break
                        if "check" in a:
                            action = "check"
                            break
                    await ws.send(json.dumps({
                        "type": "action",
                        "turn_id": msg["turn_id"],
                        "action": action,
                        "amount": amount,
                    }))
                    results[team]["actions"] += 1
                elif msg["type"] == "cards":
                    results[team]["rounds"] += 1
                elif msg["type"] == "showdown":
                    pass
                elif msg["type"] == "eliminated":
                    break
                elif msg["type"] == "tournament_over":
                    break
    except Exception:
        pass


async def run_tournament():
    print("=" * 60)
    print("E2E Tournament Test")
    print(f"  {NUM_BOTS} bots, {STARTING_STACK} chips each, WS on :{WS_PORT}")
    print("=" * 60)

    dealer = E2EDealer()
    server = await websockets.serve(dealer.ws_handler, "localhost", WS_PORT)
    stop = asyncio.Event()
    bot_results = {}

    # Start bots
    bot_names = [f"bot{i}" for i in range(1, NUM_BOTS + 1)]
    bot_tasks = [
        asyncio.create_task(dummy_bot(f"ws://localhost:{WS_PORT}", name, bot_results, stop))
        for name in bot_names
    ]
    await asyncio.sleep(0.5)

    connected = sum(1 for r in bot_results.values() if r["connected"])
    print(f"  {connected}/{NUM_BOTS} bots connected")
    assert connected == NUM_BOTS, f"Not all bots connected: {connected}/{NUM_BOTS}"

    # Prepare players
    players_data = [
        {"id": i + 1, "name": name, "stack": STARTING_STACK}
        for i, name in enumerate(bot_names)
    ]
    total_chips = STARTING_STACK * NUM_BOTS

    sb, bb = 10, 20
    dealer.engine.set_blinds(sb, bb)
    dealer_idx = 0
    max_rounds = 100
    eliminations = []

    for round_num in range(1, max_rounds + 1):
        # Update stacks
        if dealer.round_count > 0:
            for p in dealer.engine.players:
                for pd in players_data:
                    if pd["id"] == p.id:
                        pd["stack"] = p.stack

        alive = [p for p in players_data if p["stack"] > 0]
        if len(alive) < 2:
            break

        # Blind increase every 10 rounds
        if round_num > 1 and round_num % 10 == 1:
            sb *= 2
            bb *= 2
            dealer.engine.set_blinds(sb, bb)
            print(f"  ⬆ Blinds → {sb}/{bb}")

        dealer.round_count = round_num
        n = len(alive)
        sb_pos = dealer_idx % n
        rotated = alive[sb_pos:] + alive[:sb_pos]

        dealer.engine.start_round(rotated)
        await dealer.deal_cards()

        # Action loop
        while dealer.engine.state not in (GameState.SHOWDOWN, GameState.WAITING):
            player = dealer.engine.players[dealer.engine.current_idx]
            action, amount = await dealer.send_turn(player.name.lower(), player)
            result = dealer.engine.apply_action(player.id, action, amount)

            # If engine returned error, auto-fold
            if len(result) == 1 and result[0].type == "error":
                dealer.engine.apply_action(player.id, "fold", 0)

        dealer_idx += 1

        # Detect eliminations
        for p in dealer.engine.players:
            if p.stack == 0:
                for pd in players_data:
                    if pd["id"] == p.id and pd["stack"] > 0:
                        pd["stack"] = 0
                        eliminations.append(p.name)
                        print(f"  💀 {p.name} eliminated (round {round_num})")

        # Verify chip conservation every round
        engine_total = sum(p.stack for p in dealer.engine.players)
        assert engine_total == total_chips, \
            f"Chip leak at round {round_num}: {engine_total} != {total_chips}"

    # Tournament complete
    stop.set()
    for t in bot_tasks:
        t.cancel()
    try:
        await asyncio.gather(*bot_tasks, return_exceptions=True)
    except Exception:
        pass
    server.close()
    await server.wait_closed()

    # Results
    alive = [p for p in players_data if p["stack"] > 0]
    winner = max(players_data, key=lambda p: p["stack"])

    print()
    print("=" * 60)
    print("Results:")
    print(f"  Rounds played: {dealer.round_count}")
    print(f"  Eliminations:  {len(eliminations)}")
    print(f"  Survivors:     {len(alive)}")
    print(f"  Winner:        {winner['name']} ({winner['stack']} chips)")
    print()
    print("  Bot stats:")
    for name in bot_names:
        r = bot_results.get(name, {})
        pd = next(p for p in players_data if p["name"] == name)
        status = "ALIVE" if pd["stack"] > 0 else "OUT"
        print(f"    {name}: {r.get('actions', 0)} actions, "
              f"{r.get('rounds', 0)} rounds, {pd['stack']} chips [{status}]")

    print()
    # Assertions
    assert dealer.round_count >= 2, "Should play at least 2 rounds"
    assert winner["stack"] == total_chips, \
        f"Winner should have all {total_chips} chips, has {winner['stack']}"
    assert len(alive) == 1, f"Should have exactly 1 survivor, got {len(alive)}"

    print(f"ALL ASSERTIONS PASSED")
    print(f"  ✓ {dealer.round_count} rounds completed")
    print(f"  ✓ {total_chips} chips conserved")
    print(f"  ✓ 1 winner: {winner['name']}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_tournament())
