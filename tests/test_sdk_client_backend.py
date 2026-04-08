"""Tests for ClaudeSDKClient integration methods on ClaudeCodeCLI.

Covers create_client(), run_completion_with_client(), and _make_can_use_tool().
All SDK interactions are mocked — no real subprocess or Anthropic credentials required.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.session_manager import Session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cli():
    """Create a ClaudeCodeCLI instance with auth mocked out."""
    with patch("src.auth.validate_claude_code_auth") as mock_validate:
        with patch("src.auth.auth_manager") as mock_auth:
            mock_validate.return_value = (True, {"method": "anthropic"})
            mock_auth.get_claude_code_env_vars.return_value = {
                "ANTHROPIC_AUTH_TOKEN": "test-key",
            }
            from src.backends.claude.client import ClaudeCodeCLI

            return ClaudeCodeCLI(cwd="/tmp")


# ---------------------------------------------------------------------------
# create_client
# ---------------------------------------------------------------------------


async def test_create_client_returns_connected_client():
    """create_client() calls ClaudeSDKClient(options=...) then connect(prompt=None)."""
    cli = _make_cli()
    session = Session(session_id="sess-1")

    mock_client_instance = AsyncMock()

    with patch("src.backends.claude.client.ClaudeSDKClient") as MockSDKClient:
        MockSDKClient.return_value = mock_client_instance
        client = await cli.create_client(session)

    # ClaudeSDKClient was constructed with an options object
    MockSDKClient.assert_called_once()
    call_kwargs = MockSDKClient.call_args
    assert "options" in call_kwargs.kwargs or len(call_kwargs.args) > 0

    # connect was called with prompt=None
    mock_client_instance.connect.assert_awaited_once_with(prompt=None)

    # Returns the client
    assert client is mock_client_instance


async def test_create_client_sets_can_use_tool():
    """create_client() sets options.can_use_tool before constructing the client."""
    cli = _make_cli()
    session = Session(session_id="sess-2")

    captured_options = {}

    with patch("src.backends.claude.client.ClaudeSDKClient") as MockSDKClient:
        mock_instance = AsyncMock()
        MockSDKClient.return_value = mock_instance

        def capture_init(**kwargs):
            captured_options.update(kwargs)
            return mock_instance

        MockSDKClient.side_effect = capture_init
        await cli.create_client(session)

    # The options object passed to ClaudeSDKClient must have can_use_tool set
    options = captured_options.get("options")
    assert options is not None
    assert options.can_use_tool is not None
    assert callable(options.can_use_tool)


async def test_create_client_passes_session_id_to_options():
    """create_client() passes session.session_id to _build_sdk_options."""
    cli = _make_cli()
    session = Session(session_id="my-session-id")

    with patch("src.backends.claude.client.ClaudeSDKClient") as MockSDKClient:
        mock_instance = AsyncMock()
        MockSDKClient.return_value = mock_instance

        captured_options = {}

        def capture_init(**kwargs):
            captured_options.update(kwargs)
            return mock_instance

        MockSDKClient.side_effect = capture_init
        await cli.create_client(session)

    options = captured_options.get("options")
    assert options is not None
    assert options.session_id == "my-session-id"


# ---------------------------------------------------------------------------
# run_completion_with_client
# ---------------------------------------------------------------------------


async def test_run_completion_with_client_yields_messages():
    """run_completion_with_client yields converted messages from client.receive_response()."""
    cli = _make_cli()
    session = Session(session_id="sess-3")

    # Build mock messages that SDK would return (SimpleNamespace mimics SDK objects)
    msg1 = SimpleNamespace(type="assistant", content="Hello")
    msg2 = SimpleNamespace(type="result", subtype="success", result="Done")

    mock_client = AsyncMock()

    async def mock_receive_response():
        yield msg1
        yield msg2

    mock_client.receive_response = mock_receive_response

    messages = []
    async for msg in cli.run_completion_with_client(mock_client, "Hi there", session):
        messages.append(msg)

    # query was called with prompt
    mock_client.query.assert_awaited_once_with("Hi there")

    # Two messages yielded
    assert len(messages) == 2
    assert messages[0]["type"] == "assistant"
    assert messages[1]["type"] == "result"


async def test_run_completion_with_client_error_clears_session_client():
    """On SDK error, session.client is set to None and error dict is yielded."""
    cli = _make_cli()
    session = Session(session_id="sess-err")
    session.client = MagicMock()

    mock_client = AsyncMock()
    mock_client.query.side_effect = RuntimeError("connection lost")

    messages = []
    async for msg in cli.run_completion_with_client(mock_client, "fail", session):
        messages.append(msg)

    assert len(messages) == 1
    assert messages[0]["type"] == "error"
    assert messages[0]["is_error"] is True
    assert "connection lost" in messages[0]["error"]
    # session.client cleared
    assert session.client is None


async def test_run_completion_with_client_error_during_receive():
    """An error during receive_response also clears client and yields error."""
    cli = _make_cli()
    session = Session(session_id="sess-recv-err")
    session.client = MagicMock()

    mock_client = AsyncMock()

    async def mock_receive_response():
        yield SimpleNamespace(type="assistant", content="partial")
        raise RuntimeError("stream broken")

    mock_client.receive_response = mock_receive_response

    messages = []
    async for msg in cli.run_completion_with_client(mock_client, "test", session):
        messages.append(msg)

    # First message is the partial assistant, then error
    assert len(messages) == 2
    assert messages[0]["type"] == "assistant"
    assert messages[1]["type"] == "error"
    assert session.client is None


# ---------------------------------------------------------------------------
# _make_can_use_tool callback
# ---------------------------------------------------------------------------


async def test_can_use_tool_callback_allows_other_tools():
    """Non-AskUserQuestion tools get immediate PermissionResultAllow."""
    from claude_agent_sdk.types import PermissionResultAllow, ToolPermissionContext

    cli = _make_cli()
    session = Session(session_id="sess-other-tool")

    callback = cli._make_can_use_tool(session)
    context = ToolPermissionContext(tool_use_id="tu_123")

    result = await callback("BashTool", {"command": "ls"}, context)

    assert isinstance(result, PermissionResultAllow)
    # Session fields should NOT be modified
    assert session.pending_tool_call is None
    assert session.input_event is None


async def test_can_use_tool_callback_ask_user_question():
    """AskUserQuestion callback sets pending_tool_call and waits for input_event."""
    from claude_agent_sdk.types import PermissionResultAllow, ToolPermissionContext

    cli = _make_cli()
    session = Session(session_id="sess-ask")

    callback = cli._make_can_use_tool(session)
    context = ToolPermissionContext(tool_use_id="tu_ask_1")
    tool_input = {"question": "Continue?"}

    # Run callback in a task — it will block on input_event.wait()
    result_holder = []

    async def run_callback():
        result = await callback("AskUserQuestion", tool_input, context)
        result_holder.append(result)

    task = asyncio.create_task(run_callback())

    # Allow the callback to start and park
    await asyncio.sleep(0.05)

    # Verify the session was updated with pending_tool_call
    assert session.pending_tool_call is not None
    assert session.pending_tool_call["call_id"] == "tu_ask_1"
    assert session.pending_tool_call["name"] == "AskUserQuestion"
    assert session.pending_tool_call["arguments"] == {"question": "Continue?"}

    # Verify input_event was created
    assert session.input_event is not None

    # Simulate the HTTP layer providing a response
    session.input_response = "Yes, continue"
    session.input_event.set()

    # Wait for callback to complete
    await task

    # Callback should have returned PermissionResultAllow
    assert len(result_holder) == 1
    assert isinstance(result_holder[0], PermissionResultAllow)

    # After callback completes, input_response and input_event are reset
    assert session.input_response is None
    assert session.input_event is None
