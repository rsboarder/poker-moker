"""Quick smoke test: start WS server, connect a dummy bot, verify registration."""

import asyncio
import json
import websockets


async def test_registration():
    # Start a minimal WS server that mimics dealer registration
    registrations = {}

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "register":
                username = msg["team"].lower()
                registrations[username] = ws
                await ws.send(json.dumps({
                    "type": "registered",
                    "username": username,
                    "players_online": len(registrations),
                }))

    server = await websockets.serve(handler, "localhost", 9999)

    # Connect a client
    async with websockets.connect("ws://localhost:9999") as ws:
        await ws.send(json.dumps({
            "type": "register",
            "team": "TestBot",
            "invite": "TEST",
        }))
        reply = json.loads(await ws.recv())
        assert reply["type"] == "registered", f"Expected registered, got {reply}"
        assert reply["username"] == "testbot"
        assert reply["players_online"] == 1
        print(f"OK: registered as {reply['username']}, {reply['players_online']} online")

    server.close()
    await server.wait_closed()
    print("OK: WS server started and stopped cleanly")


if __name__ == "__main__":
    asyncio.run(test_registration())
