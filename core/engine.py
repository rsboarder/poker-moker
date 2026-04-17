from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from treys import Deck
from core.evaluator import str_to_card, cards_to_str, determine_winner, evaluate_hand


class GameState(Enum):
    WAITING  = "waiting"
    PREFLOP  = "preflop"
    FLOP     = "flop"
    TURN     = "turn"
    RIVER    = "river"
    SHOWDOWN = "showdown"


class PlayerStatus(Enum):
    ACTIVE = "active"
    FOLDED = "folded"
    ALL_IN = "all_in"


STREET_ORDER = [GameState.PREFLOP, GameState.FLOP, GameState.TURN, GameState.RIVER, GameState.SHOWDOWN]

SMALL_BLIND = 10
BIG_BLIND   = 20


@dataclass
class Player:
    id: int
    name: str = ""           # display name (username without @)
    stack: int = 1000
    hole_cards: list = field(default_factory=list)   # treys int cards
    street_bet: int = 0          # bet placed this street
    total_contributed: int = 0   # cumulative chips put in pot this hand (for side pot calc)
    status: PlayerStatus = PlayerStatus.ACTIVE
    acted_this_street: bool = False

    @property
    def display(self) -> str:
        return self.name if self.name else f"Player {self.id}"


@dataclass
class GameEvent:
    type: str   # state_update | dealer_message | hand_dealt | showdown | error
    data: dict


