# ClaudeSDKClient Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Migrate session-based requests from `query()` to `ClaudeSDKClient` for native session continuity, keeping stateless requests unchanged.

**Architecture:** Incremental migration — `session_id` present routes to new `ClaudeSDKClient` path, absent keeps existing `query()` path. Client lifecycle integrated into `SessionManager`. Message history kept for API compatibility.

**Tech Stack:** claude-agent-sdk 0.1.46 (`ClaudeSDKClient`, `ClaudeAgentOptions`), FastAPI, asyncio

---

### Task 1: Add `client` field and `asyncio.Lock` to Session

**Files:**
- Modify: `src/session_manager.py:1-56`
- Test: `tests/test_session_manager_unit.py`

**Step 1: Write the failing tests**

Add to `tests/test_session_manager_unit.py`:

```python
class TestSessionClientField:
    """Test Session.client field and lock."""

    def test_session_has_client_field_default_none(self):
        """Session.client defaults to None."""
        session = Session(session_id="test-123")
        assert session.client is None

    def test_session_client_can_be_set(self):
        """Session.client can be assigned."""
        session = Session(session_id="test-123")
        mock_client = MagicMock()
        session.client = mock_client
        assert session.client is mock_client

    def test_session_has_lock(self):
        """Session has an asyncio.Lock."""
        session = Session(session_id="test-123")
        assert hasattr(session, "lock")
        assert isinstance(session.lock, asyncio.Lock)
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m pytest tests/test_session_manager_unit.py::TestSessionClientField -v`
Expected: FAIL — `Session` has no `client` attribute, no `lock` attribute

**Step 3: Write minimal implementation**

In `src/session_manager.py`, add imports and modify `Session`:

```python
import asyncio
# ... existing imports ...
from typing import Any, Dict, List, Optional, Tuple

@dataclass
class Session:
    session_id: str
    ttl_minutes: int = 60
    messages: List[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_accessed: datetime = field(default_factory=datetime.utcnow)
    expires_at: datetime = field(default=None)
    client: Optional[Any] = field(default=None)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m pytest tests/test_session_manager_unit.py::TestSessionClientField -v`
Expected: PASS

**Step 5: Run all existing session_manager tests**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m pytest tests/test_session_manager_unit.py -v`
Expected: All PASS (existing tests unaffected)

**Step 6: Commit**

```bash
git add src/session_manager.py tests/test_session_manager_unit.py
git commit -m "feat: add client and lock fields to Session dataclass"
```

---

### Task 2: Convert cleanup and shutdown to async with client disconnect

**Files:**
- Modify: `src/session_manager.py:58-224`
- Test: `tests/test_session_manager_unit.py`

**Step 1: Write the failing tests**

Add to `tests/test_session_manager_unit.py`:

```python
class TestSessionManagerAsyncCleanup:
    """Test async cleanup with client disconnect."""

    @pytest.fixture
    def manager(self):
        return SessionManager(default_ttl_minutes=60, cleanup_interval_minutes=5)

    @pytest.mark.asyncio
    async def test_cleanup_disconnects_expired_client(self, manager):
        """Cleanup calls disconnect() on expired session's client."""
        session = manager.get_or_create_session("with-client")
        mock_client = AsyncMock()
        session.client = mock_client
        session.expires_at = datetime.utcnow() - timedelta(hours=1)

        await manager.cleanup_expired_sessions()

        mock_client.disconnect.assert_called_once()
        assert "with-client" not in manager.sessions

    @pytest.mark.asyncio
    async def test_cleanup_handles_disconnect_error(self, manager):
        """Cleanup handles disconnect errors gracefully."""
        session = manager.get_or_create_session("error-client")
        mock_client = AsyncMock()
        mock_client.disconnect.side_effect = RuntimeError("connection lost")
        session.client = mock_client
        session.expires_at = datetime.utcnow() - timedelta(hours=1)

        await manager.cleanup_expired_sessions()

        assert "error-client" not in manager.sessions

    @pytest.mark.asyncio
    async def test_cleanup_skips_sessions_without_client(self, manager):
        """Cleanup works for expired sessions without a client."""
        session = manager.get_or_create_session("no-client")
        session.expires_at = datetime.utcnow() - timedelta(hours=1)

        await manager.cleanup_expired_sessions()

        assert "no-client" not in manager.sessions

    @pytest.mark.asyncio
    async def test_async_shutdown_disconnects_all_clients(self, manager):
        """async_shutdown disconnects all active clients."""
        s1 = manager.get_or_create_session("s1")
        s2 = manager.get_or_create_session("s2")
        mock_c1 = AsyncMock()
        mock_c2 = AsyncMock()
        s1.client = mock_c1
        s2.client = mock_c2

        await manager.async_shutdown()

        mock_c1.disconnect.assert_called_once()
        mock_c2.disconnect.assert_called_once()
        assert len(manager.sessions) == 0

    @pytest.mark.asyncio
    async def test_async_shutdown_handles_disconnect_error(self, manager):
        """async_shutdown handles disconnect errors gracefully."""
        s1 = manager.get_or_create_session("s1")
        mock_c1 = AsyncMock()
        mock_c1.disconnect.side_effect = RuntimeError("fail")
        s1.client = mock_c1

        await manager.async_shutdown()

        assert len(manager.sessions) == 0
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m pytest tests/test_session_manager_unit.py::TestSessionManagerAsyncCleanup -v`
Expected: FAIL — `cleanup_expired_sessions` and `async_shutdown` don't exist

**Step 3: Write minimal implementation**

In `src/session_manager.py`, add to `SessionManager`:

```python
async def cleanup_expired_sessions(self):
    """Remove expired sessions and disconnect their clients."""
    with self.lock:
        expired = [sid for sid, s in self.sessions.items() if s.is_expired()]

    for sid in expired:
        session = self.sessions.get(sid)
        if session and session.client:
            try:
                await session.client.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting client for session {sid}: {e}")

    with self.lock:
        for sid in expired:
            self.sessions.pop(sid, None)
            logger.info(f"Cleaned up expired session: {sid}")

