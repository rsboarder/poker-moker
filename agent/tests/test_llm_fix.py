"""Tests for LLM knowing-doing gap fixes."""

import pytest
from llm_engine import parse_llm_response, check_reasoning_action_consistency


class TestReasoningActionConsistency:
    """Detect when LLM reasoning contradicts its action."""

    def test_profitable_call_but_folds(self):
        reasoning = "Equity is 45% which exceeds pot odds of 25%. This is a profitable call."
        action = "fold"
        fixed = check_reasoning_action_consistency(reasoning, action, equity=0.45, pot_odds=0.25)
        assert fixed != "fold"  # should override to call

    def test_consistent_fold(self):
        reasoning = "Equity is 20%, pot odds 35%. Not profitable to continue."
        action = "fold"
        fixed = check_reasoning_action_consistency(reasoning, action, equity=0.20, pot_odds=0.35)
        assert fixed == "fold"  # correct, no override

    def test_consistent_call(self):
        reasoning = "Good pot odds, calling is profitable."
        action = "call"
        fixed = check_reasoning_action_consistency(reasoning, action, equity=0.50, pot_odds=0.25)
        assert fixed == "call"  # correct, no override

    def test_no_reasoning_no_change(self):
        fixed = check_reasoning_action_consistency(None, "fold", equity=0.45, pot_odds=0.25)
        assert fixed == "fold"  # can't check without reasoning

    def test_math_override_fold_to_call(self):
        # Reasoning says profitable but LLM folded — math should win
        reasoning = "The expected value is positive. I should call."
        action = "fold"
        fixed = check_reasoning_action_consistency(reasoning, action, equity=0.50, pot_odds=0.30)
        assert fixed == "call"

    def test_doesnt_override_raise_to_fold(self):
        # LLM wants to raise, equity is fine — don't downgrade
        reasoning = "Strong hand, should raise for value."
        action = "raise 100"
        fixed = check_reasoning_action_consistency(reasoning, action, equity=0.65, pot_odds=0.25)
        assert action == "raise 100"  # keep raise


class TestStructuredParsing:
    """Parse LLM response with answer tags."""

    def test_clean_answer_tags(self):
        raw = "<think>analysis here</think>\n<answer>call</answer>"
        action, reasoning = parse_llm_response(raw)
        assert action == "call"
        assert "analysis" in reasoning

    def test_raise_with_amount(self):
        raw = "<think>value bet</think>\n<answer>raise 150</answer>"
        action, reasoning = parse_llm_response(raw)
        assert action == "raise 150"

    def test_fold_in_answer(self):
        raw = "<think>weak hand</think>\n<answer>fold</answer>"
        action, reasoning = parse_llm_response(raw)
        assert action == "fold"

    def test_no_tags_fallback(self):
        raw = "I think calling is best.\ncall"
        action, reasoning = parse_llm_response(raw)
        assert action == "call"

    def test_contradictory_tags_uses_answer(self):
        # Reasoning mentions fold but answer says call
        raw = "<think>maybe fold... but pot odds good</think>\n<answer>call</answer>"
        action, reasoning = parse_llm_response(raw)
        assert action == "call"

    def test_empty_response_defaults_fold(self):
        action, reasoning = parse_llm_response("")
        assert action == "fold"

    def test_garbage_response_defaults_fold(self):
        action, reasoning = parse_llm_response("I'm not sure what to do here honestly")
        assert action == "fold"
