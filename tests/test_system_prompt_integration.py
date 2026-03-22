"""Integration tests for custom system prompt feature.

Covers: admin endpoints, startup fail-fast, session freeze semantics,
and Claude option composition with _custom_base.
"""

import pytest
from unittest.mock import patch
from typing import Any, Dict, List

import httpx

from src import main
from src import system_prompt as sp
from src.backends import BackendRegistry
from src.rate_limiter import limiter as _global_limiter


# ---------------------------------------------------------------------------
# Fake backends
# ---------------------------------------------------------------------------


class RecordingClaudeBackend:
    """Claude backend that records run_completion kwargs for assertion."""

    name = "claude"
    owned_by = "anthropic"

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    async def run_completion(self, *, prompt: str, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        yield {"type": "assistant", "content": [{"type": "text", "text": "ok"}]}
        yield {
            "type": "result",
            "subtype": "success",
            "result": "ok",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }

    def build_options(self, request, resolved, overrides=None):
        options = request.to_claude_options() if hasattr(request, "to_claude_options") else {}
        if overrides:
            options.update(overrides)
        if resolved.provider_model:
            options["model"] = resolved.provider_model
        options["permission_mode"] = "bypassPermissions"
        return options

    def parse_message(self, messages):
        return "ok"

    def estimate_token_usage(self, prompt, completion, model=None):
        return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    async def verify(self):
        return True

    def supported_models(self):
        return ["sonnet", "opus", "haiku"]

    def resolve(self, model):
        from src.backends import ResolvedModel

        return ResolvedModel(backend="claude", provider_model=model)

    def get_auth_provider(self):
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_system_prompt(tmp_path):
    """Reset module-level state and isolate persistence before/after each test."""
    sp._default_prompt = None
    sp._runtime_prompt = None
    orig_data_dir = sp._DATA_DIR
    orig_persist = sp._PERSIST_FILE
    sp._DATA_DIR = tmp_path
    sp._PERSIST_FILE = tmp_path / "system_prompt.json"
    if _global_limiter is not None:
        try:
            _global_limiter.reset()
        except Exception:
            pass
    yield
    sp._default_prompt = None
    sp._runtime_prompt = None
    sp._DATA_DIR = orig_data_dir
    sp._PERSIST_FILE = orig_persist


@pytest.fixture()
def recording_backend():
    backend = RecordingClaudeBackend()
    BackendRegistry.register("claude", backend)
    yield backend


# ---------------------------------------------------------------------------
# Admin endpoint tests
# ---------------------------------------------------------------------------


class TestAdminSystemPromptEndpoints:
    """Test GET/PUT/DELETE /admin/api/system-prompt."""

    @pytest.fixture(autouse=True)
    def _patch_admin(self):
        with patch("src.admin_auth.ADMIN_API_KEY", "test-key"):
            yield

    async def _admin_client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://test",
            cookies={"admin_session": ""},  # will be set after login
        )

    async def _login(self, client):
        resp = await client.post("/admin/api/login", json={"api_key": "test-key"})
        assert resp.status_code == 200
        return resp

    async def test_get_returns_preset_mode_by_default(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://test",
        ) as client:
            await self._login(client)
            resp = await client.get("/admin/api/system-prompt")
            assert resp.status_code == 200
            data = resp.json()
            assert data["mode"] == "preset"
            assert data["prompt"] is None
            assert data["char_count"] == 0

    async def test_put_sets_custom_prompt(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://test",
        ) as client:
            await self._login(client)
            resp = await client.put(
                "/admin/api/system-prompt",
                json={"prompt": "You are a helpful bot."},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "updated"
            assert data["mode"] == "custom"
            assert data["char_count"] == len("You are a helpful bot.")

            # Verify GET reflects the change
            resp2 = await client.get("/admin/api/system-prompt")
            assert resp2.json()["prompt"] == "You are a helpful bot."

    async def test_put_empty_returns_422(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://test",
        ) as client:
            await self._login(client)
            resp = await client.put(
                "/admin/api/system-prompt",
                json={"prompt": "   "},
            )
            assert resp.status_code == 422

    async def test_delete_resets_to_default(self):
        sp.set_system_prompt("custom override")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://test",
        ) as client:
            await self._login(client)
            resp = await client.delete("/admin/api/system-prompt")
            assert resp.status_code == 200
            assert resp.json()["mode"] == "preset"
            assert sp.get_system_prompt() is None

    async def test_endpoints_require_admin_auth(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://test",
        ) as client:
            # No login -> should fail
            for method in ["get", "put", "delete"]:
                func = getattr(client, method)
                kwargs = {}
                if method == "put":
                    kwargs["json"] = {"prompt": "test"}
                resp = await func("/admin/api/system-prompt", **kwargs)
                assert resp.status_code in (401, 403), f"{method} should require auth"


