"""Poker Bot Template — replace decide() with your strategy.

Usage:
    python bot.py --url ws://tournament.example.com:9000 --team YourTeamName --invite POKER-XXXX

Requirements:
    pip install websockets
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("bot")


def decide(turn: dict) -> dict:
    """Your strategy goes here.

    Args:
        turn: dict with keys:
            - turn_id: int (must return in response)
            - street: "preflop" | "flop" | "turn" | "river"
            - pot: int (current pot size)
            - stack: int (your remaining chips)
            - community: list[str] (board cards, e.g. ["Ah", "7d", "3s"])
            - valid_actions: list[str] (e.g. ["fold", "call 20", "raise 40-1000"])
            - to_call: int (chips needed to call, 0 if can check)
            - min_raise: int (minimum raise-to amount)

    Returns:
        dict with keys:
            - action: "fold" | "check" | "call" | "raise"
            - amount: int (required for call and raise)

    Example responses:
        {"action": "fold", "amount": 0}
        {"action": "check", "amount": 0}
        {"action": "call", "amount": 20}
        {"action": "raise", "amount": 100}
    """
    to_call = turn.get("to_call", 0)

    # Default: call if cheap, check if free, fold if expensive
    if to_call == 0:
        return {"action": "check", "amount": 0}
    elif to_call <= turn.get("stack", 0) * 0.1:
        return {"action": "call", "amount": to_call}
    else:
        return {"action": "fold", "amount": 0}


async def run(url: str, team: str, invite: str):
    log.info("Connecting to %s...", url)

    async with websockets.connect(url) as ws:
        # Register
        await ws.send(json.dumps({
            "type": "register", "team": team, "invite": invite,
        }))
        reply = json.loads(await ws.recv())
        if reply.get("type") != "registered":
            log.error("Registration failed: %s", reply)
            return

        token = reply.get("token")
        log.info("Registered as %s", reply["username"])

        # Game loop
        async for raw in ws:
            msg = json.loads(raw)
            t = msg.get("type")

            if t == "turn":
                action = decide(msg)
                await ws.send(json.dumps({
                    "type": "action",
                    "turn_id": msg["turn_id"],
                    **action,
                }))
                log.info("[%s] %s %s (pot=%s stack=%s)",
                         msg.get("street"), action["action"],
                         action.get("amount", ""), msg.get("pot"), msg.get("stack"))

            elif t == "cards":
                log.info("Hole cards: %s", msg.get("hole_cards"))

            elif t == "event":
                pass  # game events (other players' actions, board cards)

            elif t == "showdown":
                log.info("Showdown: winner=%s pot=%s", msg.get("winner"), msg.get("pot"))

            elif t == "tournament_start":
                log.info("Tournament started! Table %s", msg.get("your_table"))

            elif t == "eliminated":
                log.info("Eliminated: %s place", msg.get("place"))
                break

            elif t == "tournament_over":
                log.info("Tournament over! Winner: %s", msg.get("winner"))
                break

            elif t == "error":
                log.warning("Error: %s", msg.get("text"))


def main():
    p = argparse.ArgumentParser(description="Poker Tournament Bot")
    p.add_argument("--url", default="ws://localhost:9000")
    p.add_argument("--team", required=True)
    p.add_argument("--invite", required=True)
    args = p.parse_args()

    try:
        asyncio.run(run(args.url, args.team, args.invite))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
