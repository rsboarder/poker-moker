"""Tests for geometric bet sizing."""

import pytest
from bet_sizing import geometric_bet_size, calculate_spr


class TestGeometricBetSize:
    """Geometric sizing: equal % of pot each street to go all-in on river."""

    def test_three_streets_remaining(self):
        # pot=100, stack=400, 3 streets → each bet ~59% pot
        size = geometric_bet_size(pot=100, stack=400, streets_remaining=3)
        assert 50 <= size <= 70  # ~59% of pot

    def test_two_streets_remaining(self):
        # pot=200, stack=400, 2 streets → each bet ~62% pot
        size = geometric_bet_size(pot=200, stack=400, streets_remaining=2)
        assert 100 <= size <= 150

    def test_one_street_remaining(self):
        # pot=300, stack=200, 1 street → just shove
        size = geometric_bet_size(pot=300, stack=200, streets_remaining=1)
        assert size == 200  # all-in

    def test_already_pot_committed(self):
        # stack < pot → just go all-in
        size = geometric_bet_size(pot=500, stack=100, streets_remaining=2)
        assert size == 100  # all-in

    def test_very_deep_stacks(self):
        # pot=100, stack=2000, 3 streets → larger geometric size
        size = geometric_bet_size(pot=100, stack=2000, streets_remaining=3)
        assert size > 70  # needs to be bigger to get it all in

    def test_zero_streets_returns_zero(self):
        size = geometric_bet_size(pot=100, stack=500, streets_remaining=0)
        assert size == 0

    def test_zero_stack_returns_zero(self):
        size = geometric_bet_size(pot=100, stack=0, streets_remaining=2)
        assert size == 0

    def test_result_is_integer(self):
        size = geometric_bet_size(pot=150, stack=600, streets_remaining=2)
        assert isinstance(size, int)

    def test_never_exceeds_stack(self):
        for pot in [50, 100, 200, 500]:
            for stack in [30, 100, 500, 1000]:
                for streets in [1, 2, 3]:
                    size = geometric_bet_size(pot=pot, stack=stack, streets_remaining=streets)
                    assert size <= stack


class TestCalculateSPR:
    """Stack-to-pot ratio calculation."""

    def test_basic_spr(self):
        assert calculate_spr(stack=1000, pot=100) == 10.0

    def test_shallow_spr(self):
        assert calculate_spr(stack=100, pot=100) == 1.0

    def test_deep_spr(self):
        assert calculate_spr(stack=2000, pot=100) == 20.0

    def test_zero_pot_returns_high(self):
        # avoid division by zero — return large number
        spr = calculate_spr(stack=1000, pot=0)
        assert spr >= 100

    def test_zero_stack(self):
        assert calculate_spr(stack=0, pot=100) == 0.0
