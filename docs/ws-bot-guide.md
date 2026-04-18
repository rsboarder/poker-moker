# Poker Bot: WebSocket Integration Guide

## What changed

Dealer now communicates with bots via **WebSocket** (JSON messages) instead of Telegram.
Your bot connects to `ws://host:9000`, receives game state as structured JSON, and replies with actions. No Telegram bot token needed, no BotFather, no groups.

## Quick start

```bash
pip install websockets
python bot.py --team YourTeamName --invite POKER-XXXX --url ws://host:9000
```

A working template is in `bot_template/bot.py`. Copy it, replace the `decide()` function with your strategy.

## Connection flow

```
Bot                              Dealer
 |                                  |
 |  ws.connect("ws://host:9000")    |
 |--------------------------------->|
 |                                  |
 |  {"type": "register",           |
 |   "team": "MyBot",              |
 |   "invite": "POKER-XXXX"}       |
 |--------------------------------->|
 |                                  |
 |  {"type": "registered",         |
 |   "username": "mybot",          |
 |   "token": "abc123..."}         |
 |<---------------------------------|
 |                                  |
 |  (save token for reconnect)      |
 |                                  |
```

Keep the WebSocket connection open. All game communication happens on this single connection.

## Messages you receive

### 1. `cards` — your hole cards (start of each round)

```json
{
  "type": "cards",
  "table_id": 1,
  "round": 5,
  "hole_cards": ["Ah", "Kd"]
}
```

### 2. `turn` — your turn to act (MUST reply)

```json
{
  "type": "turn",
  "turn_id": 42,
  "table_id": 1,
  "round": 5,
  "street": "flop",
  "pot": 360,
  "stack": 970,
  "community": ["9s", "Js", "7d"],
  "hole_cards": ["Ah", "Kd"],
  "position": "BTN",
  "players": [
    {"id": 1, "username": "aggressor",    "stack": 850, "street_bet": 120, "status": "active"},
    {"id": 2, "username": "conservative", "stack": 970, "street_bet": 0,   "status": "active"},
    {"id": 3, "username": "mybot",        "stack": 970, "street_bet": 0,   "status": "active"}
  ],
  "valid_actions": ["fold", "call 120", "raise 240-970"],
  "to_call": 120,
  "min_raise": 240
}
```

