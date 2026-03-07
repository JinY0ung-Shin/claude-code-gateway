#!/usr/bin/env python3
"""
Unit tests for session routing and lifecycle.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.constants import DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_mock_msg(text="response"):
    """Build an assistant message dict."""
    return {
        "type": "assistant",
        "content": [{"type": "text", "text": text}],
    }


def _make_result_msg(text="response"):
    """Build a success result message dict."""
    return {
        "type": "result",
        "subtype": "success",
        "result": text,
        "stop_reason": "end_turn",
    }


def make_mock_run_completion(reply_text="response", error=None):
    """Factory that returns ``(mock_run_completion, captured_kwargs)``.

    Parameters
    ----------
    reply_text : str
        Text the mock assistant will yield.
    error : dict | None
        If provided, yield this single error message instead of the normal
        assistant + result pair.
    """
    captured_kwargs: dict = {}

    async def mock_run_completion(**kwargs):
        captured_kwargs.update(kwargs)
        if error is not None:
            yield error
            return
        yield _make_mock_msg(reply_text)
        yield _make_result_msg(reply_text)

    return mock_run_completion, captured_kwargs


class TestSessionRouting:
    """Test that session_id routes to fresh query() with session_id/resume."""

    @pytest.mark.asyncio
    async def test_streaming_with_session_id_uses_run_completion(self):
        """Streaming request with session_id uses run_completion with session_id param."""
        mock_run, captured_kwargs = make_mock_run_completion()

        with patch("src.main.claude_cli") as mock_cli:
            mock_cli.run_completion = mock_run
            mock_cli.parse_claude_message.return_value = "response"

            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="Hi")],
                session_id="test-routing-stream",
                stream=True,
            )

            chunks = []
            async for chunk in generate_streaming_response(req, "test-req-id"):
                chunks.append(chunk)

            # First turn: session_id should be passed (not resume)
            assert captured_kwargs.get("session_id") == "test-routing-stream"
            assert captured_kwargs.get("resume") is None
            assert any("data:" in c for c in chunks)

    @pytest.mark.asyncio
    async def test_streaming_without_session_id_uses_run_completion(self):
        """Streaming request without session_id uses run_completion (stateless)."""
        mock_run, captured_kwargs = make_mock_run_completion("hello")

        with patch("src.main.claude_cli") as mock_cli:
            mock_cli.run_completion = mock_run
            mock_cli.parse_claude_message.return_value = "hello"

            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="Hi")],
                stream=True,
            )

            chunks = []
            async for chunk in generate_streaming_response(req, "test-req-id"):
                chunks.append(chunk)

            assert any("data:" in c for c in chunks)
            # No session_id or resume in stateless mode
            assert captured_kwargs.get("session_id") is None
            assert captured_kwargs.get("resume") is None

    @pytest.mark.asyncio
    async def test_session_resume_on_second_turn(self):
        """Second turn with session_id uses resume instead of session_id."""
        from src.session_manager import session_manager

        # Simulate first turn already happened: session exists with messages
        session = session_manager.get_or_create_session("test-resume")
        from src.models import Message as Msg

        session.add_messages([Msg(role="user", content="first")])

        mock_run, captured_kwargs = make_mock_run_completion()

        with patch("src.main.claude_cli") as mock_cli:
            mock_cli.run_completion = mock_run
            mock_cli.parse_claude_message.return_value = "response"

            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="second turn")],
                session_id="test-resume",
                stream=True,
            )

            async for _ in generate_streaming_response(req, "test-req-id"):
                pass

            # Second turn: resume should be set, session_id should be None
            assert captured_kwargs.get("resume") == "test-resume"
            assert captured_kwargs.get("session_id") is None

    @pytest.mark.asyncio
    async def test_streaming_session_passes_system_prompt_only_on_first_turn(self):
        """Session mode forwards system_prompt on first turn and suppresses it on resume."""
        captured_calls = []

        async def mock_run_completion(**kwargs):
            captured_calls.append(dict(kwargs))
            yield _make_mock_msg()
            yield _make_result_msg()

        with patch(
            "src.main._build_claude_options",
            return_value={
                "model": DEFAULT_MODEL,
                "max_turns": 1,
                "system_prompt": "session system prompt",
                "disallowed_tools": [],
            },
        ), patch("src.main.claude_cli") as mock_cli:
            mock_cli.run_completion = mock_run_completion
            mock_cli.parse_claude_message.return_value = "response"

            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            first_req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[
                    Message(role="system", content="session system prompt"),
                    Message(role="user", content="first turn"),
                ],
                session_id="test-stream-system-prompt",
                stream=True,
            )
            async for _ in generate_streaming_response(first_req, "req-first"):
                pass

            second_req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="second turn")],
                session_id="test-stream-system-prompt",
                stream=True,
            )
            async for _ in generate_streaming_response(second_req, "req-second"):
                pass

            assert len(captured_calls) == 2
            assert captured_calls[0]["system_prompt"] == "session system prompt"
            assert captured_calls[0]["session_id"] == "test-stream-system-prompt"
            assert captured_calls[0]["resume"] is None
            assert captured_calls[1]["system_prompt"] is None
            assert captured_calls[1]["session_id"] is None
            assert captured_calls[1]["resume"] == "test-stream-system-prompt"


class TestAsyncShutdownIntegration:
    """Test that lifespan uses async_shutdown."""

    @pytest.mark.asyncio
    async def test_lifespan_calls_async_shutdown(self):
        """Lifespan shutdown calls session_manager.async_shutdown()."""
        with patch("src.main.session_manager") as mock_sm:
            mock_sm.start_cleanup_task = MagicMock()
            mock_sm.async_shutdown = AsyncMock()

            with patch(
                "src.main.validate_claude_code_auth",
                return_value=(True, {"method": "test"}),
            ):
                with patch("src.main.claude_cli") as mock_cli:
                    mock_cli.verify_cli = AsyncMock(return_value=True)

                    from src.main import lifespan, app

                    async with lifespan(app):
                        pass

                    mock_sm.async_shutdown.assert_called_once()


class TestNonStreamingSessionRequest:
    """Test non-streaming (stream=False) with session_id returns proper JSON."""

    @pytest.mark.asyncio
    async def test_non_streaming_session_returns_json_response(self):
        """Non-streaming request with session_id should return ChatCompletionResponse."""
        mock_run, captured_kwargs = make_mock_run_completion("non-stream reply")

        with (
            patch("src.main.claude_cli") as mock_cli,
            patch(
                "src.main.validate_claude_code_auth",
                return_value=(True, {"method": "test"}),
            ),
            patch("src.main.verify_api_key", new_callable=AsyncMock),
        ):
            mock_cli.run_completion = mock_run
            mock_cli.parse_claude_message.return_value = "non-stream reply"
            mock_cli.estimate_token_usage.return_value = {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            }

            from src.main import app
            from httpx import AsyncClient, ASGITransport

            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": DEFAULT_MODEL,
                        "messages": [{"role": "user", "content": "Hello"}],
                        "stream": False,
                        "session_id": "test-non-stream-session",
                    },
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["id"].startswith("chatcmpl-")
            assert data["choices"][0]["message"]["role"] == "assistant"
            assert data["choices"][0]["message"]["content"] == "non-stream reply"
            assert "usage" in data

            # Verify session_id was passed (first turn, so session_id not resume)
            assert captured_kwargs.get("session_id") == "test-non-stream-session"
            assert captured_kwargs.get("resume") is None
            assert captured_kwargs.get("stream") is False

    @pytest.mark.asyncio
    async def test_non_streaming_session_error_result_returns_500(self):
        """Non-streaming session requests return 500 when SDK yields only an error result."""
        mock_run, captured_kwargs = make_mock_run_completion(
            error={
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "error_message": "SDK failed",
            }
        )

        with patch("src.main.claude_cli") as mock_cli, patch(
            "src.main.validate_claude_code_auth", return_value=(True, {"method": "test"})
        ), patch("src.main.verify_api_key", new_callable=AsyncMock):
            mock_cli.run_completion = mock_run
            mock_cli.parse_claude_message.return_value = None

            from src.main import app
            from httpx import AsyncClient, ASGITransport

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": DEFAULT_MODEL,
                        "messages": [{"role": "user", "content": "Hello"}],
                        "stream": False,
                        "session_id": "test-non-stream-error",
                    },
                )

            assert resp.status_code == 500
            assert resp.json()["error"]["message"] == "No response from Claude Code"
            assert captured_kwargs.get("session_id") == "test-non-stream-error"
            assert captured_kwargs.get("resume") is None
            assert captured_kwargs.get("stream") is False

    @pytest.mark.asyncio
    async def test_non_streaming_session_resume_on_second_turn(self):
        """Second non-streaming turn uses resume instead of session_id."""
        from src.session_manager import session_manager
        from src.models import Message

        session = session_manager.get_or_create_session("test-non-stream-resume")
        session.add_messages([Message(role="user", content="first turn")])

        mock_run, captured_kwargs = make_mock_run_completion("follow-up reply")

        with patch("src.main.claude_cli") as mock_cli, patch(
            "src.main.validate_claude_code_auth", return_value=(True, {"method": "test"})
        ), patch("src.main.verify_api_key", new_callable=AsyncMock):
            mock_cli.run_completion = mock_run
            mock_cli.parse_claude_message.return_value = "follow-up reply"

            from src.main import app
            from httpx import AsyncClient, ASGITransport

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": DEFAULT_MODEL,
                        "messages": [{"role": "user", "content": "second turn"}],
                        "stream": False,
                        "session_id": "test-non-stream-resume",
                    },
                )

            assert resp.status_code == 200
            assert resp.json()["choices"][0]["message"]["content"] == "follow-up reply"
            assert captured_kwargs.get("resume") == "test-non-stream-resume"
            assert captured_kwargs.get("session_id") is None
            assert captured_kwargs.get("stream") is False


class TestSessionEnableTools:
    """Test that enable_tools=True passes allowed_tools and permission_mode."""

    @pytest.mark.asyncio
    async def test_enable_tools_passes_allowed_tools_and_permission(self):
        """When enable_tools=True, allowed_tools and permission_mode are forwarded."""
        from src.constants import DEFAULT_ALLOWED_TOOLS

        mock_run, captured_kwargs = make_mock_run_completion("tool reply")

        with patch("src.main.claude_cli") as mock_cli:
            mock_cli.run_completion = mock_run
            mock_cli.parse_claude_message.return_value = "tool reply"

            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="Use tools")],
                session_id="test-tools-session",
                stream=True,
                enable_tools=True,
            )

            async for _ in generate_streaming_response(req, "test-req-id"):
                pass

            assert captured_kwargs.get("allowed_tools") == DEFAULT_ALLOWED_TOOLS
            assert captured_kwargs.get("permission_mode") is not None
            # disallowed_tools should NOT be set when tools are enabled
            assert captured_kwargs.get("disallowed_tools") is None

    @pytest.mark.asyncio
    async def test_disable_tools_passes_disallowed_tools(self):
        """When enable_tools=False (default), disallowed_tools are set."""
        mock_run, captured_kwargs = make_mock_run_completion("no-tool reply")

        with patch("src.main.claude_cli") as mock_cli:
            mock_cli.run_completion = mock_run
            mock_cli.parse_claude_message.return_value = "no-tool reply"

            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="No tools")],
                session_id="test-no-tools-session",
                stream=True,
                enable_tools=False,
            )

            async for _ in generate_streaming_response(req, "test-req-id"):
                pass

            assert captured_kwargs.get("disallowed_tools") is not None
            assert captured_kwargs.get("allowed_tools") is None
            assert captured_kwargs.get("max_turns") == 1


class TestStreamingErrorHandling:
    """Test that errors in session mode still emit valid SSE."""

    @pytest.mark.asyncio
    async def test_streaming_error_emits_valid_sse(self):
        """When run_completion raises, the stream should emit a valid SSE error event."""

        async def mock_run_completion(**kwargs):
            raise RuntimeError("SDK connection failed")
            # Make it an async generator
            yield  # pragma: no cover

        with patch("src.main.claude_cli") as mock_cli:
            mock_cli.run_completion = mock_run_completion

            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="Hi")],
                session_id="test-error-session",
                stream=True,
            )

            chunks = []
            async for chunk in generate_streaming_response(req, "test-req-id"):
                chunks.append(chunk)

            # Should have at least one SSE data line with error
            assert len(chunks) > 0
            error_chunks = [c for c in chunks if "error" in c]
            assert len(error_chunks) > 0
            # Verify it is valid SSE format (starts with "data: ")
            for ec in error_chunks:
                assert ec.startswith("data: ")


class TestAssistantResponseStoredInSession:
    """Test that assistant response is stored via session_manager.add_assistant_response."""

    @pytest.mark.asyncio
    async def test_streaming_stores_assistant_response(self):
        """After streaming completes, add_assistant_response is called with the right session_id."""
        mock_run, _ = make_mock_run_completion("stored reply")

        with (
            patch("src.main.claude_cli") as mock_cli,
            patch("src.main.session_manager") as mock_sm,
        ):
            mock_cli.run_completion = mock_run
            mock_cli.parse_claude_message.return_value = "stored reply"

            # get_or_create_session must return a mock session
            mock_session = MagicMock()
            mock_session.messages = []  # first turn (new session)
            mock_sm.get_or_create_session.return_value = mock_session

            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="Store this")],
                session_id="test-store-session",
                stream=True,
            )

            async for _ in generate_streaming_response(req, "test-req-id"):
                pass

            # Verify add_assistant_response was called with correct session_id
            mock_sm.add_assistant_response.assert_called_once()
            call_args = mock_sm.add_assistant_response.call_args
            assert call_args[0][0] == "test-store-session"
            assert call_args[0][1].role == "assistant"
            assert call_args[0][1].content == "stored reply"

    @pytest.mark.asyncio
    async def test_no_session_skips_add_assistant_response(self):
        """Stateless request (no session_id) should NOT call add_assistant_response."""
        mock_run, _ = make_mock_run_completion("stateless")

        with (
            patch("src.main.claude_cli") as mock_cli,
            patch("src.main.session_manager") as mock_sm,
        ):
            mock_cli.run_completion = mock_run
            mock_cli.parse_claude_message.return_value = "stateless"

            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="No session")],
                stream=True,
            )

            async for _ in generate_streaming_response(req, "test-req-id"):
                pass

            mock_sm.add_assistant_response.assert_not_called()


class TestMultipleSessionsIsolation:
    """Test that two different session_ids do not interfere with each other."""

    @pytest.mark.asyncio
    async def test_two_sessions_are_isolated(self):
        """Messages from session A should not appear in session B."""
        from src.session_manager import session_manager
        from src.models import Message

        # Create two independent sessions
        session_a = session_manager.get_or_create_session("test-iso-a")
        session_b = session_manager.get_or_create_session("test-iso-b")

        session_a.add_messages([Message(role="user", content="I am session A")])
        session_b.add_messages([Message(role="user", content="I am session B")])

        # Verify isolation
        assert len(session_a.messages) == 1
        assert len(session_b.messages) == 1
        assert session_a.messages[0].content == "I am session A"
        assert session_b.messages[0].content == "I am session B"

        # Add more to A, B should be unaffected
        session_a.add_messages([Message(role="assistant", content="Hello A")])
        assert len(session_a.messages) == 2
        assert len(session_b.messages) == 1

    @pytest.mark.asyncio
    async def test_deleting_one_session_does_not_affect_other(self):
        """Deleting session A should not affect session B."""
        from src.session_manager import session_manager
        from src.models import Message

        session_manager.get_or_create_session("test-del-a")
        session_b = session_manager.get_or_create_session("test-del-b")
        session_b.add_messages([Message(role="user", content="B content")])

        session_manager.delete_session("test-del-a")

        # Session B should still exist and be intact
        retrieved_b = session_manager.get_session("test-del-b")
        assert retrieved_b is not None
        assert len(retrieved_b.messages) == 1
        assert retrieved_b.messages[0].content == "B content"

        # Session A should be gone
        assert session_manager.get_session("test-del-a") is None

    @pytest.mark.asyncio
    async def test_concurrent_sessions_streaming_isolation(
        self, isolated_session_manager
    ):
        """Two sessions streaming concurrently stay isolated under asyncio.gather()."""

        captured_kwargs = {}
        max_inflight = 0
        inflight_sessions = set()
        state_lock = asyncio.Lock()
        both_started = asyncio.Event()

        async def mock_run_completion(**kwargs):
            nonlocal max_inflight

            session_key = kwargs.get("session_id") or kwargs.get("resume")
            prompt = kwargs["prompt"]
            reply_text = f"reply-for-{session_key}"

            captured_kwargs[session_key] = dict(kwargs)

            async with state_lock:
                inflight_sessions.add(session_key)
                max_inflight = max(max_inflight, len(inflight_sessions))
                if len(inflight_sessions) == 2:
                    both_started.set()

            await asyncio.wait_for(both_started.wait(), timeout=1)

            try:
                yield {
                    "type": "assistant",
                    "content": [{"type": "text", "text": reply_text}],
                }
                await asyncio.sleep(0)
                yield {
                    "type": "result",
                    "subtype": "success",
                    "result": reply_text,
                    "stop_reason": "end_turn",
                }
            finally:
                async with state_lock:
                    inflight_sessions.discard(session_key)

        def parse_claude_message_side_effect(messages):
            for message in reversed(messages):
                if message.get("subtype") == "success":
                    return message["result"]
            return None

        async def collect_chunks(request, request_id):
            from src.main import generate_streaming_response

            return [
                chunk
                async for chunk in generate_streaming_response(request, request_id)
            ]

        with patch("src.main.claude_cli") as mock_cli:
            mock_cli.run_completion = mock_run_completion
            mock_cli.parse_claude_message.side_effect = parse_claude_message_side_effect

            from src.models import ChatCompletionRequest, Message

            req_a = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="From A")],
                session_id="test-concurrent-a",
                stream=True,
            )
            req_b = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="From B")],
                session_id="test-concurrent-b",
                stream=True,
            )

            chunks_a, chunks_b = await asyncio.gather(
                collect_chunks(req_a, "req-a"),
                collect_chunks(req_b, "req-b"),
            )

        assert max_inflight == 2

        assert captured_kwargs["test-concurrent-a"]["session_id"] == "test-concurrent-a"
        assert captured_kwargs["test-concurrent-b"]["session_id"] == "test-concurrent-b"
        assert captured_kwargs["test-concurrent-a"]["prompt"] == "From A"
        assert captured_kwargs["test-concurrent-b"]["prompt"] == "From B"

        joined_a = "".join(chunks_a)
        joined_b = "".join(chunks_b)
        assert "reply-for-test-concurrent-a" in joined_a
        assert "reply-for-test-concurrent-b" not in joined_a
        assert "reply-for-test-concurrent-b" in joined_b
        assert "reply-for-test-concurrent-a" not in joined_b

        session_a = isolated_session_manager.get_session("test-concurrent-a")
        session_b = isolated_session_manager.get_session("test-concurrent-b")
        assert session_a is not None
        assert session_b is not None
        assert [msg.content for msg in session_a.messages] == [
            "From A",
            "reply-for-test-concurrent-a",
        ]
        assert [msg.content for msg in session_b.messages] == [
            "From B",
            "reply-for-test-concurrent-b",
        ]
