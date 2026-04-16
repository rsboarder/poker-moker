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
    {"username": "aggressor", "stack": 850, "street_bet": 120, "status": "active"},
    {"username": "conservative", "stack": 970, "street_bet": 0, "status": "active"},
    {"username": "mybot", "stack": 970, "street_bet": 0, "status": "active"}
  ],
  "valid_actions": ["fold", "call 120", "raise 240-970"],
  "to_call": 120,
  "min_raise": 240
}
```

Fields:
- `turn_id` — must include in your reply
- `street` — `preflop`, `flop`, `turn`, `river`
- `pot` — current pot size
- `stack` — your remaining chips
- `community` — board cards
- `hole_cards` — your hole cards (included so you don't need to track state)
- `position` — your seat: `SB` (small blind), `BB` (big blind), `UTG` (under the gun, first after BB), `MP` (middle), `CO` (cutoff, before button), `BTN` (button/dealer, best position)
- `players` — all players at the table with their stacks, current street bets, and status
- `valid_actions` — what you can do (human-readable)
- `to_call` — chips needed to call (0 = can check)
- `min_raise` — minimum raise-to amount

### 3. `event` — other players' actions, board updates

```json
{
  "type": "event",
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
  "street": "flop",
  "community": ["9s", "Js", "7d"],
  "text": "[DEALER] --- FLOP: 9s Js 7d ---"
}
```

The `text` field is always present for backward compatibility. Structured fields (`player`, `action`, `amount`, `street`, `community`) are parsed from the text when available.

### 4. `round_end` — round complete, final stacks

Sent after showdown, before the next round starts. Use this to update opponent tracking.

```json
{
  "type": "round_end",
  "round": 5,
  "players": [
    {"username": "aggressor", "stack": 640},
    {"username": "conservative", "stack": 1360}
  ]
}
```

### 5. `showdown` — round result

```json
{
  "type": "showdown",
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

### 5. `eliminated` — you're out

```json
{"type": "eliminated", "place": 12, "players_left": 18}
```

### 6. `tournament_start` / `tournament_over`

```json
{"type": "tournament_start", "players": 30, "tables": 5, "your_table": 2}
{"type": "tournament_over", "winner": "botname"}
```

### 7. `error` — something went wrong

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

`turn_id` must match the `turn_id` from the `turn` message. Stale IDs are rejected.

### Timeout

If you don't reply within 5 seconds, dealer auto-plays: check if possible, fold otherwise.

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

# In Telegram: /startgame
```
