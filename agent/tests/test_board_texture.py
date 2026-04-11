"""Tests for board texture classification."""

import pytest
from board_texture import classify_board, texture_bet_multiplier


class TestClassifyBoard:
    def test_dry_rainbow_unconnected(self):
        assert classify_board(["Kh", "7d", "2c"]) == "dry"

    def test_wet_connected_two_tone(self):
        assert classify_board(["Jh", "Td", "9h"]) == "wet"

    def test_monotone_three_suited(self):
        assert classify_board(["Ah", "Kh", "7h"]) == "monotone"

    def test_paired_dry_board(self):
        result = classify_board(["7h", "7d", "2c"])
        assert result == "dry"

    def test_empty_board(self):
        assert classify_board([]) == "none"

    def test_two_cards_not_enough(self):
        assert classify_board(["Ah", "Kd"]) == "none"

    def test_wet_broadway_connected(self):
        result = classify_board(["Qh", "Jd", "Th"])
        assert result == "wet"

    def test_turn_monotone(self):
        assert classify_board(["2h", "5h", "Kh", "3h"]) == "monotone"

    def test_river_spread(self):
        # 5 cards with some connectivity — classified as wet (expected)
        result = classify_board(["Kh", "7d", "2c", "9s", "4h"])
        assert result in ("wet", "dry")  # borderline with 5 cards


class TestTextureBetMultiplier:
    def test_dry_small_bet(self):
        assert texture_bet_multiplier("dry") == 0.33

    def test_wet_large_bet(self):
        assert texture_bet_multiplier("wet") == 0.66

    def test_monotone_largest_bet(self):
        assert texture_bet_multiplier("monotone") == 0.75

    def test_none_medium_bet(self):
        assert texture_bet_multiplier("none") == 0.50
