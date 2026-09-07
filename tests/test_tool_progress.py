"""Tool-aware silence handling: ``response.tool_progress`` + the tool stall budget.

Why these exist: a slow MCP tool call is *silence* to the streaming path. Before
this, the only thing distinguishing it from a wedged CLI was nothing — the
keepalive comment filled the gap, the stall guard killed the turn at
STREAM_STALL_TIMEOUT with "no SDK output", and the client (ChatDRAGON) showed
"stream broke". Meanwhile the CLI *was* emitting ``tool_progress`` frames that
the pinned SDK dropped as an unknown message type. These tests pin the three
pieces that close that gap: the in-flight registry, the tool-aware stall budget
and heartbeat, and the SDK client subclass that surfaces the CLI frames.
"""

import asyncio
import json
import logging

import pytest

from src import config_check, constants, streaming_utils
from src.backends.claude.sdk_client import (
    GatewayClaudeSDKClient,
    ToolProgressMessage,
    parse_tool_progress,
)
from src.streaming_utils import StreamStallError, _SSE_KEEPALIVE, _keepalive_wrapper
from src.tool_stats import ToolStatsCollector


def _parse(line: str) -> tuple[str, dict]:
    event_line, data_line = line.strip().splitlines()
    return event_line[len("event: ") :], json.loads(data_line[len("data: ") :])


async def _silent_source():
    while True:
        await asyncio.sleep(3600)
    yield  # pragma: no cover


def _tool_use_stream_events(
    tool_use_id="tu_1", name="mcp__confluence__search", parent=None
):
    """The stream_event chunks that make the loop register one in-flight tool."""
    base = {"parent_tool_use_id": parent} if parent else {}
    return [
        {
            **base,
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": tool_use_id, "name": name},
            },
        },
        {
            **base,
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"q":"x"}'},
            },
        },
        {
            **base,
            "type": "stream_event",
            "event": {"type": "content_block_stop", "index": 0},
        },
    ]


async def _collect(source, monkeypatch=None):
    events = []
    stream_result: dict = {}
    async for line in streaming_utils.stream_response_chunks(
        source,
        model="m",
        response_id="resp_tp",
        output_item_id="msg_tp",
        chunks_buffer=[],
        logger=logging.getLogger("test-tool-progress"),
        stream_result=stream_result,
    ):
        if line is _SSE_KEEPALIVE or line.startswith(":"):
            events.append(("keepalive", {}))
            continue
        events.append(_parse(line))
    return events, stream_result


# ---------------------------------------------------------------------------
# In-flight registry
# ---------------------------------------------------------------------------


class TestInFlight:
    def test_use_without_result_is_in_flight_and_result_clears_it(self):
        stats = ToolStatsCollector()
        stats.record_use("tu_1", "Bash")
        stats.record_use("tu_2", "mcp__x__y", parent_tool_use_id="task_1")
        running = stats.in_flight()
        assert [t["tool_use_id"] for t in running] == ["tu_1", "tu_2"]  # oldest first
        assert running[1]["parent_tool_use_id"] == "task_1"
        assert running[0]["parent_tool_use_id"] is None
        assert all(isinstance(t["elapsed_seconds"], int) for t in running)
        stats.record_result("tu_1", False)
        assert [t["tool_use_id"] for t in stats.in_flight()] == ["tu_2"]

    def test_idless_use_is_counted_but_not_in_flight(self):
        stats = ToolStatsCollector()
        stats.record_use(None, "Bash")
        assert stats.in_flight() == []
        assert stats.snapshot()["Bash"]["count"] == 1


# ---------------------------------------------------------------------------
# Keepalive wrapper: tool-aware budget
# ---------------------------------------------------------------------------


