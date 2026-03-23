#!/usr/bin/env python3
"""
Unit tests for src/parameter_validator.py

Tests the ParameterValidator and CompatibilityReporter classes.
These are pure unit tests that don't require a running server.
"""

import pytest
from unittest.mock import patch

from src.constants import DEFAULT_MODEL
from src.parameter_validator import ParameterValidator, CompatibilityReporter
from src.models import Message, ChatCompletionRequest


class TestParameterValidatorValidateModel:
    """Test ParameterValidator.validate_model()"""

    def test_valid_model_returns_true(self):
        """Known supported model returns True."""
        result = ParameterValidator.validate_model(DEFAULT_MODEL)
        assert result is True

    def test_unknown_model_returns_false_with_warning(self):
        """Unknown model returns False with warning logged."""
        with patch("src.parameter_validator.logger") as mock_logger:
            result = ParameterValidator.validate_model("unknown-model-xyz")
            assert result is False
            mock_logger.warning.assert_called_once()
            assert "unknown-model-xyz" in str(mock_logger.warning.call_args)

    def test_all_known_models_valid(self):
        """All models in supported models set are valid."""
        for model in ParameterValidator._get_supported_models():
            assert ParameterValidator.validate_model(model) is True


class TestParameterValidatorExtractClaudeHeaders:
    """Test ParameterValidator.extract_claude_headers()"""

    def test_empty_headers_returns_empty_dict(self):
        """Empty headers dict returns empty options dict."""
        result = ParameterValidator.extract_claude_headers({})
        assert result == {}

    def test_extracts_max_turns(self):
        """X-Claude-Max-Turns header is extracted correctly."""
        headers = {"x-claude-max-turns": "10"}
        result = ParameterValidator.extract_claude_headers(headers)
        assert result.get("max_turns") == 10

    def test_invalid_max_turns_logs_warning(self):
        """Invalid X-Claude-Max-Turns logs warning and is ignored."""
        headers = {"x-claude-max-turns": "not-a-number"}
        with patch("src.parameter_validator.logger") as mock_logger:
            result = ParameterValidator.extract_claude_headers(headers)
            assert "max_turns" not in result
            mock_logger.warning.assert_called_once()

    def test_extracts_allowed_tools(self):
        """X-Claude-Allowed-Tools header is extracted correctly."""
        headers = {"x-claude-allowed-tools": "Read,Write,Bash"}
        result = ParameterValidator.extract_claude_headers(headers)
        assert result.get("allowed_tools") == ["Read", "Write", "Bash"]

    def test_allowed_tools_strips_whitespace(self):
        """Tool names have whitespace stripped."""
        headers = {"x-claude-allowed-tools": " Read , Write , Bash "}
        result = ParameterValidator.extract_claude_headers(headers)
        assert result.get("allowed_tools") == ["Read", "Write", "Bash"]

    def test_extracts_disallowed_tools(self):
        """X-Claude-Disallowed-Tools header is extracted correctly."""
        headers = {"x-claude-disallowed-tools": "Edit,Delete"}
        result = ParameterValidator.extract_claude_headers(headers)
        assert result.get("disallowed_tools") == ["Edit", "Delete"]

    def test_extracts_permission_mode(self):
        """X-Claude-Permission-Mode header is extracted correctly."""
        headers = {"x-claude-permission-mode": "bypassPermissions"}
        result = ParameterValidator.extract_claude_headers(headers)
        assert result.get("permission_mode") == "bypassPermissions"

    def test_extracts_multiple_headers(self):
        """Multiple Claude headers are all extracted."""
        headers = {
            "x-claude-max-turns": "5",
            "x-claude-allowed-tools": "Read,Write",
            "x-claude-permission-mode": "default",
        }
        result = ParameterValidator.extract_claude_headers(headers)
        assert result.get("max_turns") == 5
        assert result.get("allowed_tools") == ["Read", "Write"]
        assert result.get("permission_mode") == "default"


