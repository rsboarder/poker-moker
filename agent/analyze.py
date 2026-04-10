"""Post-tournament analysis — run after the game to review bot performance."""

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "game.db"
HANDS_DIR = Path(__file__).resolve().parent.parent / "data" / "hands"


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def overview(conn):
    print("=" * 60)
    print("TOURNAMENT OVERVIEW")
    print("=" * 60)

    total = conn.execute("SELECT COUNT(*) FROM hands").fetchone()[0]
    if total == 0:
        print("  No hands recorded.")
        return

    sources = conn.execute(
        "SELECT decision_source, COUNT(*) as cnt FROM hands GROUP BY decision_source ORDER BY cnt DESC"
    ).fetchall()

    print(f"\n  Total decisions: {total}\n")
    for s in sources:
        pct = s["cnt"] / total * 100
        bar = "█" * int(pct / 2)
        print(f"  {s['decision_source']:20s} {s['cnt']:4d} ({pct:5.1f}%) {bar}")

    # Decision breakdown
    actions = conn.execute(
        "SELECT decision, COUNT(*) as cnt FROM hands GROUP BY decision ORDER BY cnt DESC"
    ).fetchall()
    print(f"\n  Actions:")
    for a in actions:
        pct = a["cnt"] / total * 100
        print(f"    {a['decision']:20s} {a['cnt']:4d} ({pct:5.1f}%)")


def equity_analysis(conn):
    print("\n" + "=" * 60)
    print("EQUITY ANALYSIS")
    print("=" * 60)

    rows = conn.execute(
        "SELECT decision, AVG(equity) as avg_eq, MIN(equity) as min_eq, "
        "MAX(equity) as max_eq, COUNT(*) as cnt "
        "FROM hands WHERE equity > 0 GROUP BY decision ORDER BY avg_eq DESC"
    ).fetchall()

    print(f"\n  {'Action':<15} {'Avg Eq':>8} {'Min':>8} {'Max':>8} {'Count':>6}")
    print(f"  {'-'*15} {'-'*8} {'-'*8} {'-'*8} {'-'*6}")
    for r in rows:
        print(f"  {r['decision']:<15} {r['avg_eq']:>7.1%} {r['min_eq']:>7.1%} "
              f"{r['max_eq']:>7.1%} {r['cnt']:>6}")

    # Questionable folds (high equity folds)
    bad_folds = conn.execute(
        "SELECT round_num, hole_cards, community_cards, street, equity, pot_odds, "
        "decision_source, hand_tier "
        "FROM hands WHERE decision = 'fold' AND equity > 0.40 ORDER BY equity DESC LIMIT 10"
    ).fetchall()

    if bad_folds:
        print(f"\n  QUESTIONABLE FOLDS (equity > 40%):")
        for r in bad_folds:
            print(f"    Round {r['round_num']:>4s}: {r['hole_cards']:>8s} | "
                  f"{r['street']:>7s} | eq={r['equity']:.1%} | odds={r['pot_odds']:.1%} | "
                  f"tier={r['hand_tier']} | src={r['decision_source']}")

    # Missed value (low equity calls/raises)
    bad_calls = conn.execute(
        "SELECT round_num, hole_cards, community_cards, street, equity, pot_odds, "
        "decision_source, hand_tier "
        "FROM hands WHERE decision IN ('call', 'raise') AND equity < 0.25 AND equity > 0 "
        "ORDER BY equity ASC LIMIT 10"
    ).fetchall()

    if bad_calls:
        print(f"\n  QUESTIONABLE CALLS/RAISES (equity < 25%):")
        for r in bad_calls:
            d = dict(r)
            print(f"    Round {d['round_num']:>4s}: {d['hole_cards']:>8s} | "
                  f"{d['street']:>7s} | eq={d['equity']:.1%} | odds={d['pot_odds']:.1%} | "
                  f"tier={d['hand_tier']} | src={d['decision_source']}")


