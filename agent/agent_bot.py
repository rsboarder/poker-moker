import asyncio
import logging
import os
import re
import subprocess
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

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

AGENT_BOT_TOKEN = os.environ["AGENT_BOT_TOKEN"]
AGENT_USERNAME = os.environ["AGENT_USERNAME"]
MAIN_GROUP_ID = int(os.environ["MAIN_GROUP_ID"])
PRIVATE_GROUP_ID = int(os.environ["PRIVATE_GROUP_ID"])
DEALER_USERNAME = os.getenv("DEALER_USERNAME", "aicollective_poker_dealer_bot")
AI_CLI_PATH = os.getenv("CODEX_PATH", "codex")

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("agent")
logger.setLevel(logging.DEBUG)

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_fh = RotatingFileHandler(LOG_DIR / f"{AGENT_USERNAME}.log", maxBytes=5_000_000, backupCount=3)
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_ch.setFormatter(_fmt)
logger.addHandler(_ch)

# ── Game State ────────────────────────────────────────────────────────────────

hole_cards: list[str] = []
community_cards: list[str] = []
pot: int = 0
stack: int = 0
street: str = "preflop"
position: str = ""
round_num: str = ""
opponent_actions: deque[str] = deque(maxlen=10)

# ── Services ──────────────────────────────────────────────────────────────────

storage = GameStorage()
tracker = OpponentTracker(storage, AGENT_USERNAME)

# ── Parsing ───────────────────────────────────────────────────────────────────

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

# ── Telegram Handlers ─────────────────────────────────────────────────────────

async def handle_turn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global pot, stack, street, community_cards, position

    msg = update.effective_message
    if not msg or not msg.text:
        return

    chat_id = msg.chat_id
    logger.info("/turn received [msg=%s chat=%s]: %.120s", msg.message_id, chat_id, msg.text)

    text = msg.text
    lines = text.splitlines()
    body = "\n".join(lines[1:]) if len(lines) > 1 else text

    state = parse_turn_message(body)
    street = state.get("street", street)
    pot = state.get("pot", pot)
    stack = state.get("stack", stack)
    if "community" in state:
        community_cards = state["community"]
    # Update position from /turn if available (fallback to existing or "MP")
    if "position" in state:
        position = state["position"]
    elif not position:
        position = "MP"
    valid_line = state.get("valid_line", "Valid: /fold /call")

    tracker.set_street(street)

    logger.info("Parsed state — street=%s pot=%d stack=%d community=%s hole=%s pos=%s",
                street, pot, stack, community_cards, hole_cards, position)

    loop = asyncio.get_event_loop()
    action = await loop.run_in_executor(
        None,
        make_decision,
        hole_cards, community_cards, street, pot, stack,
        position, valid_line, tracker, storage, round_num,
    )

    logger.info("Decision: %s", action)

    parts = action.split()
    if parts[0] == "raise" and len(parts) == 2:
        command = f"/raise {parts[1]}"
    else:
        command = f"/{parts[0]}"

    await context.bot.send_message(MAIN_GROUP_ID, command)
    logger.info("Sent to main group: %s", command)


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global hole_cards, community_cards, position, round_num

    msg = update.effective_message
    if not msg or not msg.text:
        return

    text = msg.text

    cards = parse_hole_cards(text)
    if cards:
        hole_cards = cards
        community_cards = []
        opponent_actions.clear()
        tracker.reset_round()

        if m := RE_POSITION.search(text):
            position = m.group(1).upper()
        if m := RE_ROUND_NUM.search(text):
            round_num = m.group(1)

        logger.info("Round #%s — hole cards: %s, position: %s", round_num, hole_cards, position)


async def handle_main_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global community_cards

    msg = update.effective_message
    if not msg or not msg.text:
        return

    text = msg.text
    if text.startswith("[DEALER]"):
        opponent_actions.append(text)

        # Update opponent stats + community cards
        new_community = tracker.parse_dealer_message(text)
        if new_community:
            community_cards = new_community
            logger.debug("Community updated from dealer: %s", community_cards)

# ── Startup ───────────────────────────────────────────────────────────────────

async def post_ready(application: Application) -> None:
    await application.bot.send_message(PRIVATE_GROUP_ID, "/ready")
    logger.info("Sent /ready to private group %s", PRIVATE_GROUP_ID)


def main() -> None:
    # Verify AI CLI is reachable
    try:
        subprocess.run([AI_CLI_PATH, "--version"], timeout=5, capture_output=True)
        logger.info("AI CLI verified: %s", AI_CLI_PATH)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("AI CLI check failed (%s) — bot will use rule/equity layers only", e)

    app = Application.builder().token(AGENT_BOT_TOKEN).build()

    # Handler 1: /turn command (any chat)
    app.add_handler(CommandHandler("turn", handle_turn))

    # Handler 2: private group messages (hole cards)
    app.add_handler(MessageHandler(
        filters.Chat(PRIVATE_GROUP_ID) & filters.TEXT & ~filters.COMMAND,
        handle_private_message,
    ))

    # Handler 3: main group messages (passive observation)
    app.add_handler(MessageHandler(
        filters.Chat(MAIN_GROUP_ID) & filters.TEXT & ~filters.COMMAND,
        handle_main_message,
    ))

    app.post_init = post_ready

    logger.info("Starting %s — polling...", AGENT_USERNAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
