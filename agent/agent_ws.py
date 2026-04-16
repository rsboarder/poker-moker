"""WebSocket transport for the poker agent.

Connects to the dealer via WS, receives structured JSON (cards, turns, events),
calls the same make_decision() strategy as the Telegram version.

Usage:
    python agent_ws.py --url ws://localhost:9000 --team MyBot --invite POKER-XXXX
    python agent_ws.py --url ws://localhost:9000 --team MyBot --invite POKER-XXXX --env ../bots/aggressor.env
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

import websockets
from dotenv import load_dotenv

from opponent_tracker import OpponentTracker
from storage import GameStorage
from strategy import make_decision
from strategy_profiles import get_profile

log_root = logging.getLogger("agent_ws")


class WsPokerBot:
    """Poker bot that communicates via WebSocket instead of Telegram."""

    def __init__(self, team: str, env_file: str | None = None):
        if env_file:
            load_dotenv(env_file, override=True)
        else:
            load_dotenv()

        self.team = team
        self.username = team.lower()
        self.profile = get_profile(os.getenv("STRATEGY_PROFILE", "gto"))
        self.ai_cli_path = os.getenv("CODEX_PATH", "claude")

        # Game state
        self.hole_cards: list[str] = []
        self.community_cards: list[str] = []
        self.pot: int = 0
        self.stack: int = 0
        self.street: str = "preflop"
        self.position: str = ""
        self.round_num: str = ""
        self.opponent_actions: deque[str] = deque(maxlen=10)

        # Services
        self.storage = GameStorage(bot_name=self.username)
        self.tracker = OpponentTracker(self.storage, self.username)

        # Logging
        self.log = self._setup_logging()
        self.log.info("WS bot initialized: %s (profile=%s)", self.username, self.profile.name)

    def _setup_logging(self) -> logging.Logger:
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)

        bot_logger = logging.getLogger(f"agent_ws.{self.username}")
        bot_logger.setLevel(logging.DEBUG)

        if not bot_logger.handlers:
            fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

            fh = RotatingFileHandler(
                log_dir / f"{self.username}_ws.log", maxBytes=5_000_000, backupCount=3
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            bot_logger.addHandler(fh)

            ch = logging.StreamHandler()
            ch.setLevel(logging.INFO)
            ch.setFormatter(fmt)
            bot_logger.addHandler(ch)

        return bot_logger

    async def run(self, url: str, invite: str, token: str | None = None):
        self.log.info("Connecting to %s...", url)

        async with websockets.connect(url) as ws:
            # Register
            reg = {"type": "register", "team": self.team, "invite": invite}
            if token:
                reg["token"] = token
            await ws.send(json.dumps(reg))

            reply = json.loads(await ws.recv())
            if reply.get("type") != "registered":
                self.log.error("Registration failed: %s", reply)
                return

            saved_token = reply.get("token")
            self.log.info("Registered as %s (online=%d)", reply["username"], reply.get("players_online", 0))

            # Game loop
            async for raw in ws:
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "cards":
                    self._on_cards(msg)

                elif msg_type == "turn":
                    action = await self._on_turn(msg)
                    await ws.send(json.dumps({
                        "type": "action",
                        "turn_id": msg.get("turn_id"),
                        **action,
                    }))

                elif msg_type == "event":
                    self._on_event(msg)

                elif msg_type == "showdown":
                    self.log.info("Showdown: winner=%s pot=%s", msg.get("winner"), msg.get("pot"))

                elif msg_type == "tournament_start":
                    self.log.info("Tournament started! Table %s", msg.get("your_table"))

                elif msg_type == "eliminated":
                    self.log.info("Eliminated: %s place", msg.get("place"))
                    break

                elif msg_type == "tournament_over":
                    self.log.info("Tournament over! Winner: %s", msg.get("winner"))
                    break

                elif msg_type == "error":
                    self.log.warning("Error from dealer: %s", msg.get("text"))

    def _on_cards(self, msg: dict):
        self.hole_cards = msg.get("hole_cards", [])
        self.community_cards = []
        self.opponent_actions.clear()
        self.tracker.reset_round()
        self.round_num = str(msg.get("round", ""))
        self.log.info("Round #%s — hole cards: %s", self.round_num, self.hole_cards)

    async def _on_turn(self, msg: dict) -> dict:
        self.street = msg.get("street", self.street)
        self.pot = msg.get("pot", self.pot)
        self.stack = msg.get("stack", self.stack)
        self.community_cards = msg.get("community", self.community_cards)
        self.tracker.set_street(self.street)

        # Build valid_line in the format make_decision expects
        to_call = msg.get("to_call", 0)
        min_raise = msg.get("min_raise", 0)
        if to_call > 0:
            valid_line = f"Valid: /fold  /call {to_call}  /raise <total, min:{min_raise}>"
        else:
            valid_line = f"Valid: /check  /raise <total, min:{min_raise}>"

        self.log.info("Turn [%s] street=%s pot=%d stack=%d community=%s hole=%s",
                      msg.get("turn_id"), self.street, self.pot, self.stack,
                      self.community_cards, self.hole_cards)

        # Call strategy in executor to not block event loop (LLM calls are slow)
        loop = asyncio.get_event_loop()
        action_str = await loop.run_in_executor(
            None, make_decision,
            self.hole_cards, self.community_cards, self.street, self.pot,
            self.stack, self.position or "MP", valid_line,
            self.tracker, self.storage, self.round_num,
        )

        self.log.info("Decision: %s", action_str)

        # Parse "call", "raise 200", "fold", "check" into action dict
        parts = action_str.split()
        action = parts[0].lower()
        amount = int(parts[1]) if len(parts) > 1 else 0

        if action == "call":
            amount = to_call

        return {"action": action, "amount": amount}

    def _on_event(self, msg: dict):
        text = msg.get("text", "")
        if text.startswith("[DEALER]"):
            self.opponent_actions.append(text)
            new_community = self.tracker.parse_dealer_message(text)
            if new_community:
                self.community_cards = new_community
                self.log.debug("Community updated: %s", self.community_cards)


def main():
    parser = argparse.ArgumentParser(description="Poker Agent (WebSocket)")
    parser.add_argument("--url", default="ws://localhost:9000")
    parser.add_argument("--team", required=True)
    parser.add_argument("--invite", required=True)
    parser.add_argument("--token", default=None, help="Reconnect token")
    parser.add_argument("--env", default=None, help="Path to .env file (strategy profile, etc)")
    args = parser.parse_args()

    bot = WsPokerBot(team=args.team, env_file=args.env)

    try:
        asyncio.run(bot.run(args.url, args.invite, args.token))
    except KeyboardInterrupt:
        bot.log.info("Interrupted.")
    finally:
        bot.storage.close()


if __name__ == "__main__":
    main()