async def async_shutdown(self):
    """Async shutdown: disconnect all clients and clear sessions."""
    if self._cleanup_task:
        self._cleanup_task.cancel()

    for session in list(self.sessions.values()):
        if session.client:
            try:
                await session.client.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting client for session {session.session_id}: {e}")

    with self.lock:
        self.sessions.clear()
        logger.info("Session manager async shutdown complete")
```

Also update `start_cleanup_task` to call the async version:

```python
def start_cleanup_task(self):
    if self._cleanup_task is not None:
        return

    async def cleanup_loop():
        try:
            while True:
                await asyncio.sleep(self.cleanup_interval_minutes * 60)
                await self.cleanup_expired_sessions()
        except asyncio.CancelledError:
            logger.info("Session cleanup task cancelled")
            raise

    try:
        loop = asyncio.get_running_loop()
        self._cleanup_task = loop.create_task(cleanup_loop())
        logger.info(f"Started session cleanup task (interval: {self.cleanup_interval_minutes} minutes)")
    except RuntimeError:
        logger.warning("No running event loop, automatic session cleanup disabled")
```

Keep sync `_cleanup_expired_sessions` and `shutdown` for backward compatibility (existing callers).

**Step 4: Run tests to verify they pass**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m pytest tests/test_session_manager_unit.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/session_manager.py tests/test_session_manager_unit.py
git commit -m "feat: add async cleanup and shutdown with client disconnect"
```

---

### Task 3: Extract `_convert_message` helper in claude_cli.py

**Files:**
- Modify: `src/claude_cli.py:158-175`
- Test: `tests/test_claude_cli_unit.py`

**Step 1: Write the failing test**

Add to `tests/test_claude_cli_unit.py`:

```python
class TestClaudeCodeCLIConvertMessage:
    """Test ClaudeCodeCLI._convert_message() helper."""

    @pytest.fixture
    def cli_class(self):
        from src.claude_cli import ClaudeCodeCLI
        return ClaudeCodeCLI

    def test_convert_dict_message_passthrough(self, cli_class):
        """Dict messages pass through unchanged."""
        cli = MagicMock()
        cli._convert_message = cli_class._convert_message.__get__(cli, cli_class)

        msg = {"type": "assistant", "content": "hello"}
        result = cli._convert_message(msg)
        assert result == msg

    def test_convert_object_message_to_dict(self, cli_class):
        """Object messages are converted to dicts."""
        cli = MagicMock()
        cli._convert_message = cli_class._convert_message.__get__(cli, cli_class)

        obj = MagicMock()
        obj.type = "assistant"
        obj.content = "hello"
        # Make it not a dict
        result = cli._convert_message(obj)
        assert isinstance(result, dict)
        assert result["type"] == "assistant"
        assert result["content"] == "hello"
```