class TestToolAwareStallBudget:
    async def test_in_flight_tool_extends_the_budget(self):
        """Silence past STREAM_STALL but under TOOL_STALL must keep going
        while a tool is outstanding — that silence is the tool running."""
        stats = ToolStatsCollector()
        stats.record_use("tu_1", "mcp__slow__call")
        agen = _keepalive_wrapper(
            _silent_source(),
            0.01,
            stall_after=0.05,
            in_flight_stall_after=0.5,
            in_flight=stats.in_flight,
        )
        started = asyncio.get_event_loop().time()
        seen = 0
        async for item in agen:
            assert item is _SSE_KEEPALIVE
            seen += 1
            if asyncio.get_event_loop().time() - started > 0.15:
                break
        await agen.aclose()
        assert seen >= 3, "keepalives must continue past the generic stall budget"

    async def test_in_flight_tool_past_tool_budget_names_the_tool(self):
        stats = ToolStatsCollector()
        stats.record_use("tu_9", "mcp__slow__call")
        agen = _keepalive_wrapper(
            _silent_source(),
            0.01,
            stall_after=0.02,
            in_flight_stall_after=0.06,
            in_flight=stats.in_flight,
        )
        with pytest.raises(StreamStallError) as excinfo:
            async for _ in agen:
                pass
        message = str(excinfo.value)
        assert "mcp__slow__call" in message and "tu_9" in message
        assert "TOOL_STALL_TIMEOUT" in message

    async def test_no_tool_in_flight_keeps_the_generic_budget(self):
        stats = ToolStatsCollector()  # empty → generic budget applies
        agen = _keepalive_wrapper(
            _silent_source(),
            0.01,
            stall_after=0.04,
            in_flight_stall_after=10,
            in_flight=stats.in_flight,
        )
        with pytest.raises(StreamStallError) as excinfo:
            async for _ in agen:
                pass
        assert "no SDK output" in str(excinfo.value)

    async def test_zero_tool_budget_falls_back_to_generic(self):
        stats = ToolStatsCollector()
        stats.record_use("tu_1", "Bash")
        agen = _keepalive_wrapper(
            _silent_source(),
            0.01,
            stall_after=0.04,
            in_flight_stall_after=0,
            in_flight=stats.in_flight,
        )
        with pytest.raises(StreamStallError) as excinfo:
            async for _ in agen:
                pass
        # Generic budget, but the message still says which tool was running.
        assert "Bash" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Stream loop: gateway heartbeat + stall message
# ---------------------------------------------------------------------------


