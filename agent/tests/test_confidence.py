"""Tests for confidence gate."""

import pytest
from confidence import get_confidence, exploit_weight, should_exploit


class TestGetConfidence:
    def test_no_data(self):
        assert get_confidence(0) == "none"

    def test_few_hands(self):
        assert get_confidence(10) == "none"

    def test_low_confidence(self):
        assert get_confidence(50) == "low"

    def test_medium_confidence(self):
        assert get_confidence(200) == "medium"

    def test_high_confidence(self):
        assert get_confidence(600) == "high"

    def test_boundary_20(self):
        assert get_confidence(19) == "none"
        assert get_confidence(20) == "low"

    def test_boundary_100(self):
        assert get_confidence(99) == "low"
        assert get_confidence(100) == "medium"


class TestExploitWeight:
    def test_no_data_zero(self):
        assert exploit_weight(0) == 0.0

    def test_low_partial(self):
        assert exploit_weight(50) == 0.3

    def test_medium_partial(self):
        assert exploit_weight(200) == 0.7

    def test_high_full(self):
        assert exploit_weight(600) == 1.0


class TestShouldExploit:
    def test_no_data_no_exploit(self):
        assert not should_exploit(5, "low")

    def test_low_data_low_ok(self):
        assert should_exploit(30, "low")

    def test_low_data_medium_not_ok(self):
        assert not should_exploit(30, "medium")

    def test_high_data_any_ok(self):
        assert should_exploit(600, "high")