**Step 2: Run test to verify it fails**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m pytest tests/test_claude_cli_unit.py::TestClaudeCodeCLIConvertMessage -v`
Expected: FAIL — `_convert_message` doesn't exist

**Step 3: Write minimal implementation**

In `src/claude_cli.py`, add method to `ClaudeCodeCLI` and refactor `run_completion` to use it:

```python
def _convert_message(self, message) -> Dict[str, Any]:
    """Convert SDK message object to dict if needed."""
    if isinstance(message, dict):
        return message
    if hasattr(message, "__dict__"):
        return {
            k: v for k, v in vars(message).items()
            if not k.startswith("_") and not callable(v)
        }
    return message
```

Then in `run_completion`, replace the inline conversion with:

```python
async for message in query(prompt=prompt, options=options):
    logger.debug(f"Raw SDK message type: {type(message)}")
    logger.debug(f"Raw SDK message: {message}")
    converted = self._convert_message(message)
    logger.debug(f"Converted message dict: {converted}")
    yield converted
```

**Step 4: Run all claude_cli tests**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m pytest tests/test_claude_cli_unit.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/claude_cli.py tests/test_claude_cli_unit.py
git commit -m "refactor: extract _convert_message helper in ClaudeCodeCLI"
```

---

### Task 4: Add `create_client()` and `run_completion_with_client()`

**Files:**
- Modify: `src/claude_cli.py`
- Test: `tests/test_claude_cli_unit.py`

**Step 1: Write the failing tests**

Add to `tests/test_claude_cli_unit.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

class TestClaudeCodeCLICreateClient:
    """Test ClaudeCodeCLI.create_client()."""

    @pytest.fixture
    def cli_instance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("src.auth.validate_claude_code_auth") as mock_validate:
                with patch("src.auth.auth_manager") as mock_auth:
                    mock_validate.return_value = (True, {"method": "anthropic"})
                    mock_auth.get_claude_code_env_vars.return_value = {}
                    from src.claude_cli import ClaudeCodeCLI
                    cli = ClaudeCodeCLI(cwd=temp_dir)
                    yield cli

    @pytest.mark.asyncio
    async def test_create_client_returns_connected_client(self, cli_instance):
        """create_client returns a connected ClaudeSDKClient."""
        mock_client = AsyncMock()
        with patch("src.claude_cli.ClaudeSDKClient", return_value=mock_client):
            client = await cli_instance.create_client(model="claude-sonnet-4-5-20250929")
            assert client is mock_client
            mock_client.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_client_passes_options(self, cli_instance):
        """create_client passes correct options to ClaudeSDKClient."""
        mock_client = AsyncMock()
        captured_options = []

        def capture_init(options=None, **kwargs):
            captured_options.append(options)
            return mock_client

        with patch("src.claude_cli.ClaudeSDKClient", side_effect=capture_init):
            await cli_instance.create_client(
                model="claude-sonnet-4-5-20250929",
                system_prompt="Be helpful",
                permission_mode="acceptEdits",
            )

        assert len(captured_options) == 1
        opts = captured_options[0]
        assert opts.model == "claude-sonnet-4-5-20250929"
        assert opts.permission_mode == "acceptEdits"


class TestClaudeCodeCLIRunCompletionWithClient:
    """Test ClaudeCodeCLI.run_completion_with_client()."""

    @pytest.fixture
    def cli_instance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("src.auth.validate_claude_code_auth") as mock_validate:
                with patch("src.auth.auth_manager") as mock_auth:
                    mock_validate.return_value = (True, {"method": "anthropic"})
                    mock_auth.get_claude_code_env_vars.return_value = {}
                    from src.claude_cli import ClaudeCodeCLI
                    cli = ClaudeCodeCLI(cwd=temp_dir)
                    yield cli

    @pytest.mark.asyncio
    async def test_run_with_client_calls_query_and_receive(self, cli_instance):
        """Calls client.query() then iterates client.receive_response()."""
        mock_msg = MagicMock()
        mock_msg.type = "assistant"
        mock_msg.content = [MagicMock(text="Hello")]

        mock_client = AsyncMock()

        async def mock_receive():
            yield mock_msg

        mock_client.receive_response.return_value = mock_receive()

        messages = []
        async for msg in cli_instance.run_completion_with_client(
            prompt="Hi", client=mock_client
        ):
            messages.append(msg)

        mock_client.query.assert_called_once_with("Hi")
        assert len(messages) == 1
        assert isinstance(messages[0], dict)

    @pytest.mark.asyncio
    async def test_run_with_client_yields_error_on_exception(self, cli_instance):
        """Yields error dict when client.query() raises."""
        mock_client = AsyncMock()
        mock_client.query.side_effect = RuntimeError("connection lost")

        messages = []
        async for msg in cli_instance.run_completion_with_client(
            prompt="Hi", client=mock_client
        ):
            messages.append(msg)

        assert len(messages) == 1
        assert messages[0]["is_error"] is True
        assert "connection lost" in messages[0]["error_message"]
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m pytest tests/test_claude_cli_unit.py::TestClaudeCodeCLICreateClient tests/test_claude_cli_unit.py::TestClaudeCodeCLIRunCompletionWithClient -v`
Expected: FAIL — methods don't exist

