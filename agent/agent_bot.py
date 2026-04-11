import asyncio
import logging
import os
import re
import subprocess
import sys
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from opponent_tracker import OpponentTracker
from storage import GameStorage
from strategy import make_decision
from strategy_profiles import get_profile

# ── Parsing regexes (shared) ─────────────────────────────────────────────────

CARD_RE = re.compile(r"[2-9TJQKA][hdcs]")
RE_STREET = re.compile(r"Street:\s*(\w+)", re.IGNORECASE)
RE_POT = re.compile(r"Pot:\s*(\d+)", re.IGNORECASE)
RE_STACK = re.compile(r"Stack:\s*(\d+)", re.IGNORECASE)
RE_COMMUNITY = re.compile(r"Community:\s*([^\n]+)", re.IGNORECASE)
RE_POSITION = re.compile(r"Position:\s*(\w+)", re.IGNORECASE)
RE_ROUND_NUM = re.compile(r"Round\s*#(\d+)", re.IGNORECASE)
RE_HOLE_ROUND = re.compile(r"Round\s*#\d+:\s*([2-9TJQKA][hdcs])\s+([2-9TJQKA][hdcs])")
RE_HOLE_BARE = re.compile(r"[`*]?\s*([2-9TJQKA][hdcs])\s+([2-9TJQKA][hdcs])\s*[`*]?")


def parse_turn_message(text: str) -> dict:
    state = {}
    if m := RE_STREET.search(text):
        state["street"] = m.group(1).lower()
    if m := RE_POT.search(text):
        state["pot"] = int(m.group(1))
    if m := RE_STACK.search(text):
        state["stack"] = int(m.group(1))
    if m := RE_COMMUNITY.search(text):
        raw = m.group(1).strip()
        if raw not in ("—", "-"):
            state["community"] = CARD_RE.findall(raw)
        else:
            state["community"] = []
    if m := RE_POSITION.search(text):
        state["position"] = m.group(1).upper()
    for line in text.splitlines():
        if "Valid:" in line or "valid:" in line:
            state["valid_line"] = line.strip()
            break
    return state


def parse_hole_cards(text: str) -> list[str] | None:
    if m := RE_HOLE_ROUND.search(text):
        return [m.group(1), m.group(2)]
    if m := RE_HOLE_BARE.search(text):
        return [m.group(1), m.group(2)]
    return None


# ── Bot Instance ──────────────────────────────────────────────────────────────

class PokerBot:
    """A single poker bot instance with its own config, storage, and state."""

    def __init__(self, env_file: str | None = None):
        if env_file:
            load_dotenv(env_file, override=True)
        else:
            load_dotenv()

        self.token = os.environ["AGENT_BOT_TOKEN"]
        self.username = os.environ["AGENT_USERNAME"]
        self.main_group_id = int(os.environ["MAIN_GROUP_ID"])
        self.private_group_id = int(os.environ["PRIVATE_GROUP_ID"])
        self.dealer_username = os.getenv("DEALER_USERNAME", "aicollective_poker_dealer_bot")
        self.ai_cli_path = os.getenv("CODEX_PATH", "claude")
        self.profile = get_profile(os.getenv("STRATEGY_PROFILE", "gto"))

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
        self.logger = self._setup_logging()
        self.logger.info("Bot initialized: @%s (profile=%s, cli=%s)",
                         self.username, self.profile.name, self.ai_cli_path)

    def _setup_logging(self) -> logging.Logger:
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)

        bot_logger = logging.getLogger(f"agent.{self.username}")
        bot_logger.setLevel(logging.DEBUG)

        if not bot_logger.handlers:
            fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

            fh = RotatingFileHandler(
                log_dir / f"{self.username}.log", maxBytes=5_000_000, backupCount=3
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            bot_logger.addHandler(fh)

            ch = logging.StreamHandler()
            ch.setLevel(logging.INFO)
            ch.setFormatter(fmt)
            bot_logger.addHandler(ch)

        return bot_logger

    # ── Handlers ──────────────────────────────────────────────

    async def handle_turn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not msg.text:
            return

        self.logger.info("/turn received [msg=%s]: %.120s", msg.message_id, msg.text)

        lines = msg.text.splitlines()
        body = "\n".join(lines[1:]) if len(lines) > 1 else msg.text

        state = parse_turn_message(body)
        self.street = state.get("street", self.street)
        self.pot = state.get("pot", self.pot)
        self.stack = state.get("stack", self.stack)
        if "community" in state:
            self.community_cards = state["community"]
        if "position" in state:
            self.position = state["position"]
        elif not self.position:
            self.position = "MP"
        valid_line = state.get("valid_line", "Valid: /fold /call")

        self.tracker.set_street(self.street)

        self.logger.info("State — street=%s pot=%d stack=%d community=%s hole=%s pos=%s",
                         self.street, self.pot, self.stack, self.community_cards,
                         self.hole_cards, self.position)

        loop = asyncio.get_event_loop()
        action = await loop.run_in_executor(
            None, make_decision,
            self.hole_cards, self.community_cards, self.street, self.pot,
            self.stack, self.position, valid_line, self.tracker, self.storage,
            self.round_num,
        )

        self.logger.info("Decision: %s", action)

        parts = action.split()
        if parts[0] == "raise" and len(parts) == 2:
            command = f"/raise {parts[1]}"
        else:
            command = f"/{parts[0]}"

        await context.bot.send_message(self.main_group_id, command)
        self.logger.info("Sent: %s", command)

    async def handle_private_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not msg.text:
            return

        cards = parse_hole_cards(msg.text)
        if cards:
            self.hole_cards = cards
            self.community_cards = []
            self.opponent_actions.clear()
            self.tracker.reset_round()

            if m := RE_POSITION.search(msg.text):
                self.position = m.group(1).upper()
            if m := RE_ROUND_NUM.search(msg.text):
                self.round_num = m.group(1)

            self.logger.info("Round #%s — hole: %s, pos: %s",
                             self.round_num, self.hole_cards, self.position)

    async def handle_main_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not msg.text:
            return

        text = msg.text
        if text.startswith("[DEALER]"):
            self.opponent_actions.append(text)
            new_community = self.tracker.parse_dealer_message(text)
            if new_community:
                self.community_cards = new_community
                self.logger.debug("Community updated: %s", self.community_cards)

    # ── Run ───────────────────────────────────────────────────

    def run(self):
        try:
            subprocess.run([self.ai_cli_path, "--version"], timeout=5, capture_output=True)
            self.logger.info("AI CLI verified: %s", self.ai_cli_path)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            self.logger.warning("AI CLI check failed (%s)", e)

        app = Application.builder().token(self.token).build()

        app.add_handler(CommandHandler("turn", self.handle_turn))
        app.add_handler(MessageHandler(
            filters.Chat(self.private_group_id) & filters.TEXT & ~filters.COMMAND,
            self.handle_private_message,
        ))
        app.add_handler(MessageHandler(
            filters.Chat(self.main_group_id) & filters.TEXT & ~filters.COMMAND,
            self.handle_main_message,
        ))

        async def post_ready(application):
            await application.bot.send_message(self.private_group_id, "/ready")
            self.logger.info("Sent /ready to private group")

        app.post_init = post_ready

        self.logger.info("Starting @%s (profile=%s) — polling...", self.username, self.profile.name)
        app.run_polling(allowed_updates=Update.ALL_TYPES)


def main():
    env_file = sys.argv[1] if len(sys.argv) > 1 else None
    bot = PokerBot(env_file=env_file)
    bot.run()


if __name__ == "__main__":
    main()