class TestGatewayHeartbeat:
    async def test_silent_tool_call_emits_tool_progress_then_named_stall(
        self, monkeypatch
    ):
        monkeypatch.setattr(streaming_utils, "SSE_KEEPALIVE_INTERVAL", 0.02)
        monkeypatch.setattr(streaming_utils, "STREAM_STALL_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(streaming_utils, "TOOL_STALL_TIMEOUT_SECONDS", 0.2)
        monkeypatch.setattr(streaming_utils, "STREAM_TOOL_PROGRESS", True)

        async def source():
            for chunk in _tool_use_stream_events():
                yield chunk
            await asyncio.sleep(3600)

        events, stream_result = await _collect(source())
        types = [t for t, _ in events]
        assert "response.tool_use" in types
        progress = [d for t, d in events if t == "response.tool_progress"]
        assert progress, f"no heartbeat while the tool ran: {types}"
        first = progress[0]
        assert first["tool_use_id"] == "tu_1"
        assert first["name"] == "mcp__confluence__search"
        assert first["source"] == "gateway"
        assert isinstance(first["elapsed_seconds"], int)
        assert "parent_tool_use_id" not in first
        # Heartbeats ride the keepalive tick: each is preceded by a comment.
        first_idx = types.index("response.tool_progress")
        assert types[first_idx - 1] == "keepalive"
        # Past the tool budget the turn fails, naming the tool — not "stream broke".
        assert stream_result.get("success") is False
        failed = [d for t, d in events if t == "response.failed"]
        assert failed, types
        message = failed[-1]["response"]["error"]["message"]
        assert message.startswith("Turn stalled:")
        assert "mcp__confluence__search" in message and "tu_1" in message

    async def test_heartbeat_stops_once_the_tool_answers(self, monkeypatch):
        monkeypatch.setattr(streaming_utils, "SSE_KEEPALIVE_INTERVAL", 0.02)
        monkeypatch.setattr(streaming_utils, "STREAM_STALL_TIMEOUT_SECONDS", 0.3)
        monkeypatch.setattr(streaming_utils, "TOOL_STALL_TIMEOUT_SECONDS", 0.3)

        async def source():
            for chunk in _tool_use_stream_events():
                yield chunk
            await asyncio.sleep(0.07)
            yield {
                "type": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}
                ],
            }
            await asyncio.sleep(0.07)  # silence with NO tool in flight
            yield {"subtype": "success", "result": "done"}

        events, stream_result = await _collect(source())
        types = [t for t, _ in events]
        result_idx = types.index("response.tool_result")
        assert "response.tool_progress" in types[:result_idx]
        assert "response.tool_progress" not in types[result_idx:]
        assert "keepalive" in types[result_idx:], "plain keepalive must continue"
        assert stream_result.get("success") is not False

    async def test_heartbeat_respects_stream_tool_progress_flag(self, monkeypatch):
        monkeypatch.setattr(streaming_utils, "SSE_KEEPALIVE_INTERVAL", 0.02)
        monkeypatch.setattr(streaming_utils, "STREAM_STALL_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(streaming_utils, "TOOL_STALL_TIMEOUT_SECONDS", 0.1)
        monkeypatch.setattr(streaming_utils, "STREAM_TOOL_PROGRESS", False)

        async def source():
            for chunk in _tool_use_stream_events():
                yield chunk
            await asyncio.sleep(3600)

        events, _ = await _collect(source())
        types = [t for t, _ in events]
        assert "response.tool_progress" not in types
        assert "keepalive" in types

    async def test_subagent_tool_heartbeat_follows_progress_gate(self, monkeypatch):
        monkeypatch.setattr(streaming_utils, "SSE_KEEPALIVE_INTERVAL", 0.02)
        monkeypatch.setattr(streaming_utils, "STREAM_STALL_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(streaming_utils, "TOOL_STALL_TIMEOUT_SECONDS", 0.1)
        monkeypatch.setattr(streaming_utils, "SUBAGENT_STREAM_TOOL_BLOCKS", True)
        monkeypatch.setattr(streaming_utils, "SUBAGENT_STREAM_PROGRESS", False)

        async def source():
            for chunk in _tool_use_stream_events(parent="task_1"):
                yield chunk
            await asyncio.sleep(3600)

        events, _ = await _collect(source())
        assert "response.tool_progress" not in [t for t, _ in events]

        monkeypatch.setattr(streaming_utils, "SUBAGENT_STREAM_PROGRESS", True)
        events, _ = await _collect(source())
        progress = [d for t, d in events if t == "response.tool_progress"]
        assert progress and progress[0]["parent_tool_use_id"] == "task_1"


# ---------------------------------------------------------------------------
# Stream loop: CLI tool_progress passthrough
# ---------------------------------------------------------------------------


class TestCliToolProgressPassthrough:
    async def test_cli_frame_is_forwarded_with_gateway_field_names(self, monkeypatch):
        monkeypatch.setattr(streaming_utils, "STREAM_TOOL_PROGRESS", True)

        async def source():
            for chunk in _tool_use_stream_events():
                yield chunk
            yield {
                "type": "tool_progress",
                "tool_use_id": "tu_1",
                "tool_name": "mcp__confluence__search",
                "parent_tool_use_id": None,
                "elapsed_time_seconds": 42.7,
                "message": "fetching page 3/10",
            }
            yield {
                "type": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}
                ],
            }
            yield {"subtype": "success", "result": "done"}

        events, stream_result = await _collect(source())
        progress = [d for t, d in events if t == "response.tool_progress"]
        assert len(progress) == 1
        ev = progress[0]
        assert ev["source"] == "cli"
        assert ev["tool_use_id"] == "tu_1"
        assert ev["name"] == "mcp__confluence__search"
        assert ev["elapsed_seconds"] == 42
        assert ev["message"] == "fetching page 3/10"
        assert "parent_tool_use_id" not in ev
        # It is progress, not content: the frame's text never reaches the answer.
        deltas = "".join(
            d["delta"] for t, d in events if t == "response.output_text.delta"
        )
        assert "fetching page" not in deltas
        assert stream_result.get("success") is not False

    async def test_cli_frame_without_tool_use_id_is_dropped(self):
        async def source():
            yield {
                "type": "tool_progress",
                "tool_name": "Bash",
                "elapsed_time_seconds": 3,
            }
            yield {"subtype": "success", "result": "done"}

        events, _ = await _collect(source())
        assert "response.tool_progress" not in [t for t, _ in events]

    async def test_cli_frame_from_subagent_follows_progress_gate(self, monkeypatch):
        monkeypatch.setattr(streaming_utils, "SUBAGENT_STREAM_PROGRESS", False)

        async def source():
            yield {
                "type": "tool_progress",
                "tool_use_id": "tu_c",
                "tool_name": "Bash",
                "parent_tool_use_id": "task_1",
                "elapsed_time_seconds": 3,
            }
            yield {"subtype": "success", "result": "done"}

        events, _ = await _collect(source())
        assert "response.tool_progress" not in [t for t, _ in events]