def llm_analysis(conn):
    print("\n" + "=" * 60)
    print("LLM PERFORMANCE")
    print("=" * 60)

    stats = conn.execute(
        "SELECT COUNT(*) as total, "
        "AVG(latency_ms) as avg_ms, "
        "MIN(latency_ms) as min_ms, "
        "MAX(latency_ms) as max_ms, "
        "SUM(timed_out) as timeouts "
        "FROM llm_calls"
    ).fetchone()

    if stats["total"] == 0:
        print("  No LLM calls recorded.")
        return

    print(f"\n  Total calls:    {stats['total']}")
    print(f"  Avg latency:    {stats['avg_ms']:.0f}ms ({stats['avg_ms']/1000:.1f}s)")
    print(f"  Min latency:    {stats['min_ms']:.0f}ms")
    print(f"  Max latency:    {stats['max_ms']:.0f}ms")
    print(f"  Timeouts:       {stats['timeouts']} ({stats['timeouts']/stats['total']*100:.0f}%)")

    # LLM vs equity decisions comparison
    llm_hands = conn.execute(
        "SELECT decision, COUNT(*) as cnt FROM hands "
        "WHERE decision_source = 'llm' GROUP BY decision ORDER BY cnt DESC"
    ).fetchall()
    fallback_hands = conn.execute(
        "SELECT decision, COUNT(*) as cnt FROM hands "
        "WHERE decision_source = 'equity_fallback' GROUP BY decision ORDER BY cnt DESC"
    ).fetchall()

    llm_summary = ", ".join(f"{r['decision']}({r['cnt']})" for r in llm_hands)
    fb_summary = ", ".join(f"{r['decision']}({r['cnt']})" for r in fallback_hands)
    print(f"\n  LLM decisions:      {llm_summary}")
    print(f"  Fallback decisions: {fb_summary}")

    # Show LLM reasoning examples
    reasoning = conn.execute(
        "SELECT round_num, hole_cards, community_cards, street, equity, decision, "
        "llm_reasoning, response_time_ms "
        "FROM hands WHERE llm_reasoning IS NOT NULL AND llm_reasoning != '' "
        "ORDER BY hand_id DESC LIMIT 5"
    ).fetchall()

    if reasoning:
        print(f"\n  RECENT LLM REASONING:")
        for r in reasoning:
            print(f"\n  --- Round {r['round_num']}: {r['hole_cards']} | "
                  f"{r['street']} | eq={r['equity']:.1%} → {r['decision']} "
                  f"({r['response_time_ms']:.0f}ms) ---")
            text = r["llm_reasoning"][:300]
            for line in text.splitlines():
                print(f"    {line}")
            if len(r["llm_reasoning"]) > 300:
                print(f"    ...")


def opponent_analysis(conn):
    print("\n" + "=" * 60)
    print("OPPONENT PROFILES")
    print("=" * 60)

    opps = conn.execute("SELECT * FROM opponent_stats ORDER BY hands_seen DESC").fetchall()

    if not opps:
        print("  No opponent data.")
        return

    for o in opps:
        d = dict(o)
        h = d["hands_seen"] or 1
        vpip = d["vpip_count"] / h
        pfr = d["pfr_count"] / h
        af = d["aggression_bets"] / d["aggression_calls"] if d["aggression_calls"] > 0 else d["aggression_bets"]

        if vpip > 0.40:
            style = "LOOSE-AGG" if af > 1.5 else "LOOSE-PASSIVE"
        elif vpip < 0.22:
            style = "TIGHT-AGG" if af > 1.5 else "TIGHT-PASSIVE"
        else:
            style = "AGG" if af > 1.5 else "PASSIVE"

        print(f"\n  @{d['bot_username']}")
        print(f"    Hands: {h} | VPIP: {vpip:.0%} | PFR: {pfr:.0%} | AF: {af:.1f}")
        print(f"    Style: {style}")

        # Exploitation tips
        if vpip > 0.40:
            print(f"    Tip: Very loose — tighten up, value bet wider against them")
        elif vpip < 0.22:
            print(f"    Tip: Very tight — respect their raises, steal their blinds")


def speed_analysis(conn):
    print("\n" + "=" * 60)
    print("RESPONSE SPEED")
    print("=" * 60)

    sources = conn.execute(
        "SELECT decision_source, AVG(response_time_ms) as avg_ms, "
        "MAX(response_time_ms) as max_ms, COUNT(*) as cnt "
        "FROM hands GROUP BY decision_source ORDER BY avg_ms DESC"
    ).fetchall()

    print(f"\n  {'Source':<20} {'Avg':>10} {'Max':>10} {'Count':>6}")
    print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*6}")
    for s in sources:
        avg = f"{s['avg_ms']:.0f}ms" if s["avg_ms"] < 1000 else f"{s['avg_ms']/1000:.1f}s"
        mx = f"{s['max_ms']:.0f}ms" if s["max_ms"] < 1000 else f"{s['max_ms']/1000:.1f}s"
        print(f"  {s['decision_source']:<20} {avg:>10} {mx:>10} {s['cnt']:>6}")

    # Check if any decisions risked timeout (>50s)
    slow = conn.execute(
        "SELECT COUNT(*) FROM hands WHERE response_time_ms > 50000"
    ).fetchone()[0]
    if slow:
        print(f"\n  ⚠ {slow} decisions took >50s (risk of dealer timeout)")


def street_analysis(conn):
    print("\n" + "=" * 60)
    print("PLAY BY STREET")
    print("=" * 60)

    streets = conn.execute(
        "SELECT street, decision, COUNT(*) as cnt FROM hands "
        "GROUP BY street, decision ORDER BY street, cnt DESC"
    ).fetchall()

    current_street = None
    for s in streets:
        if s["street"] != current_street:
            current_street = s["street"]
            print(f"\n  {current_street.upper()}:")
        print(f"    {s['decision']:<15} {s['cnt']}")


def main():
    if not DB_PATH.exists():
        print("No game data found. Run the bot first.")
        sys.exit(1)

    conn = get_db()

    overview(conn)
    equity_analysis(conn)
    llm_analysis(conn)
    opponent_analysis(conn)
    speed_analysis(conn)
    street_analysis(conn)

    print("\n" + "=" * 60)
    print("Raw data: data/game.db (SQLite) + data/hands/*.json")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