class TestCompatibilityReporter:
    """Test CompatibilityReporter.generate_compatibility_report()"""

    @pytest.fixture
    def minimal_request(self):
        """Request with minimal parameters."""
        return ChatCompletionRequest(
            model=DEFAULT_MODEL,
            messages=[Message(role="user", content="Hello")],
        )

    def test_supported_parameters_identified(self, minimal_request):
        """Model and messages are identified as supported."""
        report = CompatibilityReporter.generate_compatibility_report(minimal_request)
        assert "model" in report["supported_parameters"]
        assert "messages" in report["supported_parameters"]

    def test_stream_identified_as_supported(self):
        """Stream parameter is identified as supported."""
        request = ChatCompletionRequest(
            model=DEFAULT_MODEL,
            messages=[Message(role="user", content="Hello")],
            stream=True,
        )
        report = CompatibilityReporter.generate_compatibility_report(request)
        assert "stream" in report["supported_parameters"]

    def test_user_identified_as_supported(self):
        """User parameter is identified as supported (for logging)."""
        request = ChatCompletionRequest(
            model=DEFAULT_MODEL,
            messages=[Message(role="user", content="Hello")],
            user="test_user",
        )
        report = CompatibilityReporter.generate_compatibility_report(request)
        assert "user (for logging)" in report["supported_parameters"]

    def test_temperature_unsupported_when_not_default(self):
        """Non-default temperature is flagged as unsupported."""
        request = ChatCompletionRequest(
            model=DEFAULT_MODEL,
            messages=[Message(role="user", content="Hello")],
            temperature=0.8,
        )
        report = CompatibilityReporter.generate_compatibility_report(request)
        assert "temperature" in report["unsupported_parameters"]
        assert len(report["suggestions"]) > 0

    def test_top_p_unsupported_when_not_default(self):
        """Non-default top_p is flagged as unsupported."""
        request = ChatCompletionRequest(
            model=DEFAULT_MODEL,
            messages=[Message(role="user", content="Hello")],
            top_p=0.9,
        )
        report = CompatibilityReporter.generate_compatibility_report(request)
        assert "top_p" in report["unsupported_parameters"]

    def test_max_tokens_unsupported(self):
        """max_tokens is flagged as unsupported."""
        request = ChatCompletionRequest(
            model=DEFAULT_MODEL,
            messages=[Message(role="user", content="Hello")],
            max_tokens=500,
        )
        report = CompatibilityReporter.generate_compatibility_report(request)
        assert "max_tokens" in report["unsupported_parameters"]

    def test_stop_sequences_unsupported(self):
        """stop sequences are flagged as unsupported."""
        request = ChatCompletionRequest(
            model=DEFAULT_MODEL,
            messages=[Message(role="user", content="Hello")],
            stop=["END"],
        )
        report = CompatibilityReporter.generate_compatibility_report(request)
        assert "stop" in report["unsupported_parameters"]

    def test_penalties_unsupported(self):
        """presence_penalty and frequency_penalty are flagged as unsupported."""
        request = ChatCompletionRequest(
            model=DEFAULT_MODEL,
            messages=[Message(role="user", content="Hello")],
            presence_penalty=0.5,
            frequency_penalty=0.5,
        )
        report = CompatibilityReporter.generate_compatibility_report(request)
        assert "presence_penalty" in report["unsupported_parameters"]
        assert "frequency_penalty" in report["unsupported_parameters"]

    def test_logit_bias_unsupported(self):
        """logit_bias is flagged as unsupported."""
        request = ChatCompletionRequest(
            model=DEFAULT_MODEL,
            messages=[Message(role="user", content="Hello")],
            logit_bias={"hello": 2.0},
        )
        report = CompatibilityReporter.generate_compatibility_report(request)
        assert "logit_bias" in report["unsupported_parameters"]

    def test_report_has_all_sections(self, minimal_request):
        """Report contains all expected sections."""
        report = CompatibilityReporter.generate_compatibility_report(minimal_request)
        assert "supported_parameters" in report
        assert "unsupported_parameters" in report
        assert "warnings" in report
        assert "suggestions" in report

    def test_minimal_request_has_no_unsupported(self, minimal_request):
        """Minimal request with defaults has no unsupported parameters."""
        report = CompatibilityReporter.generate_compatibility_report(minimal_request)
        assert len(report["unsupported_parameters"]) == 0
