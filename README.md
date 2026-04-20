# poker-moker

Tournament platform for LLM-driven poker bots. A local **dealer** runs Texas Hold'em over WebSocket (and optionally Telegram), while independent **agent bots** connect, receive game state as JSON, and reply with actions. Designed for multi-table tournaments with arbitrary bot implementations.

---

## Architecture

```
┌────────────────┐   WS JSON    ┌─────────────────┐
│  Agent bots    │ ───────────► │  Dealer         │
│  (any stack)   │ ◄─────────── │  (this repo)    │
└────────────────┘              └─────────────────┘
                                        │
                              ┌─────────┴─────────┐
                              ▼                   ▼
                     [spectator.html]      [control.html]
                       localhost:8765       localhost:8765
```

Components:

- **[core/](core/)** — pure game engine (`engine.py`, `evaluator.py`), no I/O. Uses `treys` for hand ranking.
- **[dealer/](dealer/)** — `dealer_bot.py` orchestrates rounds, blinds, side pots, showdown; `tournament.py` handles multi-table play with table breaking; WS server at `:9000`; spectator HTTP at `:8765`.
- **[agent/](agent/)** — reference LLM-driven agent (`agent_bot.py`) with 3-layer decision: hand tiers → equity → LLM. Pluggable strategy profiles (tight / gto / aggressive).
- **[bot_template/](bot_template/)** — minimal WS bot skeleton for custom implementations (see [docs/ws-bot-guide.md](docs/ws-bot-guide.md)).
- **[bots/](bots/)** — `.env` configs for pre-set personalities (aggressor, balanced, conservative).
- **[launcher.py](launcher.py)** — start/stop/monitor multiple agent instances from one process.
- **spectator.html / control.html** — browser-based live view and tournament control.

---

## Quick start

### 1. Run the dealer

```bash
cd dealer
pip install -r requirements.txt
cp .env.example .env   # fill in Telegram token if using TG mode
python dealer_bot.py
```

Dealer opens:
- WebSocket on `ws://localhost:9000` for bots
- HTTP on `http://localhost:8765` for spectator/control UIs

### 2. Run a bot (WS mode — recommended)

```bash
cd bot_template
pip install websockets
python bot.py --url ws://localhost:9000 --team MyBot --invite POKER-XXXX
```

Replace `decide()` in [bot_template/bot.py](bot_template/bot.py) with your strategy. Full protocol reference: [docs/ws-bot-guide.md](docs/ws-bot-guide.md).

### 3. Run the reference LLM agent

```bash
cd agent
pip install -r requirements.txt
cp env.example .env    # set AGENT_BOT_TOKEN, CODEX_PATH, etc.
python agent_bot.py
```

Supported backends via `CODEX_PATH`: `claude`, `codex`, `gemini` (CLI), or `api` (Anthropic SDK direct).

### 4. Launch multiple bots at once

```bash
cp bots/balanced.env.example bots/balanced.env    # configure each
python launcher.py --all          # start all bots in bots/
python launcher.py balanced       # start one
python launcher.py --list
```

The launcher monitors processes and auto-restarts crashed bots. Logs go to `logs/<bot>_launcher.log`.

---

## Strategy profiles

Set `STRATEGY_PROFILE` in the bot's `.env` (`tight` | `gto` | `aggressive`). Each profile tunes equity thresholds, opening ranges, bet-sizing multipliers, bluff frequency, exploit sensitivity, and LLM personality hint. See [agent/strategy_profiles.py](agent/strategy_profiles.py).

Pre-configured examples:
- **[aggressor](bots/aggressor.env.example)** — loose-aggressive, Claude Sonnet
- **[balanced](bots/balanced.env.example)** — GTO baseline, Codex
- **[conservative](bots/conservative.env.example)** — tight-aggressive, Claude Haiku

---

## WebSocket protocol (summary)

```jsonc
// bot → dealer
{"type": "register", "team": "MyBot", "invite": "POKER-XXXX"}

// dealer → bot
{"type": "registered", "username": "mybot", "token": "abc..."}
{"type": "cards", "round": 5, "hole_cards": ["Ah", "Kd"]}
{"type": "turn", "turn_id": 42, "street": "flop", "pot": 120, "stack": 880,
 "community": ["Ah","7d","3s"], "valid_actions": ["fold","call 20","raise 40-880"],
 "to_call": 20, "min_raise": 40}

// bot → dealer
{"type": "action", "turn_id": 42, "action": "raise", "amount": 100}
```

Full docs: [docs/ws-bot-guide.md](docs/ws-bot-guide.md).

---

## Testing

```bash
cd dealer
python test_dummy_bot.py           # smoke test single bot
python test_ws_integration.py      # WS protocol
python test_e2e_tournament.py      # full multi-bot tournament

cd agent/tests
python -m pytest
```

---

## Layout

```
poker-moker/
├── core/              game engine + hand evaluator (no I/O)
├── dealer/            WS server, tournament director, spectator HTTP
├── agent/             reference LLM agent (3-layer decision engine)
├── bot_template/      minimal WS bot skeleton for custom strategies
├── bots/              .env configs for pre-set bot personalities
├── docs/              protocol reference
├── logs/              per-bot log files (gitignored)
├── launcher.py        multi-bot process manager
├── spectator.html     live game viewer
└── control.html       tournament admin UI
```

---

## Requirements

Python 3.10+. Per-component `requirements.txt` in [dealer/](dealer/requirements.txt) and [agent/](agent/requirements.txt). Bot-template needs only `websockets`.
