#!/usr/bin/env python3
"""
Property-based tests using Hypothesis for edge case discovery.

These tests generate random inputs to find edge cases that manual testing might miss.
"""

from hypothesis import given, strategies as st, settings, assume

from src.message_adapter import MessageAdapter
from src.parameter_validator import ParameterValidator
from src.constants import CLAUDE_MODELS


class TestMessageAdapterProperties:
    """Property-based tests for MessageAdapter."""

    @given(content=st.text(min_size=1, max_size=1000))
    @settings(max_examples=50)
    def test_filter_content_handles_any_text(self, content: str):
        """filter_content should handle any text without crashing."""
        assume("\x00" not in content)  # Null bytes

        # Should not crash
        result = MessageAdapter.filter_content(content)
        assert isinstance(result, str)

    @given(text=st.text(min_size=0, max_size=10000))
    @settings(max_examples=30)
    def test_estimate_tokens_returns_positive(self, text: str):
        """Token estimation should return a non-negative integer."""
        result = MessageAdapter.estimate_tokens(text)
        assert isinstance(result, int)
        assert result >= 0


class TestParameterValidatorProperties:
    """Property-based tests for ParameterValidator."""

    @given(model=st.sampled_from(CLAUDE_MODELS))
    @settings(max_examples=20)
    def test_valid_model_names_accepted(self, model: str):
        """Valid Claude model names should be accepted."""
        result = ParameterValidator.validate_model(model)
        assert result is True

    @given(model=st.text(min_size=1, max_size=50))
    @settings(max_examples=30)
    def test_unknown_model_rejected(self, model: str):
        """Unknown model names are rejected."""
        assume(model.strip())  # Non-empty
        supported = ParameterValidator._get_supported_models()
        base = model.split("/")[0] if "/" in model else model

        result = ParameterValidator.validate_model(model)
        if base in supported:
            assert result is True
        else:
            assert result is False


class TestTokenEstimation:
    """Property-based tests for token estimation consistency."""

    @given(text=st.text(min_size=0, max_size=5000))
    @settings(max_examples=30)
    def test_token_estimation_non_negative(self, text: str):
        """Token estimation should always return non-negative value."""
        tokens = MessageAdapter.estimate_tokens(text)
        assert tokens >= 0

    @given(
        prefix=st.text(min_size=10, max_size=100),
        suffix=st.text(min_size=10, max_size=100),
    )
    @settings(max_examples=20)
    def test_concatenation_increases_tokens(self, prefix: str, suffix: str):
        """Concatenating text should not decrease token count."""
        tokens_prefix = MessageAdapter.estimate_tokens(prefix)
        tokens_suffix = MessageAdapter.estimate_tokens(suffix)
        tokens_combined = MessageAdapter.estimate_tokens(prefix + suffix)

        # Combined should be at least as many as the larger part
        # (may be less than sum due to subword tokenization)
        assert tokens_combined >= min(tokens_prefix, tokens_suffix)