**Step 3: Write minimal implementation**

In `src/claude_cli.py`, add import and methods:

```python
from claude_agent_sdk import query, ClaudeAgentOptions, ClaudeSDKClient

# ... inside ClaudeCodeCLI class ...

async def create_client(
    self,
    model: Optional[str] = None,
    system_prompt: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    disallowed_tools: Optional[List[str]] = None,
    permission_mode: Optional[str] = None,
    max_turns: int = 10,
    output_format: Optional[Dict[str, Any]] = None,
) -> "ClaudeSDKClient":
    """Create and connect a new ClaudeSDKClient."""
    options = ClaudeAgentOptions(max_turns=max_turns, cwd=self.cwd)

    if model:
        options.model = model
    if system_prompt:
        options.system_prompt = {"type": "text", "text": system_prompt}
    else:
        options.system_prompt = {"type": "preset", "preset": "claude_code"}
    if allowed_tools:
        options.allowed_tools = allowed_tools
    if disallowed_tools:
        options.disallowed_tools = disallowed_tools
    if permission_mode:
        options.permission_mode = permission_mode
    if output_format:
        options.output_format = output_format

    client = ClaudeSDKClient(options=options)
    await client.connect()
    return client

async def run_completion_with_client(
    self,
    prompt: str,
    client: "ClaudeSDKClient",
) -> AsyncGenerator[Dict[str, Any], None]:
    """Run completion using an existing ClaudeSDKClient (session mode)."""
    try:
        await client.query(prompt)
        async for message in client.receive_response():
            yield self._convert_message(message)
    except Exception as e:
        logger.error(f"ClaudeSDKClient error: {e}")
        yield {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "error_message": str(e),
        }
```

**Step 4: Run all claude_cli tests**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m pytest tests/test_claude_cli_unit.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/claude_cli.py tests/test_claude_cli_unit.py
git commit -m "feat: add create_client and run_completion_with_client methods"
```

---

### Task 5: Add session routing in main.py streaming path

**Files:**
- Modify: `src/main.py:411-612` (`generate_streaming_response`)
- Test: `tests/test_session_complete.py`

**Step 1: Write the failing test**

Add to `tests/test_session_complete.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestSessionRouting:
    """Test that session_id routes to ClaudeSDKClient path."""

    @pytest.mark.asyncio
    async def test_streaming_with_session_id_uses_client(self):
        """Streaming request with session_id uses run_completion_with_client."""
        from src.session_manager import session_manager, Session

        # Setup: create session with mock client
        session = session_manager.get_or_create_session("test-routing")
        mock_client = AsyncMock()

        async def mock_receive():
            msg = MagicMock()
            msg.type = "assistant"
            msg.content = [MagicMock(text="response")]
            yield msg
            result = MagicMock()
            result.type = "result"
            result.subtype = "success"
            result.result = "response"
            result.stop_reason = "end_turn"
            yield result

        mock_client.receive_response.return_value = mock_receive()
        session.client = mock_client

        with patch("src.main.claude_cli") as mock_cli:
            mock_cli.run_completion_with_client = AsyncMock()

            async def mock_run(prompt, client):
                async for m in mock_receive():
                    from src.claude_cli import ClaudeCodeCLI
                    converted = {
                        k: v for k, v in vars(m).items()
                        if not k.startswith("_") and not callable(v)
                    }
                    yield converted

            mock_cli.run_completion_with_client = mock_run
            mock_cli._convert_message = lambda self, m: m

            # Verify the routing happens (session.client is not None)
            assert session.client is not None

        # Cleanup
        session_manager.delete_session("test-routing")
```

**Step 2: Run test to verify baseline**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m pytest tests/test_session_complete.py::TestSessionRouting -v`

**Step 3: Write implementation**

