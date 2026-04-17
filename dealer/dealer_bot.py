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
import math
import os
import random
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
_CONTROL_HTML   = pathlib.Path(__file__).parent.parent / "control.html"

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
    "ws_players": [],
    "tg_configured": False,  # set after config load
    "tg_logging": False,     # toggled from control panel
    "spectator_mode": False, # slow down game for human viewers
    "action_delay": 2.0,     # seconds to wait after each player action
    "round_pause": 10.0,     # seconds to pause between rounds
    "table_size": 6,         # players per table (overridable via /spectator-config)
    "tables": {},            # per-table state for multi-table mode: {table_id: {...}}
    "table_count": 0,        # number of active tables
}
_spectator_lock = threading.Lock()

# References set in async_main — used by the HTTP handler thread
_dealer_ref: "DealerBot | None" = None
_loop_ref:   asyncio.AbstractEventLoop | None = None


class _SpectatorHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silence request logs

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.end_headers()

    def do_POST(self):
        if _dealer_ref is None or _loop_ref is None:
            self._send_json(503, {"error": "dealer not ready"})
            return
        if self.path == "/startgame":
            future = asyncio.run_coroutine_threadsafe(
                _dealer_ref.trigger_startgame(), _loop_ref
            )
            try:
                result = future.result(timeout=10)
                self._send_json(200 if result.get("ok") else 400, result)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
        elif self.path == "/stopgame":
            future = asyncio.run_coroutine_threadsafe(
                _dealer_ref.trigger_stopgame(), _loop_ref
            )
            try:
                result = future.result(timeout=10)
                self._send_json(200, result)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
        elif self.path == "/tg-toggle":
            with _spectator_lock:
                if not _spectator_state["tg_configured"]:
                    self._send_json(400, {"error": "Telegram not configured"})
                    return
                _spectator_state["tg_logging"] = not _spectator_state["tg_logging"]
                new_val = _spectator_state["tg_logging"]
            log.info("Telegram logging %s via HTTP", "enabled" if new_val else "disabled")
            self._send_json(200, {"ok": True, "tg_logging": new_val})
        elif self.path == "/spectator-toggle":
            with _spectator_lock:
                _spectator_state["spectator_mode"] = not _spectator_state["spectator_mode"]
                new_val = _spectator_state["spectator_mode"]
            log.info("Spectator mode %s via HTTP", "enabled" if new_val else "disabled")
            self._send_json(200, {"ok": True, "spectator_mode": new_val})
        elif self.path == "/spectator-config":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            with _spectator_lock:
                game_active = _spectator_state.get("table_count", 0) > 0
                if "action_delay" in body:
                    _spectator_state["action_delay"] = max(0.0, float(body["action_delay"]))
                if "round_pause" in body:
                    _spectator_state["round_pause"] = max(0.0, float(body["round_pause"]))
                if "table_size" in body:
                    if game_active:
                        self._send_json(400, {"error": "cannot change table_size while game is running"})
                        return
                    size = int(body["table_size"])
                    size = max(TABLE_SIZE_MIN, min(size, TABLE_SIZE_MAX))
                    _spectator_state["table_size"] = size
                result = {
                    "ok": True,
                    "action_delay": _spectator_state["action_delay"],
                    "round_pause": _spectator_state["round_pause"],
                    "table_size": _spectator_state["table_size"],
                }
            log.info("Spectator config updated: %s", result)
            self._send_json(200, result)
        else:
            self._send_json(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/state":
            with _spectator_lock:
                self._send_json(200, _spectator_state)
        elif self.path in ("/", "/index.html"):
            self._serve_html(_SPECTATOR_HTML)
        elif self.path in ("/control", "/control.html"):
            self._serve_html(_CONTROL_HTML)
        else:
            self.send_error(404)

    def _serve_html(self, path: pathlib.Path):
        if path.exists():
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404, f"{path.name} not found")


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

DEALER_BOT_TOKEN   = os.getenv("DEALER_BOT_TOKEN", "")
MAIN_GROUP_ID      = int(os.getenv("MAIN_GROUP_ID", "0"))
TG_CONFIGURED      = bool(DEALER_BOT_TOKEN and MAIN_GROUP_ID)
ACTION_TIMEOUT     = float(os.getenv("ACTION_TIMEOUT_SECONDS", "5"))
STARTING_STACK     = int(os.getenv("STARTING_STACK", "1000"))
WS_PORT            = int(os.getenv("WS_PORT", "9000"))
TABLE_SIZE_DEFAULT = int(os.getenv("TABLE_SIZE", "6"))
TABLE_SIZE_MIN     = 2
TABLE_SIZE_MAX     = 10

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

from enum import Enum