class GameEngine:
    def __init__(self):
        self.state = GameState.WAITING
        self.players: list[Player] = []
        self.community: list[int] = []   # treys int cards
        self.pot: int = 0
        self.current_idx: int = 0        # index into self.players
        self.street_aggressor_idx: int = -1  # last player to raise this street
        self._deck: Deck | None = None
        self.round_number: int = 0
        # Blinds — configurable for tournament blind schedule
        self.small_blind: int = SMALL_BLIND
        self.big_blind: int = BIG_BLIND

    def set_blinds(self, small_blind: int, big_blind: int) -> None:
        """Update blind levels (called by dealer before each round)."""
        self.small_blind = small_blind
        self.big_blind = big_blind

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_round(self, players: list[dict]) -> list[GameEvent]:
        """Initialize and deal a fresh round.

        players: list of {"id": int, "name": str, "stack": int}
        Returns events to emit.
        """
        self._deck = Deck()
        self.community = []
        self.pot = 0
        self.state = GameState.PREFLOP
        self.round_number += 1

        self.players = [
            Player(id=p["id"], name=p.get("name", ""), stack=p["stack"])
            for p in players
        ]

        # Deal 2 hole cards per player
        for p in self.players:
            p.hole_cards = self._deck.draw(2)

        # Post blinds: first player = SB, second = BB
        events = []
        events += self._post_blind(player_idx=0, amount=self.small_blind, label="small blind")
        events += self._post_blind(player_idx=1, amount=self.big_blind,   label="big blind")

        # Preflop: action starts with player after BB (UTG / BTN in 3-player)
        # For n=2 (heads-up): 2%2=0 → SB/BTN acts first (heads-up rule)
        # For n=3+: 2%n=2 → BTN/UTG acts first, BB gets last action ("option")
        n = len(self.players)
        self.current_idx = 2 % n
        self.street_aggressor_idx = 1  # BB is the aggressor until someone raises

        # Emit hand_dealt events (private)
        for p in self.players:
            events.append(GameEvent(
                type="hand_dealt",
                data={
                    "target": p.id,
                    "hole_cards": cards_to_str(p.hole_cards),
                }
            ))

        events.append(self._state_event())
        events.append(self._dealer_prompt(self.players[self.current_idx]))
        return events

    def apply_action(self, player_id: int, action: str, amount: int = 0) -> list[GameEvent]:
        """Process a player action. Returns list of events to emit."""
        events = []

        p = self._get_player(player_id)
        if p is None:
            return [GameEvent(type="error", data={"text": f"Unknown player {player_id}"})]

        if self.state in (GameState.WAITING, GameState.SHOWDOWN):
            return [GameEvent(type="error", data={"text": "No active round."})]

        if self.players[self.current_idx].id != player_id:
            return [GameEvent(type="error", data={
                "text": f"Not your turn. Waiting for Player {self.players[self.current_idx].id}."
            })]

        action = action.lower().strip()
        to_call = self._to_call(p)

        if action == "fold":
            p.status = PlayerStatus.FOLDED
            p.acted_this_street = True
            events.append(GameEvent(type="dealer_message", data={
                "target": "all",
                "text": f"[DEALER] {p.display} folds."
            }))
            active = self._active_players()
            still_in = [p for p in self.players if p.status != PlayerStatus.FOLDED]
            if len(still_in) == 1:
                # Last player standing (active or all-in) wins the pot immediately
                winner = still_in[0]
                winner.stack += self.pot
                events.append(GameEvent(type="showdown", data={
                    "winner_id": winner.id,
                    "reason": "fold",
                    "pot": self.pot,
                    "pots": [{"amount": self.pot, "winner_ids": [winner.id], "split": False}],
                    "total_won": {winner.id: self.pot},
                    "hands": [],
                }))
                self.state = GameState.SHOWDOWN
                events.append(self._state_event())
            else:
                # Others keep playing
                events += self._advance(p)
            return events

        elif action == "check":
            if to_call > 0:
                return [GameEvent(type="error", data={"text": f"Cannot check — must call {to_call} or fold/raise."})]
            p.acted_this_street = True
            events.append(GameEvent(type="dealer_message", data={
                "target": "all",
                "text": f"[DEALER] {p.display} checks."
            }))

        elif action == "call":
            call_amt = min(to_call, p.stack)
            p.stack -= call_amt
            p.street_bet += call_amt
            p.total_contributed += call_amt
            self.pot += call_amt
            p.acted_this_street = True
            if p.stack == 0:
                p.status = PlayerStatus.ALL_IN
            events.append(GameEvent(type="dealer_message", data={
                "target": "all",
                "text": f"[DEALER] {p.display} calls {call_amt}. Pot: {self.pot}."
            }))

        elif action == "raise":
            max_street_bet = max(pl.street_bet for pl in self.players)
            min_raise = max_street_bet + self.big_blind
            total_needed = amount  # amount = total bet this street after raise

            if amount < min_raise:
                return [GameEvent(type="error", data={
                    "text": f"Raise must be at least {min_raise} total (min raise: +{self.big_blind})."
                })]
            if amount > p.stack + p.street_bet:
                return [GameEvent(type="error", data={"text": "Not enough chips."})]

            added = amount - p.street_bet
            p.stack -= added
            p.street_bet = amount
            p.total_contributed += added
            self.pot += added
            p.acted_this_street = True
            self.street_aggressor_idx = self.current_idx

            if p.stack == 0:
                p.status = PlayerStatus.ALL_IN

            # Reset other active players' acted flag so they must respond
            for other in self.players:
                if other.id != p.id and other.status == PlayerStatus.ACTIVE:
                    other.acted_this_street = False

            events.append(GameEvent(type="dealer_message", data={
                "target": "all",
                "text": f"[DEALER] {p.display} raises to {amount}. Pot: {self.pot}."
            }))

        else:
            return [GameEvent(type="error", data={"text": f"Unknown action: {action}. Use: fold check call raise <amount>"})]

        # Advance to next player or next street
        events += self._advance(p)
        return events

    def public_state(self) -> dict:
        """Return serialisable public game state (no hole cards)."""
        return {
            "state": self.state.value,
            "pot": self.pot,
            "community_cards": cards_to_str(self.community),
            "active_player": self.players[self.current_idx].id if self.state not in (GameState.WAITING, GameState.SHOWDOWN) else None,
            "players": [
                {
                    "id": p.id,
                    "stack": p.stack,
                    "street_bet": p.street_bet,
                    "status": p.status.value,
                }
                for p in self.players
            ],
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post_blind(self, player_idx: int, amount: int, label: str) -> list[GameEvent]:
        p = self.players[player_idx]
        actual = min(amount, p.stack)
        p.stack -= actual
        p.street_bet += actual
        p.total_contributed += actual
        self.pot += actual
        return [GameEvent(type="dealer_message", data={
            "target": "all",
            "text": f"[DEALER] {p.display} posts {label}: {actual}."
        })]

    def _to_call(self, player: Player) -> int:
        max_bet = max(p.street_bet for p in self.players)
        return max(0, max_bet - player.street_bet)

    def _get_player(self, player_id: int) -> Player | None:
        for p in self.players:
            if p.id == player_id:
                return p
        return None

    def _get_opponents(self, player: Player) -> list[Player]:
        return [p for p in self.players if p.id != player.id]

    def _active_players(self) -> list[Player]:
        return [p for p in self.players if p.status == PlayerStatus.ACTIVE]

    def _is_betting_complete(self) -> bool:
        # Players who can still act (not folded, not all-in)
        can_act = [p for p in self.players if p.status == PlayerStatus.ACTIVE]
        if not can_act:
            return True  # Everyone is all-in or folded — run out the board
        max_bet = max(p.street_bet for p in self.players)
        # Every active player must have acted AND matched the current bet
        return all(p.acted_this_street and p.street_bet == max_bet for p in can_act)

    def _advance(self, last_actor: Player) -> list[GameEvent]:
        events = []

        if self._is_betting_complete():
            events += self._next_street()
        else:
            self._move_to_next_active()
            events.append(self._state_event())
            events.append(self._dealer_prompt(self.players[self.current_idx]))

        return events

    def _move_to_next_active(self):
        n = len(self.players)
        for _ in range(n):
            self.current_idx = (self.current_idx + 1) % n
            if self.players[self.current_idx].status == PlayerStatus.ACTIVE:
                return

    def _next_street(self) -> list[GameEvent]:
        events = []

        # Collect bets into pot (already done incrementally), reset street bets
        for p in self.players:
            p.street_bet = 0
            p.acted_this_street = False

        current = self.state
        idx = STREET_ORDER.index(current)
        next_state = STREET_ORDER[idx + 1]
        self.state = next_state

        if next_state == GameState.FLOP:
            drawn = self._deck.draw(3)
            self.community.extend(drawn)
            events.append(GameEvent(type="dealer_message", data={
                "target": "all",
                "text": f"[DEALER] --- FLOP: {' '.join(cards_to_str(self.community))} ---"
            }))
            # Post-flop: SB (idx=0) acts first — first active player left of BTN
            self.current_idx = 0

        elif next_state == GameState.TURN:
            drawn = self._deck.draw(1)
            self.community.extend(drawn)
            events.append(GameEvent(type="dealer_message", data={
                "target": "all",
                "text": f"[DEALER] --- TURN: {' '.join(cards_to_str(self.community))} ---"
            }))
            self.current_idx = 0

        elif next_state == GameState.RIVER:
            drawn = self._deck.draw(1)
            self.community.extend(drawn)
            events.append(GameEvent(type="dealer_message", data={
                "target": "all",
                "text": f"[DEALER] --- RIVER: {' '.join(cards_to_str(self.community))} ---"
            }))
            self.current_idx = 0

        elif next_state == GameState.SHOWDOWN:
            events += self._do_showdown()
            return events

        # Skip folded/all-in players at start of street
        if self.players[self.current_idx].status != PlayerStatus.ACTIVE:
            self._move_to_next_active()

        active = self._active_players()
        if len(active) <= 1:
            events += self._do_showdown()
            return events

        events.append(self._state_event())
        events.append(self._dealer_prompt(self.players[self.current_idx]))
        return events

    def _calculate_side_pots(self) -> list[dict]:
        """Calculate main pot and side pots from per-player total contributions.

        Returns list of pots sorted from smallest to largest (main pot first):
          [{"amount": int, "eligible": [player_id, ...]}]

        Algorithm: iterate over unique contribution levels. At each level,
        collect (level_step × contributors_count) chips into a pot. Only
        players who contributed >= that level AND did not fold are eligible.
        """
        contrib = [(p.id, p.total_contributed, p.status == PlayerStatus.FOLDED)
                   for p in self.players]

        levels = sorted(set(c for _, c, _ in contrib if c > 0))
        pots: list[dict] = []
        prev = 0

        for level in levels:
            step = level - prev
            participants = [(pid, c, folded) for pid, c, folded in contrib if c >= level]
            amount = step * len(participants)
            eligible = [pid for pid, c, folded in participants if not folded]
            if amount > 0:
                pots.append({"amount": amount, "eligible": eligible})
            prev = level

        # Merge consecutive pots with identical eligible lists (e.g. folded player's
        # blind contribution creates a spurious extra level with the same two players)
        merged: list[dict] = []
        for pot in pots:
            if merged and merged[-1]["eligible"] == pot["eligible"]:
                merged[-1]["amount"] += pot["amount"]
            else:
                merged.append({"amount": pot["amount"], "eligible": list(pot["eligible"])})

        return merged

    def _do_showdown(self) -> list[GameEvent]:
        events = []
        self.state = GameState.SHOWDOWN

        # Run out remaining board cards with announcements (all-in scenario)
        if len(self.community) < 4 and self._deck:
            self.community.extend(self._deck.draw(1))
            events.append(GameEvent(type="dealer_message", data={
                "target": "all",
                "text": f"[DEALER] --- TURN: {' '.join(cards_to_str(self.community))} ---"
            }))
        if len(self.community) < 5 and self._deck:
            self.community.extend(self._deck.draw(1))
            events.append(GameEvent(type="dealer_message", data={
                "target": "all",
                "text": f"[DEALER] --- RIVER: {' '.join(cards_to_str(self.community))} ---"
            }))

        contenders = [p for p in self.players if p.status != PlayerStatus.FOLDED]

        # --- Single contender (everyone else folded) ---
        if len(contenders) == 1:
            winner = contenders[0]
            winner.stack += self.pot
            events.append(GameEvent(type="showdown", data={
                "winner_id": winner.id,
                "winner_ids": [winner.id],
                "split": False,
                "reason": "last_standing",
                "pot": self.pot,
                "pots": [{"amount": self.pot, "winner_ids": [winner.id], "split": False}],
                "total_won": {winner.id: self.pot},
                "hands": [{"player_id": winner.id,
                            "hole_cards": cards_to_str(winner.hole_cards), "rank": "—"}],
            }))
            events.append(self._state_event())
            return events

        # --- Evaluate all contender hands once ---
        hand_results: dict[int, tuple[int, str]] = {}
        for p in contenders:
            score, rank = evaluate_hand(p.hole_cards, self.community)
            hand_results[p.id] = (score, rank)

        # --- Award each pot ---
        # Side pots are only meaningful when someone went all-in.
        # Without all-ins, folded chips simply go to the best hand — one pot.
        has_allin = any(p.status == PlayerStatus.ALL_IN for p in self.players)
        if has_allin:
            pots = self._calculate_side_pots()
        else:
            pots = [{"amount": self.pot, "eligible": [p.id for p in contenders]}]
        total_won: dict[int, int] = {}
        pot_results: list[dict] = []

        for pot in pots:
            eligible = pot["eligible"]
            amount = pot["amount"]

            if len(eligible) == 0:
                # Everyone folded — give to last remaining contender
                last = contenders[-1]
                last.stack += amount
                total_won[last.id] = total_won.get(last.id, 0) + amount
                pot_results.append({"amount": amount, "winner_ids": [last.id], "split": False})
                continue

            if len(eligible) == 1:
                # Only one eligible player — they win uncontested
                pot_winner_ids = eligible
            else:
                # Best hand among eligible players
                eligible_scores = {pid: hand_results[pid][0]
                                   for pid in eligible if pid in hand_results}
                best_score = min(eligible_scores.values())
                pot_winner_ids = [pid for pid, score in eligible_scores.items()
                                  if score == best_score]

            share = amount // len(pot_winner_ids)
            remainder = amount % len(pot_winner_ids)
            for i, wid in enumerate(pot_winner_ids):
                won = share + (remainder if i == 0 else 0)
                self._get_player(wid).stack += won
                total_won[wid] = total_won.get(wid, 0) + won

            pot_results.append({
                "amount": amount,
                "winner_ids": pot_winner_ids,
                "split": len(pot_winner_ids) > 1,
            })

        # --- Build hands list for display ---
        hands = [
            {
                "player_id": p.id,
                "hole_cards": cards_to_str(p.hole_cards),
                "rank": hand_results[p.id][1] if p.id in hand_results else "—",
            }
            for p in contenders
        ]

        primary_winner = max(total_won, key=total_won.get) if total_won else contenders[0].id

        events.append(GameEvent(type="showdown", data={
            "winner_id": primary_winner,
            "winner_ids": list(total_won.keys()),
            "split": any(pr["split"] for pr in pot_results),
            "reason": "showdown",
            "pot": self.pot,
            "pots": pot_results,
            "total_won": total_won,
            "hands": hands,
        }))
        events.append(self._state_event())
        return events

    def _state_event(self) -> GameEvent:
        return GameEvent(type="state_update", data=self.public_state())

    def _dealer_prompt(self, player: Player) -> GameEvent:
        to_call = self._to_call(player)
        max_bet = max(p.street_bet for p in self.players)
        min_raise = max_bet + self.big_blind
        community_str = " ".join(cards_to_str(self.community)) if self.community else "—"

        if to_call > 0:
            options = f"/fold  /call {to_call}  /raise <total, min:{min_raise}>"
        else:
            options = f"/check  /raise <total, min:{min_raise}>"

        text = (
            f"[DEALER] Your turn. Street: {self.state.value} | "
            f"Pot: {self.pot} | Stack: {player.stack}\n"
            f"Community: {community_str}\n"
            f"Valid: {options}"
        )
        return GameEvent(type="dealer_message", data={"target": player.id, "text": text})