In `src/main.py`, modify `generate_streaming_response()`. After the session processing block (~line 417), add the client path branch:

```python
async def generate_streaming_response(
    request: ChatCompletionRequest, request_id: str, claude_headers: Optional[Dict[str, Any]] = None
) -> AsyncGenerator[str, None]:
    try:
        if request.session_id:
            # Session mode: use ClaudeSDKClient for native context continuity
            session = session_manager.get_or_create_session(request.session_id)

            # Store new messages for API compatibility
            session.add_messages(request.messages)

            # Build prompt from latest user message only (SDK maintains context)
            last_user_msg = None
            for msg in reversed(request.messages):
                if msg.role == "user":
                    last_user_msg = msg.content
                    break
            prompt = last_user_msg or MessageAdapter.messages_to_prompt(request.messages)[0]

            system_prompt = None
            for msg in request.messages:
                if msg.role == "system":
                    system_prompt = msg.content

            # Get options
            claude_options = request.to_claude_options()
            if claude_headers:
                claude_options.update(claude_headers)

            # Handle tools
            if not request.enable_tools:
                claude_options["disallowed_tools"] = CLAUDE_TOOLS
                claude_options["max_turns"] = 1
            else:
                claude_options["allowed_tools"] = DEFAULT_ALLOWED_TOOLS
                claude_options["permission_mode"] = "bypassPermissions"

            # Create client if first request in session
            async with session.lock:
                if not session.client:
                    session.client = await claude_cli.create_client(
                        model=claude_options.get("model"),
                        system_prompt=system_prompt,
                        allowed_tools=claude_options.get("allowed_tools"),
                        disallowed_tools=claude_options.get("disallowed_tools"),
                        permission_mode=claude_options.get("permission_mode"),
                        output_format=claude_options.get("output_format"),
                        max_turns=claude_options.get("max_turns", DEFAULT_MAX_TURNS),
                    )

            # Stream using client
            chunks_buffer = []
            role_sent = False
            content_sent = False

            async for chunk in claude_cli.run_completion_with_client(
                prompt=prompt, client=session.client
            ):
                chunks_buffer.append(chunk)
                # ... (same streaming SSE logic as existing code) ...
                # This reuses the exact same chunk→SSE conversion below

            # After streaming, save assistant response
            assistant_content = claude_cli.parse_claude_message(chunks_buffer)
            if assistant_content:
                filtered = MessageAdapter.filter_content(assistant_content)
                assistant_message = Message(role="assistant", content=filtered)
                session_manager.add_assistant_response(request.session_id, assistant_message)

            # Final chunk with stop reason
            sdk_stop_reason = extract_stop_reason(chunks_buffer)
            finish_reason = map_stop_reason(sdk_stop_reason)
            # ... emit final SSE chunk ...
            return

        # Stateless mode: existing code path (unchanged)
        all_messages, actual_session_id = session_manager.process_messages(
            request.messages, request.session_id
        )
        # ... rest of existing code unchanged ...
```

The SSE chunk conversion logic (lines ~471-612) stays the same for both paths — extract it to share. The key change is the `if request.session_id:` branch at the top that uses `run_completion_with_client`.

Apply the same pattern to the non-streaming `chat_completion()` handler (~line 670-766).

**Step 4: Run full test suite**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m pytest tests/ -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/main.py tests/test_session_complete.py
git commit -m "feat: route session requests through ClaudeSDKClient"
```

---

### Task 6: Update main.py shutdown to use async_shutdown

**Files:**
- Modify: `src/main.py` (lifespan context manager)

**Step 1: Find the lifespan handler**

Check `src/main.py` for the `@asynccontextmanager` lifespan or `on_event("shutdown")` handler.

**Step 2: Update shutdown call**

Replace `session_manager.shutdown()` with `await session_manager.async_shutdown()` in the lifespan shutdown path.

**Step 3: Run full test suite**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m pytest tests/ -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add src/main.py
git commit -m "feat: use async_shutdown for client cleanup on server stop"
```

---

### Task 7: Final verification

**Step 1: Run full test suite**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m pytest tests/ -v --tb=short`
Expected: All PASS

**Step 2: Run type checker**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m mypy src/ --ignore-missing-imports`
Expected: No new errors

**Step 3: Run linter**

Run: `cd /home/jinyoung/claude-code-openai-wrapper && python -m black --check src/ tests/`
Expected: All files OK (or format them)

**Step 4: Final commit if formatting changes**

```bash
git add -A
git commit -m "style: format code with black"
```
