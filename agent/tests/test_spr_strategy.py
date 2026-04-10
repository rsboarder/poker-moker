"""Tests for SPR-aware strategy adjustments."""

import pytest
from bet_sizing import calculate_spr, get_spr_category, spr_adjust_action


class TestSPRCategory:
    def test_shallow(self):
        assert get_spr_category(1.5) == "shallow"

    def test_medium(self):
        assert get_spr_category(4.0) == "medium"

    def test_deep(self):
        assert get_spr_category(16.0) == "deep"

    def test_boundary_shallow_medium(self):
        assert get_spr_category(3.0) == "shallow"
        assert get_spr_category(3.1) == "medium"

    def test_boundary_medium_deep(self):
        assert get_spr_category(12.0) == "medium"
        assert get_spr_category(13.0) == "deep"


class TestSPRAdjustAction:
    """SPR should influence the final action."""

    def test_shallow_top_pair_commits(self):
        # SPR ~2, top pair → should commit (raise/all-in)
        action = spr_adjust_action(
            base_action="call", equity=0.55, spr=2.0, can_raise=True, min_raise=50
        )
        assert "raise" in action or action == "call"  # at minimum don't fold

    def test_shallow_never_fold_decent_hand(self):
        # SPR ~1, any reasonable equity → don't fold
        action = spr_adjust_action(
            base_action="fold", equity=0.40, spr=1.0, can_raise=False, min_raise=None
        )
        assert action != "fold"

    def test_deep_top_pair_doesnt_overcommit(self):
        # SPR ~20, marginal hand → should NOT raise big
        action = spr_adjust_action(
            base_action="raise 500", equity=0.55, spr=20.0, can_raise=True, min_raise=50
        )
        # Should downgrade to call or smaller raise, not overcommit
        assert action != "raise 500"

    def test_medium_spr_no_change(self):
        # SPR ~5, standard play → no adjustment
        action = spr_adjust_action(
            base_action="call", equity=0.50, spr=5.0, can_raise=True, min_raise=50
        )
        assert action == "call"

    def test_preserves_strong_hands(self):
        # Even deep, very strong hand stays aggressive
        action = spr_adjust_action(
            base_action="raise 200", equity=0.85, spr=20.0, can_raise=True, min_raise=50
        )
        assert "raise" in action