class TournamentState(str, Enum):
    """Explicit lifecycle for the multi-table tournament coordinator."""
    IDLE        = "idle"          # no tournament running
    STARTING    = "starting"      # seating in progress, coordinator not yet scheduled
    RUNNING     = "running"       # coordinator running, multiple tables may be active
    FINAL_TABLE = "final_table"   # consolidated to one final table
    COMPLETE    = "complete"      # winner crowned, awaiting reset to IDLE


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
                 ws_connections: dict[str, websockets.WebSocketServerProtocol] | None = None,
                 dealer: "DealerBot | None" = None):
        self.table_id = table_id
        self.engine = GameEngine()
        self.agents = agents
        self._dealer = dealer  # back-reference for broadcast helpers (None in tests using MiniDealer)
        self.bot = bot
        self._ws = ws_connections or {}
        self._by_player_id: dict[int, AgentInfo] = {a.player_id: a for a in agents}

        # Action synchronization — scoped to this table
        self._pending_message_id: int | None = None
        self._pending_player_id:  int | None = None
        self._action_event = asyncio.Event()
        self._received_action: tuple[str, int] | None = None
        self._turn_id: int = 0
        self._tg_buffer: list[str] = []  # accumulates per-round lines for one batched TG message

        # Per-table spectator state (lifted out of the flat _spectator_state root so
        # multiple tables don't clobber each other's blind positions / last actions).
        self.last_actions: dict[int, str] = {}
        self.showdown_result: dict | None = None
        self.sb_player_id: int | None = None
        self.bb_player_id: int | None = None
        self.btn_player_id: int | None = None

        # Set when coordinator wants this table to exit after the current round completes
        # (used for graceful final-table formation instead of hard-cancelling mid-hand).
        self._stop_after_round: bool = False

    def _tg_prefix(self) -> str:
        """Return '[Tn] ' prefix when multi-table, empty otherwise. Decided from spectator state."""
        with _spectator_lock:
            n = int(_spectator_state.get("table_count", 0))
        return f"[T{self.table_id}] " if n > 1 else ""

    async def _tg_send(self, text: str):
        """Send to TG with table prefix when multi-table."""
        await _send(self.bot, MAIN_GROUP_ID, self._tg_prefix() + text)

    def accept_action(self, sender_username: str, action: str, amount: int,
                      turn_id: int | None = None) -> bool:
        """Try to accept an action. Returns True if accepted.
        If turn_id is provided, it MUST match self._turn_id (reject stale replies)."""
        if not self._pending_player_id:
            return False
        expected = self._by_player_id.get(self._pending_player_id)
        if expected is None or sender_username != expected.username.lower():
            return False
        if turn_id is not None and turn_id != self._turn_id:
            log.warning("[T%d] stale turn_id from @%s: got %s expected %s",
                        self.table_id, sender_username, turn_id, self._turn_id)
            return False
        self._received_action = (action, amount)
        self._action_event.set()
        action_str = f"raise {amount}" if action == "raise" else action
        self.last_actions[self._pending_player_id] = action_str
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
        await self._tg_send(
            f"--- Round {self.engine.round_number + 1} | Blinds: {sb}/{bb} "
            f"| SB: @{sb_player} | BB: @{bb_player} ---"
        )

        btn_id = active_agents[-1].player_id if len(active_agents) >= 3 else active_agents[0].player_id
        # Per-table state — will be merged into spectator snapshot in _update_spectator().
        self.sb_player_id = active_agents[0].player_id
        self.bb_player_id = active_agents[1].player_id if len(active_agents) > 1 else None
        self.btn_player_id = btn_id
        self.last_actions = {}
        self.showdown_result = None

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
                    await self._tg_send(f"⏱️ @{agent.username} timed out — auto-check.")
                else:
                    action = "fold"
                    log.warning("[T%d] Timeout: @%s auto-folded", self.table_id, agent.username)
                    await self._tg_send(f"⏱️ @{agent.username} timed out — auto-fold.")
                self.last_actions[self._pending_player_id] = f"{action} (timeout)"
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
                await self._tg_send(f"❌ {error_text}")
                agent = self._by_player_id.get(self._pending_player_id)
                if agent:
                    p = self.engine.players[self.engine.current_idx]
                    prompt_ev = self.engine._dealer_prompt(p)
                    await self._send_action_request(agent, prompt_ev.data["text"])
                continue

            await self._dispatch_events(events)

            # Spectator mode: pause after each action so humans can follow
            with _spectator_lock:
                delay = _spectator_state["action_delay"] if _spectator_state["spectator_mode"] else 0.0
            if delay > 0:
                await asyncio.sleep(delay)

        log.info("[T%d] Round %d complete. Final state: %s",
                 self.table_id, self.engine.round_number, self.engine.state.value)

        # Send one batched round summary to Telegram
        if self._tg_buffer:
            stacks_line = "Stacks: " + " | ".join(
                f"@{a.username}: {a.stack}" for a in self.agents
            )
            self._tg_buffer.append(stacks_line)
            await self._tg_send("\n".join(self._tg_buffer))
            self._tg_buffer.clear()

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
            "table_id": self.table_id,
            "round": self.engine.round_number,
            "players": players_final,
        })

        # NOTE: The coordinator tick is fired by _run_table_loop AFTER stack
        # synchronization — firing it here would wake the coordinator on stale
        # AgentInfo.stack values (still showing last hand's totals), which
        # would cause incorrect consolidation decisions.

    async def _dispatch_events(self, events: list[GameEvent]):
        """Route GameEvents to appropriate Telegram chats and WS connections."""
        for ev in events:
            if ev.type == "hand_dealt":
                await self._send_hole_cards(ev)

            elif ev.type == "dealer_message":
                target = ev.data.get("target")
                text = ev.data.get("text", "")
                if target == "all":
                    # Buffer for batched round summary; still broadcast via WS immediately
                    self._tg_buffer.append(text)
                    ws_event = self._parse_dealer_text_to_event(text)
                    if ws_event.get("street"):
                        self.last_actions = {}
                    await self._ws_broadcast(ws_event)
                    self._update_spectator(text)
                else:
                    agent = self._by_player_id.get(int(target))
                    if agent:
                        await self._send_action_request(agent, text)
                        self._update_spectator()

            elif ev.type == "showdown":
                showdown_text = self._format_showdown(ev)
                self._tg_buffer.append(showdown_text)
                winner_id = ev.data.get("winner_id")
                winner_agent = self._by_player_id.get(winner_id)
                await self._ws_broadcast({
                    "type": "showdown",
                    "table_id": self.table_id,
                    "winner": winner_agent.username if winner_agent else f"player_{winner_id}",
                    "winner_id": winner_id,
                    "pot": ev.data.get("pot"),
                    "hands": ev.data.get("hands", []),
                    "reason": ev.data.get("reason"),
                })
                await self._send_showdown(ev)  # sets rich showdown_result for spectator overlay
                self._update_spectator()

            # Event-dispatch pacing: only slow down in spectator mode.
            # Without this guard, 30 bots × many events per hand = 40-minute turniament.
            with _spectator_lock:
                in_spec_mode = _spectator_state.get("spectator_mode", False)
            if in_spec_mode:
                await asyncio.sleep(0.3)

    def _parse_dealer_text_to_event(self, text: str) -> dict:
        """Convert [DEALER] text to structured WS event."""
        import re
        event = {"type": "event", "table_id": self.table_id, "text": text}

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
        """Send a message to all WS-connected bots at this table.
        Delegates to DealerBot._broadcast_to_players when a dealer ref is available;
        falls back to the legacy per-table inline loop (used by MiniDealer-based tests).
        """
        usernames = [a.username for a in self.agents]
        if self._dealer is not None:
            await self._dealer._broadcast_to_players(usernames, msg)
            return
        # Legacy fallback: no dealer back-ref (e.g. test MiniDealer)
        raw = json.dumps(msg)
        for u in usernames:
            ws = self._ws.get(u.lower())
            if ws:
                try:
                    await ws.send(raw)
                except Exception:
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
                    "table_id": self.table_id,
                    "round": round_num,
                    "hole_cards": cards,
                }))
                return
            except websockets.ConnectionClosed:
                pass

        # Telegram fallback
        with _spectator_lock:
            tg_on = self.bot and _spectator_state.get("tg_logging", False)
        if tg_on:
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
            player_total = (player.stack + player.street_bet) if player else 0

            # NLHE rules:
            #  - A full raise (≥ min_raise) is always legal if player has enough.
            #  - A short-stack all-in is legal even below min_raise.
            # We advertise only the legal range.
            can_raise_full = player_total >= min_raise
            can_all_in = player_total > max_bet  # enough to be a raise at all

            valid_actions: list[str] = []
            if to_call > 0:
                valid_actions.append("fold")
                valid_actions.append(f"call {to_call}")
            else:
                valid_actions.append("check")
            if can_raise_full:
                valid_actions.append(f"raise {min_raise}-{player_total}")
            elif can_all_in:
                # Short-stack all-in only (cannot make a full min_raise)
                valid_actions.append(f"raise {player_total} (all-in)")

            # Build players list with stacks and status
            players_info = []
            for p in self.engine.players:
                a = self._by_player_id.get(p.id)
                players_info.append({
                    "id": p.id,
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
        with _spectator_lock:
            tg_on = self.bot and _spectator_state.get("tg_logging", False)
        if tg_on:
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

        # Per-table snapshot (authoritative for multi-table overview / focused-table views)
        table_snapshot = {
            "table_id": self.table_id,
            "game_state": eng.state.value,
            "round_number": eng.round_number,
            "street": eng.state.value,
            "pot": eng.pot,
            "community_cards": community,
            "players": players_out,
            "current_player": self._pending_player_id,
            "blinds": {"sb": eng.small_blind, "bb": eng.big_blind},
            "sb_player_id": self.sb_player_id,
            "bb_player_id": self.bb_player_id,
            "btn_player_id": self.btn_player_id,
            "last_actions": dict(self.last_actions),
            "showdown_result": self.showdown_result,
        }

        with _spectator_lock:
            # Per-table state (authoritative)
            tables_state = _spectator_state.setdefault("tables", {})
            if not isinstance(tables_state, dict):
                tables_state = {}
                _spectator_state["tables"] = tables_state
            tables_state[str(self.table_id)] = table_snapshot

            # Flat fields — populated ONLY in single-table mode so the legacy UI
            # (control.html, single-table spectator fallback) keeps working.
            # In multi-table mode, UI must read from state["tables"][tid].
            is_single = int(_spectator_state.get("table_count", 0)) <= 1
            if is_single:
                _spectator_state["game_state"] = eng.state.value
                _spectator_state["round_number"] = eng.round_number
                _spectator_state["street"] = eng.state.value
                _spectator_state["pot"] = eng.pot
                _spectator_state["community_cards"] = community
                _spectator_state["players"] = players_out
                _spectator_state["current_player"] = self._pending_player_id
                _spectator_state["blinds"] = {"sb": eng.small_blind, "bb": eng.big_blind}
                _spectator_state["sb_player_id"] = self.sb_player_id
                _spectator_state["bb_player_id"] = self.bb_player_id
                _spectator_state["btn_player_id"] = self.btn_player_id
                _spectator_state["last_actions"] = dict(self.last_actions)
                _spectator_state["showdown_result"] = self.showdown_result

            _spectator_state["timestamp"] = int(time.time())
            if event_text:
                prefix = f"[T{self.table_id}] " if not is_single else ""
                _spectator_state["recent_events"].append(f"{prefix}{event_text}")
                _spectator_state["recent_events"] = _spectator_state["recent_events"][-30:]

    def _format_showdown(self, ev: GameEvent) -> str:
        reason      = ev.data.get("reason", "showdown")
        hands       = ev.data.get("hands", [])
        pot_results = ev.data.get("pots", [])
        pot         = ev.data["pot"]
        winner_ids  = ev.data.get("winner_ids", [])

        def _name(pid: int) -> str:
            a = self._by_player_id.get(pid)
            return f"@{a.username}" if a else f"Player {pid}"

        lines = []

        # Winner announcement
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

        # Cards + combinations (only when players actually go to showdown)
        if reason == "showdown" and hands:
            from core.evaluator import cards_to_str
            community = cards_to_str(self.engine.community) if self.engine.community else []
            if community:
                lines.append(f"Board: {' '.join(community)}")
            for h in hands:
                pid   = h["player_id"]
                name  = _name(pid)
                cards = " ".join(h.get("hole_cards", []))
                rank  = h.get("rank", "—")
                mark  = "⭐" if pid in winner_ids else "  "
                lines.append(f"{mark} {name}: {cards} — {rank}")

        return "\n".join(lines)

    async def _send_showdown(self, ev: GameEvent):
        result_text = self._format_showdown(ev)
        from core.evaluator import cards_to_str
        community = cards_to_str(self.engine.community) if self.engine.community else []
        players_map = {
            str(p.id): (self._by_player_id.get(p.id).username if self._by_player_id.get(p.id) else f"Player {p.id}")
            for p in self.engine.players
        }
        snapshot = {
            "text": result_text,
            "timestamp": int(time.time()),
            "reason": ev.data.get("reason"),
            "hands": ev.data.get("hands", []),
            "pots": ev.data.get("pots", []),
            "total_won": {str(k): v for k, v in ev.data.get("total_won", {}).items()},
            "winner_id": ev.data.get("winner_id"),
            "community": community,
            "players_map": players_map,
        }
        self.showdown_result = snapshot
        with _spectator_lock:
            is_single = int(_spectator_state.get("table_count", 0)) <= 1
            if is_single:
                _spectator_state["showdown_result"] = snapshot

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

        # Active tables (multi-table): table_id -> TableSession
        self.tables: dict[int, TableSession] = {}
        # Per-table asyncio tasks
        self._table_tasks: dict[int, asyncio.Task] = {}
        # Main tournament coordinator task (spawns/joins table tasks)
        self._tournament_task: asyncio.Task | None = None
        # Global round counter across ALL tables (drives blind schedule)
        self._global_round_count: int = 0
        # Active table size for current tournament (frozen at start)
        self._table_size: int = TABLE_SIZE_DEFAULT

        # Will be set after Application is built
        self.bot: Bot | None = None

        # WebSocket connections: username → ws
        self.ws_connections: dict[str, websockets.WebSocketServerProtocol] = {}
        # Per-bot auth tokens: username → token (issued at registration)
        self._ws_tokens: dict[str, str] = {}

        # Track which players have had their elimination announced (by player_id).
        # Cleared on tournament start/stop.
        self._eliminated_announced: set[int] = set()

        # Explicit tournament lifecycle state (see TournamentState enum).
        # Use the self.tournament_state property to set — it records history.
        self._tournament_state_value: TournamentState = TournamentState.IDLE
        self._state_history: list[TournamentState] = [TournamentState.IDLE]

        # Set by tables after each hand completes. Coordinator waits on this in
        # addition to table-task completion, so it can proactively consolidate
        # to a final table when the field fits without waiting for a table to die.
        self._coordinator_tick = asyncio.Event()

    @property
    def _tournament_state(self) -> TournamentState:
        return self._tournament_state_value

    @_tournament_state.setter
    def _tournament_state(self, value: TournamentState) -> None:
        if value != self._tournament_state_value:
            self._state_history.append(value)
            log.info("Tournament state: %s → %s",
                     self._tournament_state_value.value, value.value)
        self._tournament_state_value = value

    def _find_table_for_user(self, username: str) -> "TableSession | None":
        """Return the TableSession where this username is a player."""
        username_lc = username.lower()
        for t in self.tables.values():
            for agent in t.agents:
                if agent.username.lower() == username_lc:
                    return t
        return None

    def _is_game_active(self) -> bool:
        """Tournament is active unless we are IDLE or COMPLETE.
        Avoids inspecting task states, which flicker during scheduling windows."""
        return self._tournament_state not in (TournamentState.IDLE, TournamentState.COMPLETE)

    # ------------------------------------------------------------------
    # WS broadcast helpers
    # ------------------------------------------------------------------

    async def _broadcast_global(self, msg: dict) -> None:
        """Send a message to every connected WS client."""
        raw = json.dumps(msg)
        for ws in list(self.ws_connections.values()):
            try:
                await ws.send(raw)
            except Exception:
                pass

    async def _broadcast_to_players(self, usernames, msg: dict) -> None:
        """Send a message to every WS in `usernames` (case-insensitive match)."""
        raw = json.dumps(msg)
        for u in usernames:
            ws = self.ws_connections.get(u.lower())
            if ws:
                try:
                    await ws.send(raw)
                except Exception:
                    pass

    async def _send_to_player(self, username: str, msg: dict) -> None:
        """Send a message to a single WS by username (no-op if not connected)."""
        ws = self.ws_connections.get(username.lower())
        if not ws:
            return
        try:
            await ws.send(json.dumps(msg))
        except Exception:
            pass

    def _live_ws_usernames(self) -> set[str]:
        """Return usernames whose WS socket is still open.
        Filters out dead mappings that the per-connection cleanup hasn't processed yet
        (e.g., bot disconnected just before /startgame)."""
        live: set[str] = set()
        for username, ws in list(self.ws_connections.items()):
            # websockets v12+: ws.state == 1 is OPEN; older API: ws.open
            is_open = getattr(ws, "open", None)
            if is_open is None:
                # Fallback: no reliable attribute — assume open (worst case is a
                # duplicate disconnect detected at first send, which is handled)
                is_open = True
            if is_open:
                live.add(username)
        return live

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
                    # On failed register (duplicate, bad invite), _ws_register
                    # returns None. Keep the previous `username` so the finally
                    # block still cleans up — otherwise the duplicate's close
                    # would leak a stale ws in ws_connections indefinitely.
                    registered = await self._ws_register(ws, msg)
                    if registered:
                        username = registered

                elif msg_type == "action":
                    await self._ws_action(ws, username, msg)

                else:
                    await ws.send(json.dumps({"type": "error", "text": f"unknown type: {msg_type}"}))

        except websockets.ConnectionClosed:
            pass
        finally:
            # Only drop the mapping if it still points at OUR socket.
            # On reconnect, the new handler has already overwritten
            # ws_connections[username] with a fresh ws — we must not delete
            # the new one when our old socket closes.
            if username and self.ws_connections.get(username) is ws:
                del self.ws_connections[username]
                log.info("WS disconnected: %s (%d online)", username, len(self.ws_connections))
                with _spectator_lock:
                    _spectator_state["ws_players"] = list(self.ws_connections.keys())

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
        with _spectator_lock:
            _spectator_state["ws_players"] = list(self.ws_connections.keys())
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
        # Socket ownership: reject if this socket isn't the current mapping for
        # `username`. Prevents a stale/hijacked socket from acting after reconnect.
        if self.ws_connections.get(username) is not ws:
            await ws.send(json.dumps({
                "type": "error",
                "text": "stale socket — re-register to act",
            }))
            return
        if not self.tables:
            await ws.send(json.dumps({"type": "error", "text": "no active game"}))
            return

        action = (msg.get("action") or "").lower().strip()
        # Guard int() against non-numeric payloads (string, float, null, etc.)
        raw_amount = msg.get("amount", 0)
        try:
            amount = int(raw_amount) if raw_amount is not None else 0
        except (ValueError, TypeError):
            await ws.send(json.dumps({
                "type": "error",
                "text": f"invalid amount: {raw_amount!r} (must be integer)",
            }))
            return
        if action not in ("fold", "check", "call", "raise"):
            await ws.send(json.dumps({"type": "error", "text": f"invalid action: {action}"}))
            return

        # Find the table this user belongs to
        target_table = self._find_table_for_user(username)
        if not target_table:
            await ws.send(json.dumps({"type": "error", "text": "you are not seated at any table"}))
            return

        # Mandatory turn_id: reject missing / non-integer outright.
        raw_turn_id = msg.get("turn_id")
        if raw_turn_id is None:
            await ws.send(json.dumps({
                "type": "error",
                "text": "missing turn_id (every action MUST echo the turn's turn_id)",
            }))
            return
        try:
            msg_turn_id = int(raw_turn_id)
        except (ValueError, TypeError):
            await ws.send(json.dumps({
                "type": "error",
                "text": f"invalid turn_id: {raw_turn_id!r} (must be integer)",
            }))
            return

        accepted = target_table.accept_action(username, action, amount, turn_id=msg_turn_id)
        if not accepted:
            # Distinguish stale turn from wrong-player
            if (msg_turn_id is not None
                    and target_table._pending_player_id is not None
                    and msg_turn_id != target_table._turn_id):
                text = f"stale turn_id {msg_turn_id} (current: {target_table._turn_id})"
            else:
                text = "not your turn"
            await ws.send(json.dumps({"type": "error", "text": text}))

    # ------------------------------------------------------------------
    # Public command handlers
    # ------------------------------------------------------------------

    async def cmd_startgame(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != MAIN_GROUP_ID:
            return
        if not _is_admin(update):
            await update.message.reply_text("⚠️ Only admins can start the game.")
            return
        if self._is_game_active():
            await update.message.reply_text("❌ A tournament is already in progress.")
            return

        # Build roster from WS-connected bots (primary) + Telegram-registered (fallback)
        ws_usernames = self._live_ws_usernames()
        tg_usernames = {a.username.lower() for a in self.agents}

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

        self._tournament_state = TournamentState.STARTING
        agents = self._build_tournament_agents(sorted(player_usernames))
        table_size, num_tables = self._seat_and_init_tables(agents)

        sb, bb = get_blinds(1)
        players_str = ", ".join(f"@{a.username}" for a in agents)
        log.info("=== TOURNAMENT START: %d players (%s), %d table(s) of %d ===",
                 len(agents), source, num_tables, table_size)
        await update.message.reply_text(
            f"🏆 AICollective AI Agents Poker — Tournament\n"
            f"Players: {players_str}\n"
            f"Tables: {num_tables} × {table_size}\n"
            f"Starting stack: {STARTING_STACK} chips\n"
            f"Blinds: {sb}/{bb}"
        )
        self._tournament_state = TournamentState.RUNNING
        self._tournament_task = asyncio.create_task(self._run_tournament())

    async def cmd_stopgame(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != MAIN_GROUP_ID:
            return
        if not _is_admin(update):
            await update.message.reply_text("⚠️ Only admins can stop the game.")
            return

        await self._reset_tournament_state()
        log.info("=== TOURNAMENT STOPPED by @%s ===",
                 update.effective_user.username if update.effective_user else "unknown")

        await update.message.reply_text(
            "🛑 Tournament stopped. Send /startgame to start a new one."
        )

    async def _stop_all_tables(self):
        """Cancel all table tasks and tournament coordinator, stop all TableSessions.
        Awaits cancellations to ensure old tasks cannot emit into a new tournament.
        Self-aware: if called from within the coordinator task itself, does NOT
        cancel that task (would deadlock)."""
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None

        # Collect tasks to await (only those we actually cancel)
        to_await: list[asyncio.Task] = []
        if (
            self._tournament_task
            and not self._tournament_task.done()
            and self._tournament_task is not current
        ):
            self._tournament_task.cancel()
            to_await.append(self._tournament_task)
        for task in self._table_tasks.values():
            if not task.done() and task is not current:
                task.cancel()
                to_await.append(task)

        # Unblock any in-flight _wait_for_action so cancellation can propagate cleanly
        for t in self.tables.values():
            t.stop()
            t.engine.state = GameState.WAITING

        # Await cancellations so old tasks fully unwind before we clear state.
        # Log any teardown exceptions (CancelledError is expected; others are bugs).
        if to_await:
            results = await asyncio.gather(*to_await, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                    log.warning("Teardown exception during _stop_all_tables: %r", r)

        self._table_tasks.clear()
        if self._tournament_task is not current:
            self._tournament_task = None

    async def _reset_tournament_state(self):
        """Unified reset: cancel tasks, clear tables/eliminations/blinds, reset
        spectator state to 'waiting', transition back to IDLE.
        Safe to call from TG stop path, HTTP stop path, or coordinator failure handler."""
        await self._stop_all_tables()
        self.tables.clear()
        self._global_round_count = 0
        self._eliminated_announced.clear()
        self._state_history = [TournamentState.IDLE]
        self._tournament_state = TournamentState.IDLE
        self._coordinator_tick.clear()
        with _spectator_lock:
            _spectator_state["game_state"] = "waiting"
            _spectator_state["street"] = "waiting"
            _spectator_state["pot"] = 0
            _spectator_state["community_cards"] = []
            _spectator_state["players"] = []
            _spectator_state["current_player"] = None
            _spectator_state["showdown_result"] = None
            _spectator_state["last_actions"] = {}
            _spectator_state["tables"] = {}
            _spectator_state["table_count"] = 0
            _spectator_state["timestamp"] = int(time.time())

    # ------------------------------------------------------------------
    # Multi-table helpers
    # ------------------------------------------------------------------

    def _build_tournament_agents(self, usernames: list[str]) -> list[AgentInfo]:
        """Create AgentInfo objects for all tournament players, freezing roster."""
        self._tournament_agents = []
        for pid, username in enumerate(usernames, start=1):
            tg_agent = self._by_username.get(username)
            self._tournament_agents.append(AgentInfo(
                player_id=pid,
                username=username,
                private_group_id=tg_agent.private_group_id if tg_agent else 0,
                stack=STARTING_STACK,
                ready_message_id=tg_agent.ready_message_id if tg_agent else None,
            ))
        return self._tournament_agents

    def _seat_and_init_tables(self, agents: list[AgentInfo]) -> tuple[int, int]:
        """Randomly seat agents across N tables. Creates TableSession for each.
        Returns (table_size, num_tables).
        """
        with _spectator_lock:
            table_size = max(TABLE_SIZE_MIN, min(int(_spectator_state.get("table_size", TABLE_SIZE_DEFAULT)), TABLE_SIZE_MAX))
        self._table_size = table_size

        n = len(agents)
        num_tables = max(1, math.ceil(n / table_size))
        # But don't create more tables than needed for even distribution
        # Prefer fuller tables: ceil(n / table_size) is usually right

        # Random seating
        shuffled = list(agents)
        random.shuffle(shuffled)

        # Distribute round-robin to balance table sizes
        seats_per_table: dict[int, list[AgentInfo]] = {i + 1: [] for i in range(num_tables)}
        for i, agent in enumerate(shuffled):
            tid = (i % num_tables) + 1
            seats_per_table[tid].append(agent)

        # Create TableSessions
        self.tables = {}
        self._table_tasks.clear()
        self._global_round_count = 0
        self._eliminated_announced.clear()
        self._state_history = [self._tournament_state_value]
        for tid, seated in seats_per_table.items():
            self.tables[tid] = TableSession(
                table_id=tid, agents=seated, bot=self.bot,
                ws_connections=self.ws_connections,
                dealer=self,
            )
            log.info("[T%d] seated: %s", tid, [a.username for a in seated])

        with _spectator_lock:
            _spectator_state["table_count"] = len(self.tables)
            _spectator_state["tables"] = {
                str(tid): {"players": [a.username for a in t.agents]}
                for tid, t in self.tables.items()
            }

        return table_size, num_tables

    async def _notify_tournament_start(self):
        """Send tournament_start message to each WS bot with their table assignment."""
        for tid, table in self.tables.items():
            for agent in table.agents:
                await self._send_to_player(agent.username, {
                    "type": "tournament_start",
                    "players": len(self._tournament_agents),
                    "tables": len(self.tables),
                    "your_table": tid,
                })

    # ------------------------------------------------------------------
    # HTTP control API (called from spectator server thread via run_coroutine_threadsafe)
    # ------------------------------------------------------------------

    async def trigger_startgame(self) -> dict:
        ws_usernames = self._live_ws_usernames()
        if len(ws_usernames) < 2:
            return {"ok": False, "error": f"Need at least 2 players, {len(ws_usernames)} connected"}
        if self._is_game_active():
            return {"ok": False, "error": "Tournament already in progress"}

        self._tournament_state = TournamentState.STARTING
        agents = self._build_tournament_agents(sorted(ws_usernames))
        table_size, num_tables = self._seat_and_init_tables(agents)

        sb, bb = get_blinds(1)
        log.info("=== TOURNAMENT START via HTTP: %d players, %d table(s) of %d ===",
                 len(agents), num_tables, table_size)
        self._tournament_state = TournamentState.RUNNING
        self._tournament_task = asyncio.create_task(self._run_tournament())
        return {
            "ok": True,
            "players": [a.username for a in agents],
            "tables": num_tables,
            "table_size": table_size,
            "blinds": f"{sb}/{bb}",
            "starting_stack": STARTING_STACK,
        }

    async def trigger_stopgame(self) -> dict:
        log.info("=== TOURNAMENT STOPPED via HTTP ===")
        await self._reset_tournament_state()
        return {"ok": True}

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
        if not self.tables:
            await update.message.reply_text("Game not started. Use /startgame.")
            return

        lines = [f"Tournament: {len(self.tables)} active table(s)"]
        for tid, table in sorted(self.tables.items()):
            state = table.engine.public_state()
            lines.append(f"\n── Table {tid} ({state['state']}, pot {state['pot']}) ──")
            if state["community_cards"]:
                lines.append(f"Board: {' '.join(state['community_cards'])}")
            for p in state["players"]:
                agent = table._by_player_id.get(p["id"])
                name = f"@{agent.username}" if agent else f"Player {p['id']}"
                marker = " ◀ turn" if state["active_player"] == p["id"] else ""
                lines.append(f"  {name}: stack={p['stack']} bet={p['street_bet']} [{p['status']}]{marker}")

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
        """Shared validation + dispatch for all action commands. Routes to correct table."""
        msg = update.message
        if not msg or not self.tables:
            return
        sender_username = (msg.from_user.username or "").lower() if msg.from_user else ""

        target = self._find_table_for_user(sender_username)
        if not target:
            await msg.reply_text("⚠️ You are not in this tournament.")
            return

        if not target.accept_action(sender_username, action, amount):
            await msg.reply_text("⚠️ It's not your turn.")

    async def on_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Fallback handler for human players not using /bet.
        Accepts plain text like '/call', '/raise 100' as a reply to dealer's message.
        """
        msg = update.message
        if not msg or not self.tables:
            return

        sender_username = (msg.from_user.username or "").lower() if msg.from_user else ""
        target = self._find_table_for_user(sender_username)
        if not target or not target._pending_player_id:
            return
        if not msg.reply_to_message or msg.reply_to_message.message_id != target._pending_message_id:
            return

        action, amount = _parse_command(msg.text or "")
        if not action:
            await msg.reply_text(
                "❌ Invalid command. Use /fold | /check | /call | /raise <amount>"
            )
            return

        if not target.accept_action(sender_username, action, amount):
            await msg.reply_text("⚠️ It's not your turn.")

    # ------------------------------------------------------------------
    # Tournament orchestration
    # ------------------------------------------------------------------

    async def _run_tournament(self):
        """Multi-table tournament coordinator.
        Starts one asyncio task per table. Handles table breaking, blind clock,
        and final table consolidation. Runs until one player remains across all tables.
        """
        try:
            agents = self._tournament_agents

            # Notify WS bots of their table assignment
            await self._notify_tournament_start()

            # Set initial blinds on all tables
            current_sb, current_bb = get_blinds(1)
            for t in self.tables.values():
                t.engine.set_blinds(current_sb, current_bb)

            # Start one task per table
            for tid, table in self.tables.items():
                task = asyncio.create_task(self._run_table_loop(tid))
                self._table_tasks[tid] = task

            # Main coordinator loop. Wakes on either (a) a table task finishing
            # OR (b) any table signaling end-of-hand via _coordinator_tick.
            # (b) is critical so consolidation fires when the field fits one
            # table, not only when someone drops to <=1 at a specific table.
            while self._table_tasks:
                tick_task = asyncio.create_task(self._coordinator_tick.wait())
                try:
                    done, _pending = await asyncio.wait(
                        [*self._table_tasks.values(), tick_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not tick_task.done():
                        tick_task.cancel()
                # Reset the tick so subsequent hand-completions wake us again.
                self._coordinator_tick.clear()

                # Detect crashed tables FIRST — integrity critical. A crashed
                # table task must not be treated as a clean exit, or the
                # coordinator could crown a false winner.
                for task in done:
                    if task is tick_task or task.cancelled():
                        continue
                    exc = task.exception()
                    if exc is not None:
                        log.error("Table task crashed with %s — aborting tournament",
                                  type(exc).__name__)
                        raise exc

                # Remove completed table tasks from dict (tick_task was temporary)
                for task in done:
                    if task is tick_task:
                        continue
                    finished_tid = next(
                        (tid for tid, t in self._table_tasks.items() if t is task), None
                    )
                    if finished_tid is not None:
                        self._table_tasks.pop(finished_tid, None)

                # Re-evaluate: break dead tables, form final table if field fits.
                await self._handle_table_breaking()

                # If we've consolidated to one table that's still running, continue
                # the wait. If all tables are done and no survivors → tournament over.
                alive = [a for a in agents if a.stack > 0]
                if len(alive) <= 1:
                    break

            # Tournament over — mark COMPLETE before writing the spectator signal so
            # a poll racing with this transition sees _is_game_active() == False.
            self._tournament_state = TournamentState.COMPLETE
            alive = [a for a in agents if a.stack > 0]
            winner = alive[0] if alive else max(agents, key=lambda a: a.stack)
            log.info("=== TOURNAMENT OVER. Winner: @%s ===", winner.username)
            await _send(self.bot, MAIN_GROUP_ID,
                        f"🏆 Tournament over!\n@{winner.username} wins with {winner.stack} chips!")
            await self._broadcast_global({
                "type": "tournament_over",
                "winner": winner.username,
                "winner_id": winner.player_id,
                "stack": winner.stack,
            })
            with _spectator_lock:
                _spectator_state["game_state"] = "tournament_over"
                _spectator_state["winner"] = winner.username
                _spectator_state["timestamp"] = int(time.time())
            for t in self.tables.values():
                t.engine.state = GameState.WAITING

        except asyncio.CancelledError:
            log.info("Tournament coordinator cancelled")
            # State will be reset to IDLE by the stop path that cancelled us
            raise
        except Exception as e:
            log.error("Tournament crashed: %s", e, exc_info=True)
            try:
                await _send(self.bot, MAIN_GROUP_ID, f"❌ Tournament error: {e}")
            except Exception:
                pass
        finally:
            # Full reset on ANY exit path (clean completion, cancel, crash).
            # Mirrors trigger_stopgame — prevents stale self.tables /
            # _tournament_agents / spectator_state from leaking into next tournament.
            for task in list(self._table_tasks.values()):
                if not task.done():
                    task.cancel()
            if self._table_tasks:
                results = await asyncio.gather(
                    *self._table_tasks.values(), return_exceptions=True
                )
                for r in results:
                    if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                        log.error("Residual table task raised during shutdown: %r", r)
                self._table_tasks.clear()

            # Clear only state that would interfere with a restart. Keep diagnostic
            # state (_eliminated_announced, _tournament_agents) for post-tournament
            # inspection by tests / the next /status query — it's cleared on the
            # next /startgame via _seat_and_init_tables, and on /stopgame via
            # _reset_tournament_state.
            self.tables.clear()
            self._tournament_state = TournamentState.IDLE
            with _spectator_lock:
                _spectator_state["table_count"] = 0
                # Keep `tables`, `winner`, `game_state` as set by the success path
                # so spectators see the final "tournament_over" snapshot. These get
                # cleared by the next /startgame or /stopgame.

    async def _run_table_loop(self, table_id: int):
        """Run rounds at one table until ≤1 player remains at that table OR
        the tournament coordinator decides to break it.
        """
        table = self.tables.get(table_id)
        if not table:
            return
        agents = table.agents  # live reference — players can be added/removed
        dealer_idx = 0

        try:
            while True:
                # Graceful exit: coordinator set the flag AND we just finished a round —
                # stop cleanly without cancelling mid-hand.
                if table._stop_after_round:
                    log.info("[T%d] table exiting gracefully (stop_after_round)", table_id)
                    break
                # Re-read agents each iteration — may have been mutated by breaking logic
                active = [a for a in table.agents if a.stack > 0]
                if len(active) < 2:
                    log.info("[T%d] table exiting — %d active player(s)", table_id, len(active))
                    break

                # Shared blind clock across all tables. Monotonic: we only ever
                # increase it, so breaking the fastest table doesn't regress blinds.
                table_round = table.engine.round_number + 1  # about to play this round
                live_max = max(
                    [t.engine.round_number for t in self.tables.values()] + [table_round]
                )
                self._global_round_count = max(self._global_round_count, live_max)
                new_sb, new_bb = get_blinds(self._global_round_count)
                if (new_sb, new_bb) != (table.engine.small_blind, table.engine.big_blind):
                    table.engine.set_blinds(new_sb, new_bb)

                n = len(active)
                sb_pos = dealer_idx % n
                rotated = active[sb_pos:] + active[:sb_pos]

                await table.run_single_round(rotated, new_sb, new_bb)
                dealer_idx += 1

                # Sync stacks engine → agents. MUST happen before the coordinator
                # tick so the coordinator sees authoritative state.
                for p in table.engine.players:
                    agent = table._by_player_id.get(p.id)
                    if agent:
                        agent.stack = p.stack

                # Signal the coordinator: a hand just finished WITH committed
                # stacks. It will re-evaluate consolidation / breaking decisions.
                self._coordinator_tick.set()

                # Pause between rounds
                with _spectator_lock:
                    pause = _spectator_state["round_pause"] if _spectator_state["spectator_mode"] else 0.0
                if pause > 0:
                    await asyncio.sleep(pause)
        except asyncio.CancelledError:
            log.info("[T%d] table task cancelled", table_id)
            raise
        except Exception as e:
            # A table crash is integrity-critical — coordinator must NOT treat
            # this as a clean exit and crown a false winner. Re-raise so the
            # coordinator's exception handler aborts the tournament cleanly.
            log.error("[T%d] table crashed: %s", table_id, e, exc_info=True)
            raise

    async def _handle_table_breaking(self):
        """After a table finishes, move its survivors to other tables.
        Also forms a final table when remaining players ≤ table_size.
        """
        agents = self._tournament_agents
        all_alive = [a for a in agents if a.stack > 0]

        # Announce eliminations for players who just went out
        for a in agents:
            if a.stack == 0 and a.player_id not in self._eliminated_announced:
                self._eliminated_announced.add(a.player_id)
                await _send(self.bot, MAIN_GROUP_ID, f"💀 @{a.username} is eliminated!")
                await self._send_to_player(a.username, {
                    "type": "eliminated",
                    "place": len([x for x in agents if x.stack == 0]),
                    "players_left": len(all_alive),
                })

        # ── Decide the consolidation plan UP-FRONT, before any mutation ────────
        # Computing `already_single` AFTER popping broken tables was a bug: when
        # the penultimate table breaks, len(self.tables) drops to 1 and code
        # thinks "already consolidated" when in fact the survivor still needs
        # to be moved to the final table. Snapshot now.
        forming_final_table = (
            len(all_alive) >= 2
            and len(all_alive) <= self._table_size
            and len(self.tables) > 1
        )

        # Find tables that are done (≤1 active player) — their survivors need relocating
        to_break: list[int] = []
        for tid, table in list(self.tables.items()):
            alive = [a for a in table.agents if a.stack > 0]
            if tid not in self._table_tasks and len(alive) <= 1:
                to_break.append(tid)

        for tid in to_break:
            table = self.tables[tid]
            survivors = [a for a in table.agents if a.stack > 0]

            if forming_final_table:
                # Hold survivors — the final-table block below will seat them.
                # Don't relocate to a doomed table whose task is about to be shut down.
                pass
            elif survivors:
                # Move each survivor to the smallest OTHER active table
                for survivor in survivors:
                    candidate_tids = [
                        t for t in self.tables
                        if t != tid and t in self._table_tasks
                    ]
                    if not candidate_tids:
                        continue
                    target_tid = min(
                        candidate_tids,
                        key=lambda t: len([a for a in self.tables[t].agents if a.stack > 0])
                    )
                    target = self.tables[target_tid]
                    target.agents.append(survivor)
                    target._by_player_id[survivor.player_id] = survivor
                    await _send(self.bot, MAIN_GROUP_ID,
                                f"🔀 @{survivor.username} moved to Table {target_tid}")
                    log.info("Moved @%s from T%d to T%d", survivor.username, tid, target_tid)
                    await self._send_to_player(survivor.username, {
                        "type": "table_change",
                        "new_table": target_tid,
                    })

            # Remove the broken table — from both self.tables AND spectator state
            self.tables.pop(tid, None)
            with _spectator_lock:
                _spectator_state["table_count"] = len(self.tables)
                tables_state = _spectator_state.get("tables", {})
                if isinstance(tables_state, dict):
                    tables_state.pop(str(tid), None)
            log.info("[T%d] table broken", tid)

        # Form final table when the pre-snapshot said we should.
        if forming_final_table:
            # Graceful shutdown: flag tables to exit AFTER their current round
            # completes, not mid-hand (avoids losing all-in pots).
            for tid, t in self.tables.items():
                t._stop_after_round = True
            # Wait for tasks to finish naturally (they'll see the flag at top of
            # next iteration). Gather with return_exceptions so a crash doesn't
            # skip other tasks — but re-raise afterwards so the coordinator
            # aborts instead of silently crowning someone. Integrity > continuity.
            pending = [task for task in self._table_tasks.values() if not task.done()]
            if pending:
                results = await asyncio.gather(*pending, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                        log.error("Table task crashed during consolidation: %r — aborting", r)
                        raise r
            self._table_tasks.clear()
            # Re-sync stacks after last hand played to capture final results
            for t in self.tables.values():
                for p in t.engine.players:
                    agent = t._by_player_id.get(p.id)
                    if agent:
                        agent.stack = p.stack
            # Recompute alive list — survivors may have changed during the last hand
            all_alive = [a for a in agents if a.stack > 0]
            if len(all_alive) < 2:
                # Tournament actually finished during final round — skip final table
                log.info("Tournament finished before final table formation: %d alive",
                         len(all_alive))
                self.tables.clear()
                return

            # Clear old tables, create single final table
            self.tables.clear()
            final_tid = 1
            final = TableSession(
                table_id=final_tid, agents=all_alive, bot=self.bot,
                ws_connections=self.ws_connections,
                dealer=self,
            )
            final.engine.set_blinds(*get_blinds(max(1, self._global_round_count)))
            self.tables[final_tid] = final

            self._tournament_state = TournamentState.FINAL_TABLE
            await _send(self.bot, MAIN_GROUP_ID,
                        f"🎯 Final table! {len(all_alive)} players remaining")
            await self._broadcast_to_players(
                [a.username for a in all_alive],
                {
                    "type": "table_change",
                    "new_table": final_tid,
                    "reason": "final_table",
                },
            )

            with _spectator_lock:
                _spectator_state["table_count"] = 1
                _spectator_state["tables"] = {
                    "1": {"players": [a.username for a in all_alive]}
                }

            # Start the final table loop
            task = asyncio.create_task(self._run_table_loop(final_tid))
            self._table_tasks[final_tid] = task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _send(bot: "Bot | None", chat_id: int, text: str, **kwargs) -> None:
    """Send a message to TG. No-op if TG logging is disabled or bot is None."""
    if bot is None:
        return
    with _spectator_lock:
        enabled = _spectator_state.get("tg_logging", False)
    if not enabled:
        return
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
    """Async entry point — controls the event loop explicitly."""
    # Update spectator state with TG availability
    with _spectator_lock:
        _spectator_state["tg_configured"] = TG_CONFIGURED
        _spectator_state["tg_logging"]    = False  # off by default; enable from control panel
        _spectator_state["table_size"]    = max(TABLE_SIZE_MIN, min(TABLE_SIZE_DEFAULT, TABLE_SIZE_MAX))

    agents = load_agents()
    if not agents:
        log.warning("No pre-configured agents. Waiting for WS connections before /startgame.")
    else:
        log.info("Loaded %d agent(s): %s", len(agents), [a.username for a in agents])

    dealer = DealerBot(agents)

    global _dealer_ref, _loop_ref
    _dealer_ref = dealer
    _loop_ref   = asyncio.get_event_loop()

    _start_spectator_server()

    app = None
    if TG_CONFIGURED:
        app = _build_app(dealer)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("Dealer bot started. Main group: %d", MAIN_GROUP_ID)
    else:
        log.warning("Telegram not configured — running in WebSocket-only mode (no TG bot).")

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
        if app:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
