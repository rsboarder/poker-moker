"""Shared test helpers for dealer WS tests.

Provides DummyBot — a configurable WS client — and spawn_dummies helper.
Eliminates the duplicated register → recv → decide → respond loop in
test_ws_integration.py, test_multi_table.py, test_multi_table_e2e.py.

DummyBot strategies:
  - 'always_call'    — call if possible, else check, else fold
  - 'always_check'   — check if possible, else call, else fold
  - 'always_shove'   — raise all-in; on repeated engine errors fall back to call/check/fold
  - 'smart_fallback' — same as always_shove (the most robust strategy for test busts)
"""

from __future__ import annotations

import asyncio
import json
import re

import websockets


class DummyBot:
    """Configurable WebSocket poker bot used exclusively in tests.

    Example:
        stop = asyncio.Event()
        bot = DummyBot("ws://localhost:9000", "alice", "POKER-X", strategy="always_call")
        task = asyncio.create_task(bot.run(stop))
        ...
        stop.set()
    """

    def __init__(
        self,
        url: str,
        team: str,
        invite: str,
        strategy: str = "smart_fallback",
        token: str | None = None,
    ):
        self.url = url
        self.team = team
        self.invite = invite
        self.strategy = strategy
        self.token = token
        # Stats — inspected by tests
        self.stats: dict = {
            "actions": 0,
            "rounds": 0,
            "tables_seen": set(),
            "connected": False,
            "elimination": None,
            "errors": 0,
            "saved_token": None,
        }
        # Internal retry tracking for smart_fallback
        self._last_turn_id: int | None = None
        self._retry_count: int = 0

    async def run(self, stop: asyncio.Event) -> None:
        try:
            async with websockets.connect(self.url) as ws:
                reg = {"type": "register", "team": self.team, "invite": self.invite}
                if self.token:
                    reg["token"] = self.token
                await ws.send(json.dumps(reg))
                reply = json.loads(await ws.recv())
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

    def _decide(self, turn_msg: dict) -> tuple[str, int]:
        valid = turn_msg.get("valid_actions", [])
        stack = turn_msg.get("stack", 0)
        to_call = turn_msg.get("to_call", 0)
        min_raise = turn_msg.get("min_raise", 0)
        turn_id = turn_msg.get("turn_id")

        # Track whether this looks like a re-prompt (dealer sent us the turn again)
        if self._last_turn_id is not None and turn_id is not None and turn_id == self._last_turn_id + 1:
            self._retry_count += 1
        else:
            self._retry_count = 0
        self._last_turn_id = turn_id

        if self.strategy == "always_call":
            return self._pick_call(valid, to_call)
        if self.strategy == "always_check":
            return self._pick_check(valid, to_call)
        if self.strategy in ("always_shove", "smart_fallback"):
            # After 2 retries on the same situation, give up on raising and play safe.
            if self._retry_count >= 2:
                return self._pick_call(valid, to_call)
            raise_amt = stack + to_call
            if any("raise" in a for a in valid) and raise_amt >= min_raise:
                return ("raise", raise_amt)
            return self._pick_call(valid, to_call)
        # default safe
        return self._pick_call(valid, to_call)

    @staticmethod
    def _pick_call(valid, to_call) -> tuple[str, int]:
        parsed = DummyBot.parse_valid_actions(valid)
        if parsed["can_call"]:
            return ("call", to_call or parsed["call_amount"])
        if parsed["can_check"]:
            return ("check", 0)
        return ("fold", 0)

    @staticmethod
    def _pick_check(valid, to_call) -> tuple[str, int]:
        parsed = DummyBot.parse_valid_actions(valid)
        if parsed["can_check"]:
            return ("check", 0)
        if parsed["can_call"]:
            return ("call", to_call or parsed["call_amount"])
        return ("fold", 0)

    @staticmethod
    def parse_valid_actions(valid) -> dict:
        """Normalize valid_actions into a flag dict.

        Accepts either a string ('Valid: /fold /call 20 /raise 40')
        or a list (['fold', 'call 20', 'raise 40-1000']).
        """
        text: str
        items: list[str]
        if isinstance(valid, list):
            items = [str(x) for x in valid]
            text = " ".join(items).lower()
        else:
            text = str(valid).lower()
            items = re.split(r"\s+|,", text)
        can_call = any("call" in it for it in items) or "call" in text
        can_check = any("check" in it for it in items) or "check" in text
        can_raise = any("raise" in it for it in items) or "raise" in text
        m = re.search(r"call\s+(\d+)", text)
        call_amount = int(m.group(1)) if m else 0
        m = re.search(r"raise\s+(\d+)", text)
        min_raise = int(m.group(1)) if m else 0
        return {
            "can_call": can_call,
            "can_check": can_check,
            "can_raise": can_raise,
            "call_amount": call_amount,
            "min_raise": min_raise,
        }


def spawn_dummies(
    url: str,
    teams: list[str],
    invite: str,
    stop: asyncio.Event,
    strategy: str = "smart_fallback",
) -> tuple[list[DummyBot], list[asyncio.Task]]:
    """Create and start N DummyBots. Returns (bots, tasks) — inspect bot.stats after."""
    bots = [DummyBot(url, team, invite, strategy=strategy) for team in teams]
    tasks = [asyncio.create_task(b.run(stop)) for b in bots]
    return bots, tasks


# Minimal sanity tests — run `python test_helpers.py` to sanity-check parsing.
if __name__ == "__main__":
    # String form
    p1 = DummyBot.parse_valid_actions("Valid: /fold /call 20 /raise 40")
    assert p1 == {"can_call": True, "can_check": False, "can_raise": True,
                  "call_amount": 20, "min_raise": 40}, p1
    # List form
    p2 = DummyBot.parse_valid_actions(["fold", "call 120", "raise 240-970"])
    assert p2["can_call"] and p2["can_raise"] and not p2["can_check"], p2
    assert p2["call_amount"] == 120 and p2["min_raise"] == 240, p2
    # Check-only
    p3 = DummyBot.parse_valid_actions(["check", "raise 20-480"])
    assert p3["can_check"] and not p3["can_call"] and p3["can_raise"], p3
    print("parse_valid_actions: all cases pass")