# ---------------------------------------------------------------------------
# SDK client subclass
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, frames):
        self._frames = frames

    async def receive_messages(self):
        for frame in self._frames:
            yield frame


class TestGatewayClaudeSDKClient:
    async def test_tool_progress_frames_surface_and_others_parse(self):
        from claude_agent_sdk.types import ResultMessage

        client = GatewayClaudeSDKClient.__new__(GatewayClaudeSDKClient)
        client._query = _FakeQuery(
            [
                {
                    "type": "tool_progress",
                    "tool_use_id": "tu_1",
                    "tool_name": "mcp__x__y",
                    "parent_tool_use_id": None,
                    "elapsed_time_seconds": 12,
                    "uuid": "u-1",
                    "session_id": "s-1",
                },
                {"type": "totally_unknown_frame"},  # SDK drops, so do we
                {
                    "type": "result",
                    "subtype": "success",
                    "duration_ms": 1,
                    "duration_api_ms": 1,
                    "is_error": False,
                    "num_turns": 1,
                    "session_id": "s-1",
                },
            ]
        )
        got = [m async for m in client.receive_messages()]
        assert len(got) == 2
        progress, result = got
        assert isinstance(progress, ToolProgressMessage)
        assert progress.tool_use_id == "tu_1"
        assert progress.tool_name == "mcp__x__y"
        assert progress.elapsed_time_seconds == 12.0
        assert progress.parent_tool_use_id is None
        assert progress.data == {"uuid": "u-1", "session_id": "s-1"}
        assert isinstance(result, ResultMessage)

    async def test_not_connected_raises_like_the_sdk(self):
        from claude_agent_sdk._errors import CLIConnectionError

        client = GatewayClaudeSDKClient.__new__(GatewayClaudeSDKClient)
        client._query = None
        with pytest.raises(CLIConnectionError):
            async for _ in client.receive_messages():
                pass

    def test_parse_tool_progress_requires_tool_use_id(self):
        assert (
            parse_tool_progress({"type": "tool_progress", "tool_name": "Bash"}) is None
        )
        msg = parse_tool_progress(
            {"type": "tool_progress", "tool_use_id": "tu", "elapsed_time_seconds": True}
        )
        assert msg is not None and msg.elapsed_time_seconds == 0.0

    def test_convert_message_types_it_as_tool_progress(self):
        from src.backends.claude.client import ClaudeCodeCLI as ClaudeClient

        converted = ClaudeClient._convert_message(
            ClaudeClient.__new__(ClaudeClient),
            ToolProgressMessage(
                tool_use_id="tu_1", tool_name="Bash", elapsed_time_seconds=3.0
            ),
        )
        assert converted["type"] == "tool_progress"
        assert converted["tool_use_id"] == "tu_1"
        assert converted["tool_name"] == "Bash"
        assert converted["elapsed_time_seconds"] == 3.0

    def test_idle_reader_ignores_tool_progress(self):
        """Between turns the outbox must not turn progress into a stored event."""
        from src.session_outbox import _message_to_event

        assert (
            _message_to_event(
                ToolProgressMessage(
                    tool_use_id="tu_1", tool_name="Bash", elapsed_time_seconds=1
                )
            )
            is None
        )


