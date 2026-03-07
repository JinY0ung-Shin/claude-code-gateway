#!/usr/bin/env python3
"""
Unit tests for src/streaming_utils.py.
"""

import json
import logging

import pytest

from src.models import ChatCompletionRequest, Message
from src.response_models import OutputItem, ResponseObject
from src.streaming_utils import make_response_sse, stream_chunks, stream_response_chunks


def _parse_chat_sse(line: str) -> dict:
    assert line.startswith("data: ")
    return json.loads(line[len("data: ") :])


def _parse_response_sse(line: str) -> tuple[str, dict]:
    event_line, data_line = line.strip().splitlines()
    assert event_line.startswith("event: ")
    assert data_line.startswith("data: ")
    return event_line[len("event: ") :], json.loads(data_line[len("data: ") :])


class TestMakeResponseSSE:
    def test_serializes_models_and_sequence_numbers(self):
        response_obj = ResponseObject(id="resp-1", model="claude-test")
        item = OutputItem(id="msg-1")

        line = make_response_sse(
            "response.created",
            response_obj=response_obj,
            item=item,
            sequence_number=7,
        )

        event_type, payload = _parse_response_sse(line)
        assert event_type == "response.created"
        assert payload["type"] == "response.created"
        assert payload["response"]["id"] == "resp-1"
        assert payload["item"]["id"] == "msg-1"
        assert payload["sequence_number"] == 7


@pytest.mark.asyncio
async def test_stream_chunks_formats_tool_results_from_legacy_user_messages():
    async def tool_result_source():
        yield {
            "type": "user",
            "content": "ignored",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "done",
                    }
                ]
            },
        }

    request = ChatCompletionRequest(
        model="claude-test",
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )
    chunks_buffer = []
    logger = logging.getLogger("test-stream-chunks-tool-result")

    lines = [
        line
        async for line in stream_chunks(tool_result_source(), request, "req-tool-result", chunks_buffer, logger)
    ]

    assert len(lines) == 2
    assert _parse_chat_sse(lines[0])["choices"][0]["delta"]["role"] == "assistant"
    assert "tool_result" in lines[1]
    assert chunks_buffer[0]["type"] == "user"


@pytest.mark.asyncio
async def test_stream_chunks_emits_role_for_empty_text_delta_then_fallback():
    async def empty_delta_source():
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": ""},
            },
        }

    request = ChatCompletionRequest(
        model="claude-test",
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )
    lines = [
        line
        async for line in stream_chunks(
            empty_delta_source(),
            request,
            "req-empty-delta",
            [],
            logging.getLogger("test-stream-chunks-empty-delta"),
        )
    ]

    assert len(lines) == 2
    assert _parse_chat_sse(lines[0])["choices"][0]["delta"]["role"] == "assistant"
    assert "unable to provide a response" in lines[1]


@pytest.mark.asyncio
async def test_stream_chunks_reassembles_tool_use_with_invalid_json_as_raw_text():
    async def invalid_tool_use_source():
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hi"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "tool-1", "name": "Read"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": "{bad json"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {"type": "content_block_stop", "index": 1},
        }

    request = ChatCompletionRequest(
        model="claude-test",
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )
    lines = [
        line
        async for line in stream_chunks(
            invalid_tool_use_source(),
            request,
            "req-invalid-tool-json",
            [],
            logging.getLogger("test-stream-chunks-invalid-tool-json"),
        )
    ]

    assert len(lines) == 3
    assert "Hi" in lines[1]
    payload = _parse_chat_sse(lines[2])
    content = payload["choices"][0]["delta"]["content"]
    assert '"type": "tool_use"' in content
    assert '"input": "{bad json"' in content


@pytest.mark.asyncio
async def test_stream_chunks_warns_when_tool_use_is_incomplete(caplog):
    async def incomplete_tool_source():
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 3,
                "content_block": {"type": "tool_use", "id": "tool-3", "name": "Write"},
            },
        }

    request = ChatCompletionRequest(
        model="claude-test",
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )
    logger = logging.getLogger("test-stream-chunks-incomplete-tool")

    with caplog.at_level(logging.WARNING):
        lines = [
            line
            async for line in stream_chunks(
                incomplete_tool_source(),
                request,
                "req-incomplete-tool",
                [],
                logger,
            )
        ]

    assert len(lines) == 2
    assert "Hello" in lines[1]
    assert "Incomplete tool_use blocks" in caplog.text