# ---------------------------------------------------------------------------
# Startup fail-fast test
# ---------------------------------------------------------------------------


class TestStartupLoading:
    def test_load_default_prompt_fails_on_missing_file(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            sp.load_default_prompt("/nonexistent/path/prompt.txt")

    def test_load_default_prompt_succeeds_with_valid_file(self, tmp_path):
        f = tmp_path / "prompt.txt"
        f.write_text("Custom system prompt content.", encoding="utf-8")
        sp.load_default_prompt(str(f))
        assert sp.get_system_prompt() == "Custom system prompt content."
        assert sp.get_prompt_mode() == "file"


# ---------------------------------------------------------------------------
# Session freeze tests
# ---------------------------------------------------------------------------


class TestSessionFreeze:
    """Verify that system prompt changes mid-session don't affect existing sessions."""

    async def test_session_freezes_prompt_on_first_turn(self, recording_backend):
        """First turn snapshots base_system_prompt; subsequent admin change doesn't affect it."""
        sp.set_system_prompt("Original prompt")

        with (
            patch.object(main, "runtime_api_key", None),
            patch("src.routes.chat.verify_api_key", return_value=True),
            patch("src.routes.chat._validate_backend_auth"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://test",
            ) as client:
                # Turn 1: creates session with "Original prompt"
                resp1 = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "sonnet",
                        "messages": [{"role": "user", "content": "hello"}],
                        "session_id": "freeze-test-session",
                        "stream": False,
                    },
                )
                assert resp1.status_code == 200

                # Admin changes prompt between turns
                sp.set_system_prompt("Updated prompt")

                # Turn 2: should still use frozen "Original prompt"
                resp2 = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "sonnet",
                        "messages": [{"role": "user", "content": "world"}],
                        "session_id": "freeze-test-session",
                        "stream": False,
                    },
                )
                assert resp2.status_code == 200

        # Check that both calls used "Original prompt" as _custom_base
        assert len(recording_backend.calls) == 2
        assert recording_backend.calls[0]["_custom_base"] == "Original prompt"
        assert recording_backend.calls[1]["_custom_base"] == "Original prompt"

    async def test_new_session_picks_up_updated_prompt(self, recording_backend):
        """A new session after admin change uses the updated prompt."""
        sp.set_system_prompt("Prompt v1")

        with (
            patch.object(main, "runtime_api_key", None),
            patch("src.routes.chat.verify_api_key", return_value=True),
            patch("src.routes.chat._validate_backend_auth"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://test",
            ) as client:
                # Session 1 with v1
                await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "sonnet",
                        "messages": [{"role": "user", "content": "hello"}],
                        "session_id": "session-v1",
                        "stream": False,
                    },
                )

                # Admin updates prompt
                sp.set_system_prompt("Prompt v2")

                # Session 2 with v2
                await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "sonnet",
                        "messages": [{"role": "user", "content": "hello"}],
                        "session_id": "session-v2",
                        "stream": False,
                    },
                )

        assert recording_backend.calls[0]["_custom_base"] == "Prompt v1"
        assert recording_backend.calls[1]["_custom_base"] == "Prompt v2"

    async def test_preset_mode_session_stays_preset(self, recording_backend):
        """A session created in preset mode stays in preset mode even if admin sets a prompt."""
        # No custom prompt set (preset mode)

        with (
            patch.object(main, "runtime_api_key", None),
            patch("src.routes.chat.verify_api_key", return_value=True),
            patch("src.routes.chat._validate_backend_auth"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://test",
            ) as client:
                # Turn 1 in preset mode
                await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "sonnet",
                        "messages": [{"role": "user", "content": "hello"}],
                        "session_id": "preset-session",
                        "stream": False,
                    },
                )

                # Admin sets custom prompt
                sp.set_system_prompt("New custom prompt")

                # Turn 2: should still have None base (preset)
                await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "sonnet",
                        "messages": [{"role": "user", "content": "world"}],
                        "session_id": "preset-session",
                        "stream": False,
                    },
                )

        assert recording_backend.calls[0]["_custom_base"] is None
        assert recording_backend.calls[1]["_custom_base"] is None


