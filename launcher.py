#!/usr/bin/env python3
"""Multi-bot launcher — start one or all poker bots."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BOTS_DIR = Path(__file__).resolve().parent / "bots"
AGENT_SCRIPT = Path(__file__).resolve().parent / "agent" / "agent_bot.py"
LOGS_DIR = Path(__file__).resolve().parent / "logs"

processes: dict[str, subprocess.Popen] = {}


def list_bots():
    if not BOTS_DIR.exists():
        print("No bots/ directory found.")
        return []
    envs = sorted(BOTS_DIR.glob("*.env"))
    if not envs:
        print("No .env files in bots/")
        return []
    return envs


def start_bot(env_path: Path) -> subprocess.Popen | None:
    name = env_path.stem
    if name in processes and processes[name].poll() is None:
        print(f"  {name}: already running (pid={processes[name].pid})")
        return processes[name]

    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{name}_launcher.log"

    print(f"  {name}: starting...")
    proc = subprocess.Popen(
        [sys.executable, str(AGENT_SCRIPT), str(env_path)],
        stdout=open(log_file, "a"),
        stderr=subprocess.STDOUT,
        cwd=str(AGENT_SCRIPT.parent),
    )
    processes[name] = proc
    print(f"  {name}: started (pid={proc.pid})")
    return proc


def stop_bot(name: str):
    if name not in processes:
        print(f"  {name}: not running")
        return
    proc = processes[name]
    if proc.poll() is None:
        print(f"  {name}: stopping (pid={proc.pid})...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    del processes[name]
    print(f"  {name}: stopped")


def status():
    envs = list_bots()
    if not envs:
        return
    print(f"\n{'Bot':<25} {'Status':<15} {'PID':<10}")
    print("-" * 50)
    for env in envs:
        name = env.stem
        if name in processes and processes[name].poll() is None:
            print(f"{name:<25} {'RUNNING':<15} {processes[name].pid:<10}")
        elif name in processes:
            print(f"{name:<25} {'EXITED':<15} {processes[name].returncode:<10}")
        else:
            print(f"{name:<25} {'STOPPED':<15} {'-':<10}")
    print()


def shutdown_all():
    print("\nStopping all bots...")
    for name in list(processes.keys()):
        stop_bot(name)


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print(f"  {sys.argv[0]} <bot_name>       Start one bot (e.g. bot1)")
        print(f"  {sys.argv[0]} --all            Start all bots in bots/")
        print(f"  {sys.argv[0]} --list           List available bots")
        print(f"\nBot configs go in: {BOTS_DIR}/")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "--list":
        envs = list_bots()
        for e in envs:
            print(f"  {e.stem}: {e}")
        return

    if cmd == "--all":
        envs = list_bots()
        if not envs:
            sys.exit(1)

        signal.signal(signal.SIGINT, lambda *_: shutdown_all() or sys.exit(0))
        signal.signal(signal.SIGTERM, lambda *_: shutdown_all() or sys.exit(0))

        print(f"Starting {len(envs)} bots...")
        for env in envs:
            start_bot(env)

        print(f"\nAll bots running. Press Ctrl+C to stop all.")
        status()

        try:
            while True:
                # Monitor and restart crashed bots
                for env in envs:
                    name = env.stem
                    if name in processes and processes[name].poll() is not None:
                        code = processes[name].returncode
                        print(f"\n  {name}: crashed (exit={code}), restarting...")
                        start_bot(env)
                time.sleep(5)
        except KeyboardInterrupt:
            shutdown_all()
        return

    # Single bot
    env_path = BOTS_DIR / f"{cmd}.env"
    if not env_path.exists():
        # Try direct .env path
        env_path = Path(cmd)
        if not env_path.exists():
            print(f"Config not found: {env_path}")
            print(f"Available: {[e.stem for e in list_bots()]}")
            sys.exit(1)

    signal.signal(signal.SIGINT, lambda *_: shutdown_all() or sys.exit(0))

    proc = start_bot(env_path)
    if proc:
        print(f"\nBot running. Press Ctrl+C to stop.")
        try:
            proc.wait()
        except KeyboardInterrupt:
            shutdown_all()


if __name__ == "__main__":
    main()