Fields:
- `turn_id` — must include in your reply. Unique per table; stale IDs are rejected.
- `table_id` — which table this turn belongs to (relevant in multi-table tournaments; safe to ignore if you don't care)
- `street` — `preflop`, `flop`, `turn`, `river`
- `pot` — current pot size
- `stack` — your remaining chips
- `community` — board cards
- `hole_cards` — your hole cards (included so you don't need to track state)
- `position` — your seat at the current table. Possible values depend on table size:
  - 2 players (heads-up): `SB`, `BB`
  - 3 players: `SB`, `BB`, `BTN`
  - 4 players: `SB`, `BB`, `CO`, `BTN`
  - 5 players: `SB`, `BB`, `UTG`, `CO`, `BTN`
  - 6+ players: `SB`, `BB`, `UTG`, `MP`, `CO`, `BTN`
- `players` — all players at the current table. Each has `id` (player_id — used in `eliminated` and cross-referenced in event broadcasts), `username`, `stack`, `street_bet` (chips put in this street), and `status` (`active` / `folded` / `all_in`).
- `valid_actions` — what you can do (human-readable strings)
- `to_call` — chips needed to call (0 = can check)
- `min_raise` — minimum raise-to amount (total bet after your raise)

### 3. `event` — other players' actions, board updates

Scoped to the table you're sitting at. You will **not** receive events from other tables in a multi-table tournament.

```json
{
  "type": "event",
  "table_id": 1,
  "player": "aggressor",
  "action": "raise",
  "amount": 200,
  "pot": 360,
  "text": "[DEALER] aggressor raises to 200. Pot: 360."
}
```

Board update event:
```json
{
  "type": "event",
  "table_id": 1,
  "street": "flop",
  "community": ["9s", "Js", "7d"],
  "text": "[DEALER] --- FLOP: 9s Js 7d ---"
}
```

The `text` field is always present for backward compatibility. Structured fields (`player`, `action`, `amount`, `street`, `community`) are parsed from the text when available.

**Ordering guarantee:** all `event` messages for a given round arrive **before** the `showdown` and `round_end` messages for that same round. Messages from the dealer are delivered in the order the game produced them — you don't need to handle out-of-order events or reconcile after-the-fact.

### 4. `round_end` — round complete, final stacks

Sent after showdown, before the next round starts. Use this to update opponent tracking.

```json
{
  "type": "round_end",
  "table_id": 1,
  "round": 5,
  "players": [
    {"username": "aggressor", "stack": 640},
    {"username": "conservative", "stack": 1360}
  ]
}
```

`players` lists only the players at **your** table — not all tournament survivors. In a multi-table tournament, don't use this to infer how many players remain overall.

### 5. `showdown` — round result

```json
{
  "type": "showdown",
  "table_id": 1,
  "winner": "conservative",
  "winner_id": 2,
  "pot": 500,
  "hands": [
    {"player_id": 1, "hole_cards": ["Ah", "Kd"], "rank": "Pair"},
    {"player_id": 2, "hole_cards": ["Qs", "Qd"], "rank": "Pair"}
  ],
  "reason": "showdown"
}
```

### 6. `eliminated` — you're out

```json
{"type": "eliminated", "place": 12, "players_left": 18}
```

**Note:** `place` is the number of players eliminated so far (1-indexed), not your finishing position. For an 18-player tournament, `place: 12` means "the 12th player to bust out" — your finishing rank is `total_players - place + 1`. `players_left` is the count still alive after your elimination.

After `eliminated` the WebSocket connection stays open. You simply stop receiving `turn`/`cards` messages. You'll still receive `tournament_over` at the end. Don't reconnect on your own — the dealer doesn't require it.

### 7. `tournament_start` / `tournament_over`

Sent to every WS-connected bot when the tournament begins / ends.

```json
{"type": "tournament_start", "players": 30, "tables": 5, "your_table": 2}
{"type": "tournament_over", "winner": "botname", "winner_id": 2, "stack": 30000}
```

`your_table` is the table you've been seated at initially. You can ignore it — all subsequent `turn`, `cards`, `event`, and `showdown` messages tell you everything you need. Use only if you want to log or debug.

### 8. `table_change` — you've been moved (multi-table only)

When a table runs out of players, survivors are moved to another table. When the tournament consolidates to one final table, every remaining player is moved there.

```json
{"type": "table_change", "new_table": 2}
{"type": "table_change", "new_table": 1, "reason": "final_table"}
```

`reason: "final_table"` is only sent when the final table is formed. Otherwise the field is absent.

You don't need to do anything — the dealer will start sending you `turn` messages from the new table automatically. The `table_id` inside each subsequent `turn` will reflect your new seating.

**Timing guarantee:** `table_change` only arrives **between rounds** — never mid-hand. Your current round always completes at your old table before you are moved. Safe to keep per-round state (equity calculations, action history) right up to `showdown` without fearing a surprise reseat.

**Blind level:** when you move to a new table (including the final table), blinds are carried forward — the next round uses the maximum blind level seen across all tables, not a reset. Your stack-to-pot ratios don't get a fresh start.

### 9. `error` — something went wrong

```json
{"type": "error", "text": "not your turn"}
```

## Messages you send

### Reply to `turn` (required)

```json
{
  "type": "action",
  "turn_id": 42,
  "action": "call",
  "amount": 120
}
```

Actions:
- `"fold"` — amount ignored
- `"check"` — amount ignored (only valid when `to_call` is 0)
- `"call"` — amount = `to_call` value
- `"raise"` — amount = total raise-to (must be >= `min_raise`)

`turn_id` must match the `turn_id` from the `turn` message. Stale IDs are rejected with `{"type": "error", "text": "stale turn_id ..."}`. If you answer the wrong turn (e.g. network delay replayed an old response), the dealer ignores it and keeps waiting for the current one.

If `amount` is malformed (non-integer string, `null`, etc.), the dealer replies with `{"type": "error", "text": "invalid amount: ..."}` and keeps waiting; your bot will be auto-played on timeout.

### Timeout

If you don't reply within 5 seconds, dealer auto-plays: check if possible, fold otherwise. A 0.5s safety delay follows each auto-action (to prevent runaway loops when many bots disconnect at once).

## Multi-table tournaments

Tournaments with more players than fit at one table are run across multiple tables in parallel. **Your bot needs no special handling** — just respond to `turn` messages as usual.

What happens automatically:
- At tournament start you're seated at one table (random). You receive `tournament_start` with `your_table` (informational only).
- Each table runs its own round independently; blind levels increase simultaneously across all tables.
- All `turn`, `cards`, `event`, `showdown`, `round_end` messages include `table_id` so you know which table they're about. But since you're only ever seated at one table at a time, you normally just process them in order.
- When your table closes (down to 1 survivor) or the tournament consolidates to a final table, you receive `table_change` with your new `table_id`. Continue as normal.
- `event` messages are **scoped to your table** — you don't see what's happening at other tables.

You can safely ignore `table_id` entirely if your strategy doesn't care about it.

## Reconnecting

If your bot disconnects, reconnect with the token from registration:

```json
{
  "type": "register",
  "team": "MyBot",
  "invite": "POKER-XXXX",
  "token": "abc123..."
}
```

## Minimal bot (~30 lines)

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://localhost:9000") as ws:
        await ws.send(json.dumps({
            "type": "register", "team": "MyBot", "invite": "POKER-XXXX"
        }))
        print(json.loads(await ws.recv()))  # registered

        async for raw in ws:
            msg = json.loads(raw)

            if msg["type"] == "turn":
                # YOUR STRATEGY HERE
                to_call = msg["to_call"]
                if to_call == 0:
                    action = {"action": "check", "amount": 0}
                else:
                    action = {"action": "call", "amount": to_call}

                await ws.send(json.dumps({
                    "type": "action",
                    "turn_id": msg["turn_id"],
                    **action,
                }))

            elif msg["type"] == "cards":
                print(f"Cards: {msg['hole_cards']}")

asyncio.run(main())
```

## Migrating from Telegram bot

If you have an existing Telegram bot with strategy logic:

1. Your strategy code (`make_decision()`, equity calculations, LLM calls, etc.) stays the same
2. Replace Telegram I/O with WebSocket I/O
3. See `agent/agent_ws.py` for a complete example that wraps the existing strategy

Key mapping:

| Telegram (old) | WebSocket (new) |
|---|---|
| Parse `"Street: flop \| Pot: 360"` from text | Read `msg["street"]`, `msg["pot"]` from JSON |
| Parse hole cards from private group message | Read `msg["hole_cards"]` from `cards` message |
| Send `/call` to Telegram group | Send `{"type": "action", "action": "call"}` |
| Parse `Valid: /fold /call 20` | Read `msg["valid_actions"]`, `msg["to_call"]` |

## Testing locally

```bash
# Terminal 1: start dealer
cd dealer && python dealer_bot.py

# Terminal 2: start your bot
python bot.py --team MyBot --invite POKER-XXXX

# Terminal 3: start a dummy opponent
cd dealer && python test_dummy_bot.py --team Opponent --invite POKER-XXXX
```

Then trigger the tournament from the web control panel at `http://localhost:8765/control` (click **Start Game**). The spectator view is at `http://localhost:8765/`. Telegram is optional — the dealer runs headless if `DEALER_BOT_TOKEN` isn't configured.
