"""SQLite + JSON storage for game data and research analysis."""

import json
import sqlite3
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


def _get_dirs(bot_name: str | None = None):
    if bot_name:
        data_dir = PROJECT_ROOT / "data" / bot_name
    else:
        data_dir = DEFAULT_DATA_DIR
    hands_dir = data_dir / "hands"
    db_path = data_dir / "game.db"
    return data_dir, hands_dir, db_path


def get_db(bot_name: str | None = None) -> tuple[sqlite3.Connection, Path, Path]:
    data_dir, hands_dir, db_path = _get_dirs(bot_name)
    data_dir.mkdir(parents=True, exist_ok=True)
    hands_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _create_tables(conn)
    return conn, hands_dir, db_path


def _create_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hands (
            hand_id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_num TEXT,
            timestamp REAL,
            hole_cards TEXT,
            community_cards TEXT,
            street TEXT,
            pot INTEGER,
            stack INTEGER,
            position TEXT,
            equity REAL,
            pot_odds REAL,
            hand_tier INTEGER,
            equity_category TEXT,
            llm_reasoning TEXT,
            decision TEXT,
            decision_source TEXT,
            response_time_ms REAL,
            -- v2: research improvements
            board_texture TEXT,
            spr REAL,
            geo_bet_size INTEGER,
            confidence_level TEXT,
            exploit_triggered TEXT,
            solver_recommendation TEXT,
            llm_agreed_with_solver INTEGER
        );

        CREATE TABLE IF NOT EXISTS opponent_stats (
            bot_username TEXT PRIMARY KEY,
            hands_seen INTEGER DEFAULT 0,
            vpip_count INTEGER DEFAULT 0,
            pfr_count INTEGER DEFAULT 0,
            aggression_bets INTEGER DEFAULT 0,
            aggression_calls INTEGER DEFAULT 0,
            fold_to_cbet_seen INTEGER DEFAULT 0,
            fold_to_cbet_folded INTEGER DEFAULT 0,
            three_bet_opportunities INTEGER DEFAULT 0,
            three_bet_count INTEGER DEFAULT 0,
            went_to_showdown INTEGER DEFAULT 0,
            hands_reached_flop INTEGER DEFAULT 0,
            updated_at REAL
        );

        CREATE TABLE IF NOT EXISTS llm_calls (
            call_id INTEGER PRIMARY KEY AUTOINCREMENT,
            hand_id INTEGER,
            prompt TEXT,
            raw_response TEXT,
            parsed_action TEXT,
            model_name TEXT,
            latency_ms REAL,
            timed_out INTEGER DEFAULT 0,
            timestamp REAL,
            FOREIGN KEY (hand_id) REFERENCES hands(hand_id)
        );
    """)
    conn.commit()


class GameStorage:
    def __init__(self, bot_name: str | None = None):
        self.bot_name = bot_name
        self.conn, self.hands_dir, self.db_path = get_db(bot_name)

    def save_hand(
        self,
        round_num: str,
        hole_cards: list[str],
        community_cards: list[str],
        street: str,
        pot: int,
        stack: int,
        position: str,
        equity: float,
        pot_odds: float,
        hand_tier: int,
        equity_category: str,
        llm_reasoning: str | None,
        decision: str,
        decision_source: str,
        response_time_ms: float,
        board_texture: str | None = None,
        spr: float | None = None,
        geo_bet_size: int | None = None,
        confidence_level: str | None = None,
        exploit_triggered: str | None = None,
        solver_recommendation: str | None = None,
        llm_agreed_with_solver: bool | None = None,
    ) -> int:
        ts = time.time()
        cursor = self.conn.execute(
            """INSERT INTO hands (
                round_num, timestamp, hole_cards, community_cards, street,
                pot, stack, position, equity, pot_odds, hand_tier,
                equity_category, llm_reasoning, decision, decision_source,
                response_time_ms, board_texture, spr, geo_bet_size,
                confidence_level, exploit_triggered, solver_recommendation,
                llm_agreed_with_solver
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                round_num, ts,
                " ".join(hole_cards), " ".join(community_cards), street,
                pot, stack, position, equity, pot_odds, hand_tier,
                equity_category, llm_reasoning, decision, decision_source,
                response_time_ms, board_texture, spr, geo_bet_size,
                confidence_level, exploit_triggered, solver_recommendation,
                int(llm_agreed_with_solver) if llm_agreed_with_solver is not None else None,
            ),
        )
        self.conn.commit()
        hand_id = cursor.lastrowid

        self._save_hand_json(hand_id, round_num, ts, hole_cards, community_cards,
                             street, pot, stack, position, equity, pot_odds,
                             hand_tier, equity_category, llm_reasoning, decision,
                             decision_source, response_time_ms, board_texture,
                             spr, geo_bet_size, confidence_level, exploit_triggered,
                             solver_recommendation, llm_agreed_with_solver)
        return hand_id

    def _save_hand_json(self, hand_id, round_num, ts, hole_cards, community_cards,
                        street, pot, stack, position, equity, pot_odds,
                        hand_tier, equity_category, llm_reasoning, decision,
                        decision_source, response_time_ms, board_texture=None,
                        spr=None, geo_bet_size=None, confidence_level=None,
                        exploit_triggered=None, solver_recommendation=None,
                        llm_agreed_with_solver=None):
        record = {
            "hand_id": hand_id,
            "round_num": round_num,
            "timestamp": ts,
            "hole_cards": hole_cards,
            "community_cards": community_cards,
            "street": street,
            "pot": pot,
            "stack": stack,
            "position": position,
            "equity": round(equity, 4),
            "pot_odds": round(pot_odds, 4),
            "hand_tier": hand_tier,
            "equity_category": equity_category,
            "llm_reasoning": llm_reasoning,
            "decision": decision,
            "decision_source": decision_source,
            "response_time_ms": round(response_time_ms, 1),
            "board_texture": board_texture,
            "spr": round(spr, 2) if spr is not None else None,
            "geo_bet_size": geo_bet_size,
            "confidence_level": confidence_level,
            "exploit_triggered": exploit_triggered,
            "solver_recommendation": solver_recommendation,
            "llm_agreed_with_solver": llm_agreed_with_solver,
        }
        fname = f"round_{round_num or hand_id}.json"
        path = self.hands_dir / fname
        # Append to existing round file if it exists
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, list):
                data.append(record)
            else:
                data = [data, record]
        else:
            data = record
        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def save_llm_call(
        self,
        hand_id: int | None,
        prompt: str,
        raw_response: str,
        parsed_action: str,
        model_name: str,
        latency_ms: float,
        timed_out: bool = False,
    ) -> int:
        cursor = self.conn.execute(
            """INSERT INTO llm_calls (
                hand_id, prompt, raw_response, parsed_action,
                model_name, latency_ms, timed_out, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (hand_id, prompt, raw_response, parsed_action,
             model_name, latency_ms, int(timed_out), time.time()),
        )
        self.conn.commit()
        return cursor.lastrowid

    def update_opponent(self, bot_username: str, vpip: bool, pfr: bool,
                        is_bet_or_raise: bool, folded_to_cbet: bool | None = None,
                        is_three_bet: bool = False, three_bet_opportunity: bool = False,
                        went_to_showdown: bool = False, reached_flop: bool = False):
        self.conn.execute(
            """INSERT INTO opponent_stats (bot_username, hands_seen, vpip_count,
                pfr_count, aggression_bets, aggression_calls,
                fold_to_cbet_seen, fold_to_cbet_folded,
                three_bet_opportunities, three_bet_count,
                went_to_showdown, hands_reached_flop, updated_at)
            VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bot_username) DO UPDATE SET
                hands_seen = hands_seen + 1,
                vpip_count = vpip_count + excluded.vpip_count,
                pfr_count = pfr_count + excluded.pfr_count,
                aggression_bets = aggression_bets + excluded.aggression_bets,
                aggression_calls = CASE WHEN excluded.aggression_calls > 0
                    THEN aggression_calls + 1 ELSE aggression_calls END,
                fold_to_cbet_seen = fold_to_cbet_seen + excluded.fold_to_cbet_seen,
                fold_to_cbet_folded = fold_to_cbet_folded + excluded.fold_to_cbet_folded,
                three_bet_opportunities = three_bet_opportunities + excluded.three_bet_opportunities,
                three_bet_count = three_bet_count + excluded.three_bet_count,
                went_to_showdown = went_to_showdown + excluded.went_to_showdown,
                hands_reached_flop = hands_reached_flop + excluded.hands_reached_flop,
                updated_at = excluded.updated_at
            """,
            (bot_username, int(vpip), int(pfr), int(is_bet_or_raise),
             int(not is_bet_or_raise and vpip),
             int(folded_to_cbet is not None), int(folded_to_cbet or False),
             int(three_bet_opportunity), int(is_three_bet),
             int(went_to_showdown), int(reached_flop),
             time.time()),
        )
        self.conn.commit()

    def get_opponent_stats(self, bot_username: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM opponent_stats WHERE bot_username = ?",
            (bot_username,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        h = d["hands_seen"] or 1
        d["vpip"] = d["vpip_count"] / h
        d["pfr"] = d["pfr_count"] / h
        d["aggression_factor"] = (
            d["aggression_bets"] / d["aggression_calls"]
            if d["aggression_calls"] > 0 else d["aggression_bets"]
        )
        d["fold_to_cbet"] = (
            d["fold_to_cbet_folded"] / d["fold_to_cbet_seen"]
            if d["fold_to_cbet_seen"] > 0 else None
        )
        d["three_bet_freq"] = (
            d["three_bet_count"] / d["three_bet_opportunities"]
            if d["three_bet_opportunities"] > 0 else None
        )
        d["wtsd"] = (
            d["went_to_showdown"] / d["hands_reached_flop"]
            if d["hands_reached_flop"] > 0 else None
        )
        return d

    def get_all_opponent_stats(self) -> dict[str, dict]:
        rows = self.conn.execute("SELECT * FROM opponent_stats").fetchall()
        result = {}
        for row in rows:
            d = dict(row)
            h = d["hands_seen"] or 1
            d["vpip"] = d["vpip_count"] / h
            d["pfr"] = d["pfr_count"] / h
            d["aggression_factor"] = (
                d["aggression_bets"] / d["aggression_calls"]
                if d["aggression_calls"] > 0 else d["aggression_bets"]
            )
            d["fold_to_cbet"] = (
                d["fold_to_cbet_folded"] / d["fold_to_cbet_seen"]
                if d.get("fold_to_cbet_seen", 0) > 0 else None
            )
            d["three_bet_freq"] = (
                d["three_bet_count"] / d["three_bet_opportunities"]
                if d.get("three_bet_opportunities", 0) > 0 else None
            )
            d["wtsd"] = (
                d["went_to_showdown"] / d["hands_reached_flop"]
                if d.get("hands_reached_flop", 0) > 0 else None
            )
            result[d["bot_username"]] = d
        return result

    def close(self):
        self.conn.close()