# ---------------------------------------------------------------------------
# Claude option composition tests
# ---------------------------------------------------------------------------


class TestClaudeOptionComposition:
    """Test _build_sdk_options composes system_prompt correctly."""

    def test_preset_mode_no_system_prompt(self):
        """No custom base and no per-request prompt -> preset dict."""
        from src.backends.claude.client import ClaudeCodeCLI

        cli = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        cli.cwd = "/tmp"
        with (
            patch.object(cli, "_configure_thinking"),
            patch.object(cli, "_configure_sandbox"),
            patch.object(cli, "_configure_tools"),
            patch.object(cli, "_configure_session"),
            patch("src.runtime_config.get_token_streaming", return_value=False),
        ):
            options = cli._build_sdk_options(_custom_base=None)
            assert options.system_prompt == {"type": "preset", "preset": "claude_code"}

    def test_preset_mode_with_per_request_prompt(self):
        """No custom base but per-request prompt -> preset + append."""
        from src.backends.claude.client import ClaudeCodeCLI

        cli = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        cli.cwd = "/tmp"
        with (
            patch.object(cli, "_configure_thinking"),
            patch.object(cli, "_configure_sandbox"),
            patch.object(cli, "_configure_tools"),
            patch.object(cli, "_configure_session"),
            patch("src.runtime_config.get_token_streaming", return_value=False),
        ):
            options = cli._build_sdk_options(
                system_prompt="extra instructions",
                _custom_base=None,
            )
            assert options.system_prompt == {
                "type": "preset",
                "preset": "claude_code",
                "append": "extra instructions",
            }

    def test_custom_base_only(self):
        """Custom base, no per-request prompt -> base as plain string."""
        from src.backends.claude.client import ClaudeCodeCLI

        cli = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        cli.cwd = "/tmp"
        with (
            patch.object(cli, "_configure_thinking"),
            patch.object(cli, "_configure_sandbox"),
            patch.object(cli, "_configure_tools"),
            patch.object(cli, "_configure_session"),
            patch("src.runtime_config.get_token_streaming", return_value=False),
        ):
            options = cli._build_sdk_options(_custom_base="My custom system prompt")
            assert options.system_prompt == "My custom system prompt"

    def test_custom_base_with_per_request_prompt(self):
        """Custom base + per-request prompt -> composed string."""
        from src.backends.claude.client import ClaudeCodeCLI

        cli = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        cli.cwd = "/tmp"
        with (
            patch.object(cli, "_configure_thinking"),
            patch.object(cli, "_configure_sandbox"),
            patch.object(cli, "_configure_tools"),
            patch.object(cli, "_configure_session"),
            patch("src.runtime_config.get_token_streaming", return_value=False),
        ):
            options = cli._build_sdk_options(
                system_prompt="per-request instructions",
                _custom_base="My custom base",
            )
            assert options.system_prompt == "My custom base\n\nper-request instructions"

    def test_unset_sentinel_reads_global_state(self):
        """When _custom_base is not provided, reads from global state."""
        from src.backends.claude.client import ClaudeCodeCLI

        cli = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        cli.cwd = "/tmp"
        sp.set_system_prompt("Global custom prompt")
        with (
            patch.object(cli, "_configure_thinking"),
            patch.object(cli, "_configure_sandbox"),
            patch.object(cli, "_configure_tools"),
            patch.object(cli, "_configure_session"),
            patch("src.runtime_config.get_token_streaming", return_value=False),
        ):
            # Don't pass _custom_base -> uses sentinel -> reads global state
            options = cli._build_sdk_options()
            assert options.system_prompt == "Global custom prompt"
