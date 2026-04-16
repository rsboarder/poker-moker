from treys import Card, Evaluator

_ev = Evaluator()


def str_to_card(s: str) -> int:
    """Convert string notation 'Ah' to treys int card."""
    return Card.new(s)


def cards_to_str(cards: list[int]) -> list[str]:
    """Convert list of treys int cards to string notation."""
    result = []
    for c in cards:
        result.append(Card.int_to_str(c))
    return result


def evaluate_hand(hole_cards: list[int], community: list[int]) -> tuple[int, str]:
    """
    hole_cards: list of treys int cards (2 cards)
    community:  list of treys int cards (3, 4, or 5 cards)
    Returns: (score, rank_string) — lower score = better hand
    """
    score = _ev.evaluate(community, hole_cards)
    rank = _ev.class_to_string(_ev.get_rank_class(score))
    return score, rank


def determine_winner(
    player_ids: list[int],
    hole_cards_map: dict[int, list[int]],
    community: list[int],
) -> tuple[list[int], dict[int, tuple[int, str]]]:
    """
    Returns (winner_ids, {player_id: (score, rank_string)})
    Lower treys score = better hand. Returns multiple IDs on a tie (split pot).
    """
    results = {}
    for pid in player_ids:
        score, rank = evaluate_hand(hole_cards_map[pid], community)
        results[pid] = (score, rank)

    best_score = min(results[pid][0] for pid in player_ids)
    winner_ids = [pid for pid in player_ids if results[pid][0] == best_score]
    return winner_ids, results