# ---------------------------------------------------------------------------
# Constants + config check: the budgets nest
# ---------------------------------------------------------------------------


class TestToolStallDefaults:
    def test_derived_from_mcp_tool_timeout(self, monkeypatch):
        monkeypatch.setenv("MCP_TOOL_TIMEOUT", "900000")
        assert (
            constants._tool_stall_timeout_default()
            == 900 + constants.TOOL_STALL_GRACE_SECONDS
        )

    def test_rounds_partial_seconds_up(self, monkeypatch):
        monkeypatch.setenv("MCP_TOOL_TIMEOUT", "1500")
        assert (
            constants._tool_stall_timeout_default()
            == 2 + constants.TOOL_STALL_GRACE_SECONDS
        )

    @pytest.mark.parametrize("raw", ["", "abc", "0", "-5"])
    def test_unset_or_invalid_falls_back_to_generic(self, monkeypatch, raw):
        monkeypatch.setenv("MCP_TOOL_TIMEOUT", raw)
        assert constants._tool_stall_timeout_default() == 0


class TestStallHierarchyCheck:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        for name in (
            "SSE_KEEPALIVE_INTERVAL",
            "STREAM_STALL_TIMEOUT",
            "TOOL_STALL_TIMEOUT",
            "ACTIVE_TURN_MAX_AGE",
            "MCP_TOOL_TIMEOUT",
        ):
            monkeypatch.delenv(name, raising=False)

    def test_defaults_are_consistent(self):
        assert config_check._check_stall_hierarchy() == []

    def test_mcp_tool_timeout_derives_a_nested_budget(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_TOOL_TIMEOUT", "900000"
        )  # → tool stall 960 < max age 1800
        assert config_check._check_stall_hierarchy() == []

    def test_mcp_tool_timeout_above_explicit_tool_budget_warns(self, monkeypatch):
        monkeypatch.setenv("MCP_TOOL_TIMEOUT", "900000")
        monkeypatch.setenv("TOOL_STALL_TIMEOUT", "600")
        issues = config_check._check_stall_hierarchy()
        assert any(
            "MCP_TOOL_TIMEOUT" in i.message and i.severity == "warning" for i in issues
        )

    def test_max_age_below_tool_budget_warns(self, monkeypatch):
        monkeypatch.setenv("TOOL_STALL_TIMEOUT", "2400")
        issues = config_check._check_stall_hierarchy()
        assert any("ACTIVE_TURN_MAX_AGE" in i.message for i in issues)

    def test_keepalive_off_warns_that_guards_are_dead(self, monkeypatch):
        monkeypatch.setenv("SSE_KEEPALIVE_INTERVAL", "0")
        issues = config_check._check_stall_hierarchy()
        assert any("SSE_KEEPALIVE_INTERVAL=0" in i.message for i in issues)

    def test_all_guards_disabled_is_quiet(self, monkeypatch):
        monkeypatch.setenv("STREAM_STALL_TIMEOUT", "0")
        monkeypatch.setenv("TOOL_STALL_TIMEOUT", "0")
        monkeypatch.setenv("ACTIVE_TURN_MAX_AGE", "0")
        assert config_check._check_stall_hierarchy() == []
