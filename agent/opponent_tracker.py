"""Parse [DEALER] messages and track opponent statistics."""

import re
from storage import GameStorage

# Patterns for parsing dealer announcements
RE_PLAYER_ACTION = re.compile(
    r"\[DEALER\]\s+@?(\w+)\s+(folds?|checks?|calls?|bets?|raises?|all.?in)",
    re.IGNORECASE,
)
RE_COMMUNITY = re.compile(r"Community:\s*([^\n]+)", re.IGNORECASE)
CARD_RE = re.compile(r"[2-9TJQKA][hdcs]")


class OpponentTracker:
    def __init__(self, storage: GameStorage, our_username: str):
        self.storage = storage
        self.our_username = our_username.lower()
        # Per-round tracking to know if action is preflop
        self._current_round_actors: set[str] = set()
        self._is_preflop = True

    def reset_round(self):
        self._current_round_actors.clear()
        self._is_preflop = True

    def set_street(self, street: str):
        self._is_preflop = street.lower() == "preflop"

    def parse_dealer_message(self, text: str) -> list[str]:
        """Parse a [DEALER] message, update opponent stats, return community cards if found."""
        community = []
        if m := RE_COMMUNITY.search(text):
            raw = m.group(1).strip()
            if raw not in ("—", "-"):
                community = CARD_RE.findall(raw)

        for match in RE_PLAYER_ACTION.finditer(text):
            username = match.group(1).lower()
            action = match.group(2).lower().rstrip("s")

            if username == self.our_username:
                continue

            is_voluntary = action in ("call", "raise", "bet", "all-in", "allin", "all_in")
            is_preflop_raise = self._is_preflop and action in ("raise", "bet")
            is_aggressive = action in ("raise", "bet", "all-in", "allin", "all_in")

            self.storage.update_opponent(
                bot_username=username,
                vpip=is_voluntary,
                pfr=is_preflop_raise,
                is_bet_or_raise=is_aggressive,
            )

        return community

    def get_stats_summary(self) -> str:
        all_stats = self.storage.get_all_opponent_stats()
        if not all_stats:
            return "(no opponent data yet)"

        lines = []
        for username, stats in all_stats.items():
            if stats["hands_seen"] < 3:
                lines.append(f"@{username}: {stats['hands_seen']} hands (too few for stats)")
                continue

            vpip_pct = stats["vpip"] * 100
            pfr_pct = stats["pfr"] * 100
            af = stats["aggression_factor"]

            style = "unknown"
            if vpip_pct > 40:
                style = "loose-aggressive" if af > 1.5 else "loose-passive"
            elif vpip_pct < 22:
                style = "tight-aggressive" if af > 1.5 else "tight-passive"
            else:
                style = "aggressive" if af > 1.5 else "passive"

            lines.append(
                f"@{username}: VPIP {vpip_pct:.0f}% | PFR {pfr_pct:.0f}% | "
                f"AF {af:.1f} | Style: {style} | ({stats['hands_seen']} hands)"
            )
        return "\n".join(lines)

    def get_avg_vpip(self) -> float | None:
        all_stats = self.storage.get_all_opponent_stats()
        if not all_stats:
            return None
        vpips = [s["vpip"] for s in all_stats.values() if s["hands_seen"] >= 3]
        return sum(vpips) / len(vpips) if vpips else None
