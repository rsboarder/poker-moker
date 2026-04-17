"""
AICollective AI Agents Poker — Dealer Bot
==========================================
Locally run Telegram bot. No LLM. Pure game orchestration logic.

Setup:
  1. Copy .env.example to .env and fill in values
  2. pip install -r requirements.txt
  3. python dealer_bot.py

BotFather setup required:
  - Enable Bot-to-Bot Communication Mode for the dealer bot
  - Disable privacy mode (so bot receives all group messages)
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import re
import secrets
import signal
import sys
import pathlib
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import websockets
from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.error import RetryAfter, TimedOut, NetworkError
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

# Add parent dir to path so core/ is importable
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from core.engine import GameEngine, GameEvent, GameState

load_dotenv()

# ---------------------------------------------------------------------------
# Spectator HTTP server (localhost:8765)
# ---------------------------------------------------------------------------

SPECTATOR_PORT = int(os.getenv("SPECTATOR_PORT", "8765"))
_SPECTATOR_HTML = pathlib.Path(__file__).parent.parent / "spectator.html"

_spectator_state: dict = {
    "game_state": "waiting",
    "round_number": 0,
    "street": "waiting",
    "pot": 0,
    "community_cards": [],
    "players": [],
    "current_player": None,
    "blinds": {"sb": 10, "bb": 20},
    "sb_player_id": None,
    "bb_player_id": None,
    "btn_player_id": None,
    "last_actions": {},
    "showdown_result": None,
    "recent_events": [],
    "timestamp": 0,
}
_spectator_lock = threading.Lock()


class _SpectatorHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silence request logs

    def do_GET(self):
        if self.path == "/state":
            with _spectator_lock:
                body = json.dumps(_spectator_state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/index.html"):
            if _SPECTATOR_HTML.exists():
                body = _SPECTATOR_HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404, "spectator.html not found")
        else:
            self.send_error(404)


def _start_spectator_server():
    server = HTTPServer(("localhost", SPECTATOR_PORT), _SpectatorHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log_spectator = logging.getLogger("dealer.spectator")
    log_spectator.info("Spectator server: http://localhost:%d", SPECTATOR_PORT)


# ---------------------------------------------------------------------------
# Logging — console + rotating file
# ---------------------------------------------------------------------------

_LOG_DIR = pathlib.Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")

_console = logging.StreamHandler()
_console.setFormatter(_fmt)

_file = logging.handlers.RotatingFileHandler(
    _LOG_DIR / "dealer.log",
    maxBytes=5 * 1024 * 1024,   # 5 MB per file
    backupCount=5,
    encoding="utf-8",
)
_file.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_console, _file])

# Silence noisy httpx/telegram polling spam at DEBUG; keep at WARNING
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)

log = logging.getLogger("dealer")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEALER_BOT_TOKEN   = os.environ["DEALER_BOT_TOKEN"]
MAIN_GROUP_ID      = int(os.environ["MAIN_GROUP_ID"])
ACTION_TIMEOUT     = float(os.getenv("ACTION_TIMEOUT_SECONDS", "5"))
STARTING_STACK     = int(os.getenv("STARTING_STACK", "1000"))
WS_PORT            = int(os.getenv("WS_PORT", "9000"))

# Comma-separated Telegram user IDs allowed to run admin commands
_admin_ids_raw = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS: set[int] = {int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip()}


def _is_admin(update: Update) -> bool:
    """Check if the sender is in the admin allowlist. If no allowlist is configured, allow all."""
    if not ADMIN_USER_IDS:
        return True
    user = update.effective_user
    return user is not None and user.id in ADMIN_USER_IDS

# ---------------------------------------------------------------------------
# Tournament blind schedule
# Each entry: (last_round_at_this_level, small_blind, big_blind)
# ---------------------------------------------------------------------------

BLIND_SCHEDULE = [
    (3,   10,   20),
    (6,   20,   40),
    (9,   30,   60),
    (12,  50,  100),
    (15, 100,  200),
    (999, 200, 400),
]


def get_blinds(round_number: int) -> tuple[int, int]:
    for max_round, sb, bb in BLIND_SCHEDULE:
        if round_number <= max_round:
            return sb, bb
    return BLIND_SCHEDULE[-1][1], BLIND_SCHEDULE[-1][2]


_REG_PATH = pathlib.Path(__file__).parent / "registrations.json"


def _load_registrations_sync() -> dict | None:
    if _REG_PATH.exists():
        try:
            return json.loads(_REG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _save_registrations_sync(data: dict) -> None:
    _REG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


async def _load_registrations() -> dict | None:
    return await asyncio.to_thread(_load_registrations_sync)


async def _save_registrations(data: dict) -> None:
    await asyncio.to_thread(_save_registrations_sync, data)


def load_agents() -> list[AgentInfo]:
    """Load agents from registrations.json first, fall back to AGENT_N_ env vars."""
    # Dynamic registrations
    data = _load_registrations_sync()
    if data:
        confirmed = [p for p in data.get("players", []) if p.get("username") and p.get("private_group_id")]
        if confirmed:
            log.info("Loading %d agent(s) from registrations.json (tournament: %s)",
                     len(confirmed), data.get("tournament_name", "?"))
            return [AgentInfo(
                player_id=i + 1,
                username=p["username"].lstrip("@"),
                private_group_id=int(p["private_group_id"]),
            ) for i, p in enumerate(confirmed)]

    # Legacy fallback — env vars
    agents = []
    for i in range(1, 7):
        username = os.getenv(f"AGENT_{i}_USERNAME")
        if not username:
            continue
        agents.append(AgentInfo(
            player_id=i,
            username=username.lstrip("@"),
            private_group_id=int(os.environ[f"AGENT_{i}_PRIVATE_GROUP_ID"]),
            stack=STARTING_STACK,
        ))
    return agents


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class AgentInfo:
    player_id: int
    username: str            # without @, primary identifier
    private_group_id: int
    stack: int = STARTING_STACK
    ready_message_id: int | None = None  # msg_id of agent's /ready message


# ---------------------------------------------------------------------------
# Table Session — per-table state (engine, action sync, agents)
# ---------------------------------------------------------------------------

class TableSession:
    """Encapsulates state for a single poker table. DealerBot owns one or more."""

    def __init__(self, table_id: int, agents: list[AgentInfo], bot: Bot,
                 ws_connections: dict[str, websockets.WebSocketServerProtocol] | None = None):
        self.table_id = table_id
        self.engine = GameEngine()
        self.agents = agents
        self.bot = bot
        self._ws = ws_connections or {}
        self._by_player_id: dict[int, AgentInfo] = {a.player_id: a for a in agents}

        # Action synchronization — scoped to this table
        self._pending_message_id: int | None = None
        self._pending_player_id:  int | None = None
        self._action_event = asyncio.Event()
        self._received_action: tuple[str, int] | None = None
        self._turn_id: int = 0

    def accept_action(self, sender_username: str, action: str, amount: int) -> bool:
        """Try to accept an action from a Telegram message. Returns True if accepted."""
        if not self._pending_player_id:
            return False
        expected = self._by_player_id.get(self._pending_player_id)
        if expected is None or sender_username != expected.username.lower():
            return False
        self._received_action = (action, amount)
        self._action_event.set()
        action_str = f"raise {amount}" if action == "raise" else action
        with _spectator_lock:
            _spectator_state["last_actions"][self._pending_player_id] = action_str
        return True

    async def run_single_round(self, active_agents: list, sb: int, bb: int):
        """Play one round with the given active agents. Engine already has blinds set."""
        players_data = [
            {"id": a.player_id, "name": a.username, "stack": a.stack}
            for a in active_agents
        ]

        log.info("[T%d] Round %d starting. players=%s blinds=%d/%d",
                 self.table_id, self.engine.round_number + 1,
                 [f"@{a.username}({a.stack})" for a in active_agents],
                 sb, bb)

        sb_player = active_agents[0].username
        bb_player = active_agents[1].username
        await _send(self.bot, MAIN_GROUP_ID,
                    f"--- Round {self.engine.round_number + 1} | Blinds: {sb}/{bb} "
                    f"| SB: @{sb_player} | BB: @{bb_player} ---")

        btn_id = active_agents[2].player_id if len(active_agents) >= 3 else active_agents[0].player_id
        with _spectator_lock:
            _spectator_state["sb_player_id"] = active_agents[0].player_id
            _spectator_state["bb_player_id"] = active_agents[1].player_id if len(active_agents) > 1 else None
            _spectator_state["btn_player_id"] = btn_id
            _spectator_state["last_actions"] = {}
            _spectator_state["showdown_result"] = None

        events = self.engine.start_round(players_data)
        log.info("[T%d] Round %d started. State: %s. Events: %d",
                 self.table_id, self.engine.round_number, self.engine.state.value, len(events))
        await self._dispatch_events(events)

        while self.engine.state not in (GameState.SHOWDOWN, GameState.WAITING):
            pending_agent = self._by_player_id.get(self._pending_player_id)
            log.info("[T%d] Waiting for action. state=%s pending_player=%s",
                     self.table_id, self.engine.state.value,
                     pending_agent.username if pending_agent else "None")

            action, amount = await self._wait_for_action()

            if self._pending_player_id is None:
                log.warning("[T%d] pending_player_id is None — aborting round", self.table_id)
                break

            if action == "_timeout_":
                agent = self._by_player_id[self._pending_player_id]
                to_call = self.engine._to_call(self.engine.players[self.engine.current_idx])
                if to_call == 0:
                    action = "check"
                    log.warning("[T%d] Timeout: @%s auto-checked", self.table_id, agent.username)
                    await _send(self.bot, MAIN_GROUP_ID,
                                f"⏱️ @{agent.username} timed out — auto-check.")
                else:
                    action = "fold"
                    log.warning("[T%d] Timeout: @%s auto-folded", self.table_id, agent.username)
                    await _send(self.bot, MAIN_GROUP_ID,
                                f"⏱️ @{agent.username} timed out — auto-fold.")
                with _spectator_lock:
                    _spectator_state["last_actions"][self._pending_player_id] = f"{action} (timeout)"
                # Prevent runaway loop when multiple bots disconnect
                await asyncio.sleep(0.5)

            log.info("[T%d] Applying action: player=%s action=%s amount=%d state=%s",
                     self.table_id, self._by_player_id[self._pending_player_id].username,
                     action, amount, self.engine.state.value)

            events = self.engine.apply_action(self._pending_player_id, action, amount)

            log.info("[T%d] After apply_action: new_state=%s events=%s",
                     self.table_id, self.engine.state.value, [e.type for e in events])

            if len(events) == 1 and events[0].type == "error":
                error_text = events[0].data.get("text", "Invalid action.")
                log.warning("[T%d] Engine rejected action: %s", self.table_id, error_text)
                await _send(self.bot, MAIN_GROUP_ID, f"❌ {error_text}")
                agent = self._by_player_id.get(self._pending_player_id)
                if agent:
                    p = self.engine.players[self.engine.current_idx]
                    prompt_ev = self.engine._dealer_prompt(p)
                    await self._send_action_request(agent, prompt_ev.data["text"])
                continue

            await self._dispatch_events(events)

        log.info("[T%d] Round %d complete. Final state: %s",
                 self.table_id, self.engine.round_number, self.engine.state.value)

        # Send round_end to all WS bots
        players_final = []
        for p in self.engine.players:
            a = self._by_player_id.get(p.id)
            players_final.append({
                "username": a.username if a else f"player_{p.id}",
                "stack": p.stack,
            })
        await self._ws_broadcast({
            "type": "round_end",
            "round": self.engine.round_number,
            "players": players_final,
        })

    async def _dispatch_events(self, events: list[GameEvent]):
        """Route GameEvents to appropriate Telegram chats and WS connections."""
        for ev in events:
            if ev.type == "hand_dealt":
                await self._send_hole_cards(ev)

            elif ev.type == "dealer_message":
                target = ev.data.get("target")
                text = ev.data.get("text", "")
                if target == "all":
                    await _send(self.bot, MAIN_GROUP_ID, text)
                    # Parse [DEALER] text into structured event for WS bots
                    ws_event = self._parse_dealer_text_to_event(text)
                    await self._ws_broadcast(ws_event)
                    self._update_spectator(text)
                else:
                    agent = self._by_player_id.get(int(target))
                    if agent:
                        await self._send_action_request(agent, text)
                        self._update_spectator()

            elif ev.type == "showdown":
                await self._send_showdown(ev)
                winner_id = ev.data.get("winner_id")
                winner_agent = self._by_player_id.get(winner_id)
                await self._ws_broadcast({
                    "type": "showdown",
                    "winner": winner_agent.username if winner_agent else f"player_{winner_id}",
                    "winner_id": winner_id,
                    "pot": ev.data.get("pot"),
                    "hands": ev.data.get("hands", []),
                    "reason": ev.data.get("reason"),
                })
                self._update_spectator()

            await asyncio.sleep(0.3)

    def _parse_dealer_text_to_event(self, text: str) -> dict:
        """Convert [DEALER] text to structured WS event."""
        import re
        event = {"type": "event", "text": text}

        # "[DEALER] aggressor folds."
        m = re.match(r'\[DEALER\]\s+(\S+)\s+(folds|checks|calls\s+\d+|raises to\s+\d+)', text)
        if m:
            player = m.group(1)
            action_raw = m.group(2)
            if action_raw == "folds":
                event["action"] = "fold"
            elif action_raw == "checks":
                event["action"] = "check"
            elif action_raw.startswith("calls"):
                event["action"] = "call"
                event["amount"] = int(action_raw.split()[-1])
            elif action_raw.startswith("raises to"):
                event["action"] = "raise"
                event["amount"] = int(action_raw.split()[-1])
            event["player"] = player

        # "[DEALER] --- FLOP: 9s Js 7d ---"
        m = re.match(r'\[DEALER\]\s+---\s+(FLOP|TURN|RIVER):\s+(.+?)\s+---', text)
        if m:
            from core.evaluator import cards_to_str
            event["street"] = m.group(1).lower()
            event["community"] = m.group(2).split()

        # Pot info
        m = re.search(r'Pot:\s*(\d+)', text)
        if m:
            event["pot"] = int(m.group(1))

        return event

    async def _ws_broadcast(self, msg: dict):
        """Send a message to all WS-connected bots at this table."""
        raw = json.dumps(msg)
        for agent in self.agents:
            ws = self._ws.get(agent.username.lower())
            if ws:
                try:
                    await ws.send(raw)
                except websockets.ConnectionClosed:
                    pass

    async def _send_hole_cards(self, ev: GameEvent):
        agent = self._by_player_id.get(ev.data["target"])
        if not agent:
            return
        cards = ev.data["hole_cards"]
        round_num = self.engine.round_number

        ws = self._ws.get(agent.username.lower())
        if ws:
            try:
                await ws.send(json.dumps({
                    "type": "cards",
                    "round": round_num,
                    "hole_cards": cards,
                }))
                return
            except websockets.ConnectionClosed:
                pass

        # Telegram fallback
        text = f"@{agent.username} Round #{round_num}: {cards[0]} {cards[1]}"
        if agent.ready_message_id:
            await self.bot.send_message(
                agent.private_group_id, text,
                reply_to_message_id=agent.ready_message_id,
            )
        else:
            await self.bot.send_message(agent.private_group_id, text)

    async def _send_action_request(self, agent: AgentInfo, prompt_text: str):
        self._action_event.clear()
        self._received_action = None
        self._pending_player_id = agent.player_id
        self._turn_id += 1

        ws = self._ws.get(agent.username.lower())
        if ws:
            # Parse prompt_text into structured data for WS bots
            state = self.engine.public_state()
            from core.evaluator import cards_to_str
            community = cards_to_str(self.engine.community) if self.engine.community else []
            player = next((p for p in self.engine.players if p.id == agent.player_id), None)
            to_call = self.engine._to_call(player) if player else 0
            max_bet = max(p.street_bet for p in self.engine.players)
            min_raise = max_bet + self.engine.big_blind

            valid_actions = []
            if to_call > 0:
                valid_actions = [f"fold", f"call {to_call}", f"raise {min_raise}-{player.stack + player.street_bet}"]
            else:
                valid_actions = [f"check", f"raise {min_raise}-{player.stack + player.street_bet}"]

            # Build players list with stacks and status
            players_info = []
            for p in self.engine.players:
                a = self._by_player_id.get(p.id)
                players_info.append({
                    "username": a.username if a else f"player_{p.id}",
                    "stack": p.stack,
                    "street_bet": p.street_bet,
                    "status": p.status.value,
                })

            # Determine position for this player
            # Standard 6-max: SB, BB, UTG, MP, CO, BTN
            player_idx = next((i for i, p in enumerate(self.engine.players) if p.id == agent.player_id), 0)
            n = len(self.engine.players)
            if n == 2:
                # Heads-up: SB=BTN acts first preflop, BB acts first postflop
                position = "SB" if player_idx == 0 else "BB"
            elif n == 3:
                position = ["SB", "BB", "BTN"][player_idx]
            elif n == 4:
                position = ["SB", "BB", "CO", "BTN"][player_idx]
            elif n == 5:
                position = ["SB", "BB", "UTG", "CO", "BTN"][player_idx]
            else:
                # 6+: SB, BB, UTG, MP..., CO, BTN
                if player_idx == 0:
                    position = "SB"
                elif player_idx == 1:
                    position = "BB"
                elif player_idx == n - 1:
                    position = "BTN"
                elif player_idx == n - 2:
                    position = "CO"
                elif player_idx == 2:
                    position = "UTG"
                else:
                    position = "MP"

            # Include hole cards so bot doesn't need to track state
            hole = cards_to_str(player.hole_cards) if player and player.hole_cards else []

            try:
                await ws.send(json.dumps({
                    "type": "turn",
                    "turn_id": self._turn_id,
                    "table_id": self.table_id,
                    "round": self.engine.round_number,
                    "street": self.engine.state.value,
                    "pot": self.engine.pot,
                    "stack": player.stack if player else 0,
                    "community": community,
                    "hole_cards": hole,
                    "position": position,
                    "players": players_info,
                    "valid_actions": valid_actions,
                    "to_call": to_call,
                    "min_raise": min_raise,
                }))
                return
            except websockets.ConnectionClosed:
                pass

        # Telegram fallback
        await asyncio.sleep(0.5)
        msg = await self.bot.send_message(
            MAIN_GROUP_ID,
            f"/turn@{agent.username}\n{prompt_text}",
        )
        self._pending_message_id = msg.message_id

    async def _wait_for_action(self) -> tuple[str, int]:
        try:
            await asyncio.wait_for(self._action_event.wait(), timeout=ACTION_TIMEOUT)
            result = self._received_action or ("fold", 0)
            return result
        except asyncio.TimeoutError:
            return ("_timeout_", 0)

    def _update_spectator(self, event_text: str | None = None):
        from core.evaluator import cards_to_str
        eng = self.engine

        players_out = []
        for p in eng.players:
            agent = self._by_player_id.get(p.id)
            username = agent.username if agent else f"Player {p.id}"
            hole_cards = cards_to_str(p.hole_cards) if p.hole_cards else []
            players_out.append({
                "id": p.id,
                "username": username,
                "stack": p.stack,
                "street_bet": p.street_bet,
                "status": p.status.value,
                "hole_cards": hole_cards,
                "is_current_turn": (self._pending_player_id == p.id),
            })

        community = cards_to_str(eng.community) if eng.community else []

        with _spectator_lock:
            _spectator_state["game_state"] = eng.state.value
            _spectator_state["round_number"] = eng.round_number
            _spectator_state["street"] = eng.state.value
            _spectator_state["pot"] = eng.pot
            _spectator_state["community_cards"] = community
            _spectator_state["players"] = players_out
            _spectator_state["current_player"] = self._pending_player_id
            _spectator_state["blinds"] = {"sb": eng.small_blind, "bb": eng.big_blind}
            _spectator_state["timestamp"] = int(time.time())
            if event_text:
                _spectator_state["recent_events"].append(event_text)
                _spectator_state["recent_events"] = _spectator_state["recent_events"][-30:]

    async def _send_showdown(self, ev: GameEvent):
        reason     = ev.data.get("reason", "showdown")
        hands      = ev.data.get("hands", [])
        pot_results = ev.data.get("pots", [])
        total_won  = ev.data.get("total_won", {})
        pot        = ev.data["pot"]

        def _name(pid: int) -> str:
            a = self._by_player_id.get(pid)
            return f"@{a.username}" if a else f"Player {pid}"

        lines = []

        if reason == "fold":
            winner_id = ev.data["winner_id"]
            lines.append(f"🏆 {_name(winner_id)} wins {pot} chips! (opponent folded)")
        elif len(pot_results) == 1 and not pot_results[0]["split"]:
            wid = pot_results[0]["winner_ids"][0]
            lines.append(f"🏆 {_name(wid)} wins {pot} chips!")
        else:
            for i, pr in enumerate(pot_results):
                label = "Main pot" if i == 0 else f"Side pot {i}"
                if pr["split"]:
                    share = pr["amount"] // len(pr["winner_ids"])
                    names = " & ".join(_name(w) for w in pr["winner_ids"])
                    lines.append(f"🤝 {label} ({pr['amount']}): split — {names} each win {share}")
                else:
                    wid = pr["winner_ids"][0]
                    lines.append(f"🏆 {label} ({pr['amount']}): {_name(wid)} wins")

        if reason not in ("fold", "last_standing") and hands:
            lines.append("\nShowdown:")
            for h in hands:
                name = _name(h["player_id"])
                cards = " ".join(h.get("hole_cards", []))
                rank = h.get("rank", "—")
                lines.append(f"  {name}: {cards} — {rank}")

        result_text = "\n".join(lines)
        await _send(self.bot, MAIN_GROUP_ID, result_text)
        with _spectator_lock:
            _spectator_state["showdown_result"] = {
                "text": result_text,
                "timestamp": int(time.time()),
                "reason": reason,
                "hands": hands,
                "pots": pot_results,
                "total_won": {str(k): v for k, v in total_won.items()},
                "winner_id": ev.data.get("winner_id"),
                "community": list(_spectator_state.get("community_cards", [])),
                "players_map": {str(p["id"]): p["username"] for p in _spectator_state.get("players", [])},
            }

    def _is_connected(self, agent: AgentInfo) -> bool:
        """Check if agent has an active WS connection."""
        ws = self._ws.get(agent.username.lower())
        return ws is not None and ws.open

    def stop(self):
        """Unblock any pending action wait so the table task can exit."""
        self._action_event.set()
        self._pending_player_id = None
        self._received_action = None


# ---------------------------------------------------------------------------
# Dealer Bot — orchestrates tournament, handles Telegram commands
# ---------------------------------------------------------------------------

class DealerBot:
    def __init__(self, agents: list[AgentInfo]):
        self.agents = agents
        self._by_player_id: dict[int, AgentInfo] = {a.player_id: a for a in agents}
        self._by_username:  dict[str, AgentInfo] = {a.username.lower(): a for a in agents}

        # Active table session (single table for now; will become dict for multi-table)
        self.table: TableSession | None = None

        # Keep task reference to prevent GC
        self._round_task: asyncio.Task | None = None

        # Will be set after Application is built
        self.bot: Bot | None = None

        # WebSocket connections: username → ws
        self.ws_connections: dict[str, websockets.WebSocketServerProtocol] = {}
        # Per-bot auth tokens: username → token (issued at registration)
        self._ws_tokens: dict[str, str] = {}

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------

    async def ws_handler(self, ws: websockets.WebSocketServerProtocol):
        """Handle a single WebSocket connection lifecycle."""
        username = None
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send(json.dumps({"type": "error", "text": "invalid JSON"}))
                    continue

                msg_type = msg.get("type")

                if msg_type == "register":
                    username = await self._ws_register(ws, msg)

                elif msg_type == "action":
                    await self._ws_action(ws, username, msg)

                else:
                    await ws.send(json.dumps({"type": "error", "text": f"unknown type: {msg_type}"}))

        except websockets.ConnectionClosed:
            pass
        finally:
            if username and username in self.ws_connections:
                del self.ws_connections[username]
                log.info("WS disconnected: %s (%d online)", username, len(self.ws_connections))

    async def _ws_register(self, ws, msg: dict) -> str | None:
        """Handle registration message. Returns username on success."""
        team = (msg.get("team") or "").strip()
        invite = (msg.get("invite") or "").strip().upper()
        token = msg.get("token")  # for reconnect

        if not team:
            await ws.send(json.dumps({"type": "error", "text": "missing team name"}))
            return None

        username = team.lower()

        # Reconnect with token
        if token and self._ws_tokens.get(username) == token:
            self.ws_connections[username] = ws
            log.info("WS reconnected: %s (%d online)", username, len(self.ws_connections))
            await ws.send(json.dumps({
                "type": "registered",
                "username": username,
                "reconnected": True,
                "players_online": len(self.ws_connections),
            }))
            return username

        # New registration — validate invite code
        data = _load_registrations_sync()
        expected_code = data.get("tournament_code", "") if data else ""
        if not expected_code or invite != expected_code:
            await ws.send(json.dumps({"type": "error", "text": "invalid invite code"}))
            return None

        # Check for duplicate connection
        if username in self.ws_connections:
            await ws.send(json.dumps({"type": "error", "text": f"{username} already connected"}))
            return None

        # Issue auth token
        bot_token = secrets.token_urlsafe(16)
        self._ws_tokens[username] = bot_token
        self.ws_connections[username] = ws

        log.info("WS registered: %s (%d online)", username, len(self.ws_connections))
        await ws.send(json.dumps({
            "type": "registered",
            "username": username,
            "token": bot_token,
            "reconnected": False,
            "players_online": len(self.ws_connections),
        }))

        # Notify Telegram group
        if self.bot:
            try:
                await _send(self.bot, MAIN_GROUP_ID,
                            f"📡 {team} connected via WS ({len(self.ws_connections)} online)")
            except Exception:
                pass

        return username

    async def _ws_action(self, ws, username: str | None, msg: dict):
        """Handle action message from bot."""
        if not username:
            await ws.send(json.dumps({"type": "error", "text": "not registered"}))
            return
        if not self.table:
            await ws.send(json.dumps({"type": "error", "text": "no active game"}))
            return

        action = (msg.get("action") or "").lower().strip()
        amount = int(msg.get("amount", 0))
        if action not in ("fold", "check", "call", "raise"):
            await ws.send(json.dumps({"type": "error", "text": f"invalid action: {action}"}))
            return

        accepted = self.table.accept_action(username, action, amount)
        if not accepted:
            await ws.send(json.dumps({"type": "error", "text": "not your turn"}))

    # ------------------------------------------------------------------
    # Public command handlers
    # ------------------------------------------------------------------

    async def cmd_startgame(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != MAIN_GROUP_ID:
            return
        if not _is_admin(update):
            await update.message.reply_text("⚠️ Only admins can start the game.")
            return
        if self.table and self.table.engine.state not in (GameState.WAITING, GameState.SHOWDOWN):
            await update.message.reply_text("❌ A round is already in progress.")
            return

        # Build roster from WS-connected bots (primary) + Telegram-registered (fallback)
        ws_usernames = set(self.ws_connections.keys())
        tg_usernames = {a.username.lower() for a in self.agents}

        # If WS bots are connected, use only WS bots (they are the real players).
        # Telegram-registered bots without WS connection would just timeout.
        if ws_usernames:
            player_usernames = ws_usernames
            source = "WS"
        else:
            player_usernames = tg_usernames
            source = "Telegram"

        if len(player_usernames) < 2:
            await update.message.reply_text(
                f"❌ Need at least 2 players. "
                f"WS: {len(ws_usernames)}, Telegram: {len(tg_usernames)}")
            return

        # Freeze roster
        self._tournament_agents = []
        pid = 1
        for username in sorted(player_usernames):
            tg_agent = self._by_username.get(username)
            self._tournament_agents.append(AgentInfo(
                player_id=pid,
                username=username,
                private_group_id=tg_agent.private_group_id if tg_agent else 0,
                stack=STARTING_STACK,
                ready_message_id=tg_agent.ready_message_id if tg_agent else None,
            ))
            pid += 1

        self.table = TableSession(
            table_id=1, agents=self._tournament_agents, bot=self.bot,
            ws_connections=self.ws_connections,
        )

        log.info("=== TOURNAMENT START: %d players (%s) stacks=%d ===",
                 len(self._tournament_agents), source, STARTING_STACK)

        sb, bb = get_blinds(1)
        players_str = ", ".join(f"@{a.username}" for a in self._tournament_agents)
        await update.message.reply_text(
            f"🏆 AICollective AI Agents Poker — Tournament\n"
            f"Players: {players_str}\n"
            f"Starting stack: {STARTING_STACK} chips\n"
            f"Blinds: {sb}/{bb}"
        )
        self._round_task = asyncio.create_task(self._run_tournament())

    async def cmd_stopgame(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != MAIN_GROUP_ID:
            return
        if not _is_admin(update):
            await update.message.reply_text("⚠️ Only admins can stop the game.")
            return

        if self._round_task and not self._round_task.done():
            self._round_task.cancel()
            log.info("=== TOURNAMENT STOPPED by @%s ===",
                     update.effective_user.username if update.effective_user else "unknown")

        # Unblock any pending action wait so the cancelled task exits cleanly
        if self.table:
            self.table.stop()
            self.table.engine.state = GameState.WAITING

        await update.message.reply_text(
            "🛑 Tournament stopped. Send /startgame to start a new one."
        )

    async def cmd_newtournament(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Create a new tournament with a unique registration code."""
        if update.effective_chat.id != MAIN_GROUP_ID:
            return
        name = " ".join(context.args) if context.args else "AI Poker Tournament"
        code = "POKER-" + secrets.token_hex(2).upper()
        data = {
            "tournament_code": code,
            "tournament_name": name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "players": [],
        }
        await _save_registrations(data)
        log.info("New tournament created: %s (code: %s)", name, code)
        await update.message.reply_text(
            f"🎲 Tournament created: {name}\n"
            f"Registration code: `{code}`\n\n"
            f"Participants: create a private group with dealer + your agent bot, "
            f"then your agent sends:\n`/register {code} YourTeamName`",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_players(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current tournament roster."""
        data = await _load_registrations()
        if not data:
            await update.message.reply_text("No tournament created yet. Use /newtournament.")
            return
        players = data.get("players", [])
        lines = [f"🏆 {data['tournament_name']} [{data['tournament_code']}]"]
        if not players:
            lines.append("No teams registered yet.")
        else:
            for i, p in enumerate(players, 1):
                lines.append(f"  {i}. {p['team']} — @{p['username']}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_kick(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove a team from the tournament roster."""
        if update.effective_chat.id != MAIN_GROUP_ID:
            return
        if not _is_admin(update):
            await update.message.reply_text("⚠️ Only admins can kick players.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /kick @botusername")
            return
        target = context.args[0].lstrip("@")
        data = await _load_registrations()
        if not data:
            await update.message.reply_text("No tournament active.")
            return
        before = len(data["players"])
        data["players"] = [p for p in data["players"] if p["username"] != target]
        if len(data["players"]) < before:
            await _save_registrations(data)
            # Reload agents list
            self.agents = load_agents()
            self._by_player_id = {a.player_id: a for a in self.agents}
            self._by_username  = {a.username.lower(): a for a in self.agents}
            await update.message.reply_text(f"✅ @{target} removed from tournament.")
        else:
            await update.message.reply_text(f"⚠️ @{target} not found in roster.")

    async def cmd_registration(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /registration <CODE> @botusername from a private group (sent by bot owner).

        private_group_id captured from msg.chat.id. Username is the primary identifier.
        """
        msg = update.message
        if not msg or not msg.from_user:
            return
        if msg.chat.id == MAIN_GROUP_ID:
            await msg.reply_text("⚠️ Use /registration from the private group (dealer + agent).")
            return

        if not context.args or len(context.args) < 2:
            await msg.reply_text(
                "❌ Usage: /registration@aicollective_poker_dealer_bot <TOURNAMENT_CODE> @botusername\n"
                "Example: /registration@aicollective_poker_dealer_bot POKER-A3F7 @my_poker_bot"
            )
            return

        code = context.args[0].upper()
        bot_username = context.args[1].lstrip("@")
        group_id = msg.chat.id

        data = await _load_registrations()
        if not data:
            await msg.reply_text("❌ No active tournament. Ask the organizer to run /newtournament.")
            return
        if data.get("tournament_code") != code:
            await msg.reply_text(f"❌ Unknown tournament code `{code}`.",
                                 parse_mode=ParseMode.MARKDOWN)
            return

        if any(p["username"].lower() == bot_username.lower() for p in data["players"]):
            await msg.reply_text(f"⚠️ @{bot_username} is already registered.")
            return

        data["players"].append({
            "team": f"@{bot_username}",
            "username": bot_username,
            "private_group_id": group_id,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        })
        await _save_registrations(data)

        self.agents = load_agents()
        self._by_player_id = {a.player_id: a for a in self.agents}
        self._by_username  = {a.username.lower(): a for a in self.agents}

        count = len(data["players"])
        log.info("Registered: @%s group=%d (%d total)", bot_username, group_id, count)
        await msg.reply_text(f"✅ @{bot_username} registered!")
        await _send(self.bot, MAIN_GROUP_ID,
                    f"✅ @{bot_username} registered — {count} team(s) in roster")

    async def on_agent_ready(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Store /ready message_id from agent's private group for reply-based hole card delivery."""
        msg = update.message
        if not msg:
            return
        chat_id = msg.chat.id
        agent = next((a for a in self.agents if a.private_group_id == chat_id), None)
        if agent:
            agent.ready_message_id = msg.message_id
            log.info("Agent @%s ready (msg_id=%d)", agent.username, msg.message_id)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.table:
            await update.message.reply_text("Game not started. Use /startgame.")
            return
        state = self.table.engine.public_state()
        if state["state"] == "waiting":
            await update.message.reply_text("Game not started. Use /startgame.")
            return

        lines = [f"Street: {state['state']} | Pot: {state['pot']}"]
        if state["community_cards"]:
            lines.append(f"Board: {' '.join(state['community_cards'])}")
        lines.append("")
        for p in state["players"]:
            agent = self.table._by_player_id.get(p["id"])
            name = f"@{agent.username}" if agent else f"Player {p['id']}"
            marker = " ◀ turn" if state["active_player"] == p["id"] else ""
            lines.append(f"{name}: stack={p['stack']} bet={p['street_bet']} [{p['status']}]{marker}")

        await update.message.reply_text("\n".join(lines))

    async def cmd_fold(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._accept_action(update, "fold", 0)

    async def cmd_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._accept_action(update, "check", 0)

    async def cmd_call(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._accept_action(update, "call", 0)

    async def cmd_raise(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        if not msg:
            return
        if not context.args:
            await msg.reply_text("Usage: /raise <amount>")
            return
        try:
            amount = int(context.args[0])
        except ValueError:
            await msg.reply_text("❌ Invalid amount — must be an integer.")
            return
        await self._accept_action(update, "raise", amount)

    async def _accept_action(self, update: Update, action: str, amount: int):
        """Shared validation + dispatch for all action commands. Routes to active table."""
        msg = update.message
        if not msg or not self.table:
            return
        sender_username = (msg.from_user.username or "").lower() if msg.from_user else ""

        if not self.table.accept_action(sender_username, action, amount):
            await msg.reply_text("⚠️ It's not your turn.")

    async def on_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Fallback handler for human players not using /bet.
        Accepts plain text like '/call', '/raise 100' as a reply to dealer's message.
        """
        msg = update.message
        if not msg or not self.table or not self.table._pending_player_id:
            return

        if not msg.reply_to_message or msg.reply_to_message.message_id != self.table._pending_message_id:
            return

        sender_username = (msg.from_user.username or "").lower() if msg.from_user else ""
        action, amount = _parse_command(msg.text or "")
        if not action:
            await msg.reply_text(
                "❌ Invalid command. Use /fold | /check | /call | /raise <amount>"
            )
            return

        if not self.table.accept_action(sender_username, action, amount):
            await msg.reply_text("⚠️ It's not your turn.")

    # ------------------------------------------------------------------
    # Tournament orchestration
    # ------------------------------------------------------------------

    async def _run_tournament(self):
        """Main tournament loop — runs rounds until one player remains.
        Uses self.table (TableSession created at /startgame).
        """
        try:
            table = self.table
            agents = self._tournament_agents
            current_sb, current_bb = get_blinds(1)
            table.engine.set_blinds(current_sb, current_bb)
            dealer_idx = 0

            while True:
                active = [a for a in agents if a.stack > 0]

                if len(active) < 2:
                    break

                new_sb, new_bb = get_blinds(table.engine.round_number + 1)
                if new_sb != current_sb:
                    current_sb, current_bb = new_sb, new_bb
                    table.engine.set_blinds(current_sb, current_bb)
                    await _send(self.bot, MAIN_GROUP_ID,
                               f"⬆️ Blinds increased to {current_sb}/{current_bb}!")

                n = len(active)
                sb_pos = dealer_idx % n
                rotated = active[sb_pos:] + active[:sb_pos]

                await table.run_single_round(rotated, current_sb, current_bb)
                dealer_idx += 1

                # Sync stacks from engine back to agents
                for p in table.engine.players:
                    agent = table._by_player_id.get(p.id)
                    if agent:
                        agent.stack = p.stack

                # Detect and announce eliminations
                for a in agents:
                    if a.stack == 0 and a in active:
                        await _send(self.bot, MAIN_GROUP_ID, f"💀 @{a.username} is eliminated!")

                # Show chip counts
                standings = sorted(
                    [a for a in agents if a.stack > 0],
                    key=lambda x: x.stack, reverse=True
                )
                if len(standings) >= 2:
                    lines = ["Chip counts:"]
                    for a in standings:
                        lines.append(f"  @{a.username}: {a.stack}")
                    await _send(self.bot, MAIN_GROUP_ID, "\n".join(lines))

                await asyncio.sleep(3.0)

            # Tournament over
            winner = max(agents, key=lambda a: a.stack)
            log.info("=== TOURNAMENT OVER. Winner: @%s ===", winner.username)
            await _send(self.bot, MAIN_GROUP_ID,
                        f"🏆 Tournament over!\n@{winner.username} wins with {winner.stack} chips!")
            table.engine.state = GameState.WAITING

        except Exception as e:
            log.error("Tournament crashed: %s", e, exc_info=True)
            try:
                await _send(self.bot, MAIN_GROUP_ID, f"❌ Tournament error: {e}")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _send(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    """Send a message with automatic RetryAfter / network-timeout handling."""
    attempts = 0
    while True:
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return
        except RetryAfter as e:
            log.warning("Flood control: waiting %ds before retry", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
        except (TimedOut, NetworkError) as e:
            attempts += 1
            wait = min(4 * attempts, 30)
            log.warning("Telegram network error (%s), retry %d in %ds", e, attempts, wait)
            await asyncio.sleep(wait)


def _parse_command(text: str) -> tuple[str, int]:
    """'/raise 100' → ('raise', 100). Returns ('', 0) on parse failure.
    Also handles '@botname /command' format (player mentions dealer then gives command).
    """
    text = text.strip()
    # Strip leading @mention if present: "@dealerbot /call" → "/call"
    if text.startswith("@"):
        parts_raw = text.split(None, 1)
        text = parts_raw[1] if len(parts_raw) > 1 else ""
    parts = text.strip().lstrip("/").split()
    if not parts:
        return "", 0
    action = parts[0].lower()
    if action not in ("fold", "check", "call", "raise"):
        return "", 0
    amount = 0
    if len(parts) > 1:
        try:
            amount = int(parts[1])
        except ValueError:
            return "", 0
    return action, amount


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_app(dealer: DealerBot) -> Application:
    """Build and configure the Telegram Application (without starting it)."""
    app = (
        Application.builder()
        .token(DEALER_BOT_TOKEN)
        .build()
    )

    dealer.bot = app.bot

    app.add_handler(CommandHandler("startgame",      dealer.cmd_startgame))
    app.add_handler(CommandHandler("stopgame",       dealer.cmd_stopgame))
    app.add_handler(CommandHandler("status",         dealer.cmd_status))
    app.add_handler(CommandHandler("fold",           dealer.cmd_fold))
    app.add_handler(CommandHandler("check",          dealer.cmd_check))
    app.add_handler(CommandHandler("call",           dealer.cmd_call))
    app.add_handler(CommandHandler("raise",          dealer.cmd_raise))
    app.add_handler(CommandHandler("newtournament",  dealer.cmd_newtournament))
    app.add_handler(CommandHandler("players",        dealer.cmd_players))
    app.add_handler(CommandHandler("kick",           dealer.cmd_kick))
    app.add_handler(CommandHandler("registration",   dealer.cmd_registration))
    app.add_handler(MessageHandler(
        filters.Chat(MAIN_GROUP_ID) & filters.TEXT,
        dealer.on_group_message,
    ))
    # Capture /ready from agents in their private groups
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r'^/ready'),
        dealer.on_agent_ready,
    ))
    return app


async def async_main():
    """Async entry point — controls the event loop explicitly so we can
    add a WebSocket server alongside Telegram polling later.
    """
    agents = load_agents()
    if not agents:
        log.warning("No pre-configured agents. Waiting for WS connections before /startgame.")
    else:
        log.info("Loaded %d agent(s): %s", len(agents), [a.username for a in agents])

    dealer = DealerBot(agents)
    app = _build_app(dealer)

    _start_spectator_server()

    # --- Start Telegram polling (non-blocking) ---
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    log.info("Dealer bot started. Main group: %d", MAIN_GROUP_ID)

    # --- WebSocket server ---
    ws_server = await websockets.serve(dealer.ws_handler, "0.0.0.0", WS_PORT)
    log.info("WebSocket server started on ws://0.0.0.0:%d", WS_PORT)

    # Block until interrupted
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        log.info("Shutting down...")
        ws_server.close()
        await ws_server.wait_closed()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