@pytest.mark.asyncio
async def test_stream_response_chunks_success_suppresses_thinking_and_formats_tool_blocks():
    async def success_source():
        yield {
            "type": "stream_event",
            "event": {"type": "content_block_start", "content_block": {"type": "thinking"}},
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "hidden"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {"type": "content_block_stop"},
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            },
        }
        yield {"content": [{"type": "text", "text": "duplicate assistant payload"}]}
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "tool-1", "name": "Read"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"path":"/tmp/demo.txt"}',
                },
            },
        }
        yield {
            "type": "stream_event",
            "event": {"type": "content_block_stop", "index": 1},
        }
        yield {
            "type": "user",
            "content": "ignored",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "done",
                    }
                ]
            },
        }

    chunks_buffer = []
    stream_result = {}
    logger = logging.getLogger("test-stream-response-success")

    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=success_source(),
            model="claude-test",
            response_id="resp-stream-1",
            output_item_id="msg-stream-1",
            chunks_buffer=chunks_buffer,
            logger=logger,
            prompt_text="Prompt text",
            metadata={"trace_id": "abc"},
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]
    event_types = [event_type for event_type, _payload in parsed]

    assert event_types[0] == "response.created"
    assert event_types[1] == "response.in_progress"
    assert "response.output_item.added" in event_types
    assert "response.content_part.added" in event_types
    assert event_types[-1] == "response.completed"
    assert event_types.index("response.output_text.done") < event_types.index("response.content_part.done")
    assert event_types.index("response.content_part.done") < event_types.index("response.output_item.done")
    assert event_types.index("response.output_item.done") < event_types.index("response.completed")

    deltas = [payload["delta"] for event_type, payload in parsed if event_type == "response.output_text.delta"]
    assert deltas[0] == "Hello"
    assert any("tool_use" in delta for delta in deltas[1:])
    assert any("tool_result" in delta for delta in deltas[1:])
    assert all("hidden" not in delta for delta in deltas)
    assert all("<think>" not in delta for delta in deltas)
    assert all("duplicate assistant payload" not in delta for delta in deltas)

    completed_payload = parsed[-1][1]
    assert completed_payload["response"]["status"] == "completed"
    assert completed_payload["response"]["metadata"] == {"trace_id": "abc"}
    assert completed_payload["response"]["usage"]["input_tokens"] == 2
    assert completed_payload["response"]["usage"]["output_tokens"] > 0
    assert stream_result["success"] is True
    assert len(chunks_buffer) == 1
    assert chunks_buffer[0]["type"] == "user"


@pytest.mark.asyncio
async def test_stream_response_chunks_formats_legacy_assistant_messages():
    async def legacy_assistant_source():
        yield {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Legacy answer"}]},
        }

    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=legacy_assistant_source(),
            model="claude-test",
            response_id="resp-stream-legacy",
            output_item_id="msg-stream-legacy",
            chunks_buffer=[],
            logger=logging.getLogger("test-stream-response-legacy"),
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]

    delta_payloads = [payload for event_type, payload in parsed if event_type == "response.output_text.delta"]
    assert delta_payloads[0]["delta"] == "Legacy answer"
    assert parsed[-1][1]["response"]["output"][0]["content"][0]["text"] == "Legacy answer"
    assert stream_result["success"] is True


@pytest.mark.asyncio
async def test_stream_response_chunks_emits_failed_event_for_sdk_error_chunk():
    async def sdk_error_source():
        yield {"is_error": True, "error_message": "sdk exploded"}

    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=sdk_error_source(),
            model="claude-test",
            response_id="resp-stream-sdk-error",
            output_item_id="msg-stream-sdk-error",
            chunks_buffer=[],
            logger=logging.getLogger("test-stream-response-sdk-error"),
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]

    assert parsed[-1][0] == "response.failed"
    assert parsed[-1][1]["response"]["error"]["code"] == "sdk_error"
    assert parsed[-1][1]["response"]["error"]["message"] == "sdk exploded"
    assert stream_result["success"] is False


@pytest.mark.asyncio
async def test_stream_response_chunks_emits_failed_event_for_empty_response():
    async def empty_source():
        yield {"type": "metadata"}

    chunks_buffer = []
    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=empty_source(),
            model="claude-test",
            response_id="resp-stream-empty",
            output_item_id="msg-stream-empty",
            chunks_buffer=chunks_buffer,
            logger=logging.getLogger("test-stream-response-empty"),
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]

    assert parsed[-1][0] == "response.failed"
    assert parsed[-1][1]["response"]["error"]["code"] == "empty_response"
    assert chunks_buffer == [{"type": "metadata"}]
    assert stream_result["success"] is False


@pytest.mark.asyncio
async def test_stream_response_chunks_emits_failed_event_for_unexpected_exception():
    async def exploding_source():
        raise RuntimeError("boom")
        yield  # pragma: no cover

    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=exploding_source(),
            model="claude-test",
            response_id="resp-stream-exception",
            output_item_id="msg-stream-exception",
            chunks_buffer=[],
            logger=logging.getLogger("test-stream-response-exception"),
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]

    assert parsed[-1][0] == "response.failed"
    assert parsed[-1][1]["response"]["error"]["code"] == "server_error"
    assert parsed[-1][1]["response"]["error"]["message"] == "Internal server error"
    assert stream_result["success"] is False


@pytest.mark.asyncio
async def test_stream_response_chunks_warns_on_incomplete_tool_use_and_still_completes(caplog):
    async def incomplete_tool_source():
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 9,
                "content_block": {"type": "tool_use", "id": "tool-9", "name": "Read"},
            },
        }

    stream_result = {}
    logger = logging.getLogger("test-stream-response-incomplete-tool")

    with caplog.at_level(logging.WARNING):
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=incomplete_tool_source(),
                model="claude-test",
                response_id="resp-stream-incomplete",
                output_item_id="msg-stream-incomplete",
                chunks_buffer=[],
                logger=logger,
                stream_result=stream_result,
            )
        ]

    parsed = [_parse_response_sse(line) for line in lines]
    assert parsed[-1][0] == "response.completed"
    assert stream_result["success"] is True
    assert "Incomplete tool_use blocks" in caplog.text
