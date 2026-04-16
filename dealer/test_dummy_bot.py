"""Dummy bot — connects via WebSocket, always calls or checks.

Usage:
    python test_dummy_bot.py --url ws://localhost:9000 --team DummyA --invite POKER-C17D
    # Run multiple in parallel:
    python test_dummy_bot.py --team Bot1 --invite POKER-C17D &
    python test_dummy_bot.py --team Bot2 --invite POKER-C17D &
    python test_dummy_bot.py --team Bot3 --invite POKER-C17D &
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)
log = logging.getLogger("dummy")


async def run_bot(url: str, team: str, invite: str, token: str | None = None):
    log.info("Connecting to %s as %s...", url, team)

    async with websockets.connect(url) as ws:
        # Register
        reg = {"type": "register", "team": team, "invite": invite}
        if token:
            reg["token"] = token
        await ws.send(json.dumps(reg))

        reply = json.loads(await ws.recv())
        if reply.get("type") != "registered":
            log.error("Registration failed: %s", reply)
            return
        log.info("Registered as %s (token=%s, online=%d)",
                 reply["username"], reply.get("token", "?"), reply.get("players_online", 0))

        saved_token = reply.get("token")

        # Main loop — respond to dealer messages
        async for raw in ws:
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "turn":
                action = _decide(msg)
                response = {
                    "type": "action",
                    "turn_id": msg.get("turn_id"),
                    "action": action["action"],
                    "amount": action.get("amount", 0),
                }
                await ws.send(json.dumps(response))
                log.info("Turn %s: %s %s (pot=%s stack=%s)",
                         msg.get("turn_id"), action["action"],
                         action.get("amount", ""), msg.get("pot"), msg.get("stack"))

            elif msg_type == "cards":
                log.info("Cards: %s", msg.get("hole_cards"))

            elif msg_type == "event":
                log.info("Event: %s", _summarize_event(msg))

            elif msg_type == "showdown":
                log.info("Showdown: winner=%s pot=%s", msg.get("winner"), msg.get("pot"))

            elif msg_type == "eliminated":
                log.info("Eliminated! Place: %s", msg.get("place"))
                break

            elif msg_type == "tournament_start":
                log.info("Tournament started! Table: %s", msg.get("your_table"))

            elif msg_type == "tournament_over":
                log.info("Tournament over! Winner: %s", msg.get("winner"))
                break

            elif msg_type == "error":
                log.warning("Error: %s", msg.get("text"))

            else:
                log.debug("Unknown message: %s", msg_type)


def _decide(turn_msg: dict) -> dict:
    """Trivial strategy: call if possible, check if possible, fold as last resort."""
    valid = turn_msg.get("valid_actions", "")

    # Parse valid_actions — can be string or list
    if isinstance(valid, str):
        valid_lower = valid.lower()
        if "call" in valid_lower:
            # Extract call amount
            import re
            m = re.search(r'call\s+(\d+)', valid_lower)
            amount = int(m.group(1)) if m else 0
            return {"action": "call", "amount": amount}
        if "check" in valid_lower:
            return {"action": "check"}
        return {"action": "fold"}

    # If it's a list of action strings
    if isinstance(valid, list):
        for a in valid:
            if "call" in a.lower():
                import re
                m = re.search(r'(\d+)', a)
                amount = int(m.group(1)) if m else 0
                return {"action": "call", "amount": amount}
        for a in valid:
            if "check" in a.lower():
                return {"action": "check"}
        return {"action": "fold"}

    return {"action": "fold"}


def _summarize_event(msg: dict) -> str:
    parts = []
    if "action" in msg:
        parts.append(f"{msg.get('player', '?')} {msg['action']}")
    if "street" in msg:
        parts.append(f"street={msg['street']}")
    if "community" in msg:
        parts.append(f"board={msg['community']}")
    return " | ".join(parts) if parts else json.dumps(msg)


def main():
    parser = argparse.ArgumentParser(description="Dummy poker bot (WS)")
    parser.add_argument("--url", default="ws://localhost:9000")
    parser.add_argument("--team", required=True)
    parser.add_argument("--invite", required=True)
    parser.add_argument("--token", default=None, help="Reconnect token")
    args = parser.parse_args()

    try:
        asyncio.run(run_bot(args.url, args.team, args.invite, args.token))
    except KeyboardInterrupt:
        log.info("Interrupted.")


if __name__ == "__main__":
    main()
