"""Verify bot configuration: token, groups, username, AI CLI."""

import asyncio
import os
import subprocess

from dotenv import load_dotenv

load_dotenv()


def check_env():
    print("=== 1. ENV Variables ===\n")

    fields = {
        "AGENT_BOT_TOKEN": os.getenv("AGENT_BOT_TOKEN"),
        "AGENT_USERNAME": os.getenv("AGENT_USERNAME"),
        "MAIN_GROUP_ID": os.getenv("MAIN_GROUP_ID"),
        "PRIVATE_GROUP_ID": os.getenv("PRIVATE_GROUP_ID"),
        "DEALER_USERNAME": os.getenv("DEALER_USERNAME"),
        "CODEX_PATH": os.getenv("CODEX_PATH"),
        "CODEX_MODEL": os.getenv("CODEX_MODEL"),
        "CODEX_TIMEOUT": os.getenv("CODEX_TIMEOUT"),
    }

    ok = True
    for k, v in fields.items():
        if not v:
            if k == "CODEX_MODEL":
                print(f"  {k}: (not set, default: haiku)")
            else:
                print(f"  {k}: *** MISSING ***")
                ok = False
        elif "TOKEN" in k:
            print(f"  {k}: {v[:8]}...{v[-4:]}")
        else:
            print(f"  {k}: {v}")

    # Check username format
    username = fields.get("AGENT_USERNAME", "")
    if username and username.startswith("@"):
        print(f"\n  ⚠ AGENT_USERNAME starts with '@' — remove it!")
        print(f"    Current:  {username}")
        print(f"    Should be: {username[1:]}")
        ok = False

    print()
    return ok


async def check_bot():
    print("=== 2. Bot Token ===\n")
    from telegram import Bot

    token = os.environ.get("AGENT_BOT_TOKEN")
    if not token:
        print("  Skipped — no token")
        return False

    try:
        bot = Bot(token)
        me = await bot.get_me()
        print(f"  Bot: @{me.username} (id={me.id})")
        print(f"  Name: {me.first_name}")

        expected = os.getenv("AGENT_USERNAME", "").lstrip("@")
        if expected and me.username.lower() != expected.lower():
            print(f"\n  ⚠ AGENT_USERNAME mismatch!")
            print(f"    .env says: {expected}")
            print(f"    Telegram says: {me.username}")
    except Exception as e:
        print(f"  FAIL: {e}")
        return False

    print()
    print("=== 3. Group Access ===\n")

    for name, env_key in [("MAIN", "MAIN_GROUP_ID"), ("PRIVATE", "PRIVATE_GROUP_ID")]:
        gid = os.environ.get(env_key)
        if not gid:
            print(f"  {name}: skipped — not set")
            continue
        try:
            chat = await bot.get_chat(int(gid))
            print(f"  {name}: \"{chat.title}\" (type={chat.type}) ✓")
        except Exception as e:
            print(f"  {name}: FAIL ({gid}) — {e}")

    print()
    return True


def check_cli():
    print("=== 4. AI CLI ===\n")

    cli = os.getenv("CODEX_PATH", "claude")
    try:
        result = subprocess.run(
            [cli, "--version"], capture_output=True, text=True, timeout=5
        )
        version = result.stdout.strip() or result.stderr.strip()
        print(f"  {cli}: {version} ✓")
    except FileNotFoundError:
        print(f"  {cli}: NOT FOUND")
        return False
    except subprocess.TimeoutExpired:
        print(f"  {cli}: timeout (but exists)")

    print()
    return True


def check_modules():
    print("=== 5. Modules ===\n")

    modules = ["hand_tiers", "equity", "storage", "opponent_tracker", "llm_engine", "strategy"]
    ok = True
    for mod in modules:
        try:
            __import__(mod)
            print(f"  {mod}: ✓")
        except Exception as e:
            print(f"  {mod}: FAIL — {e}")
            ok = False

    print()
    return ok


def main():
    print("Poker Bot — Config Check")
    print("=" * 50)
    print()

    env_ok = check_env()
    modules_ok = check_modules()
    cli_ok = check_cli()
    bot_ok = asyncio.run(check_bot())

    print("=" * 50)
    results = {
        "ENV": env_ok,
        "Modules": modules_ok,
        "AI CLI": cli_ok,
        "Bot + Groups": bot_ok,
    }
    for name, ok in results.items():
        print(f"  {name}: {'✓' if ok else 'FAIL'}")

    print()
    if all(results.values()):
        print("All checks passed! Run: python agent_bot.py")
    else:
        print("Fix the issues above before running the bot.")


if __name__ == "__main__":
    main()
