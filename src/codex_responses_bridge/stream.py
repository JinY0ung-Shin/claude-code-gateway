"""Chat Completions stream -> OpenAI Responses SSE-event translation (#173 checkpoint-2A PR-2).

Pure, HTTP-independent. The streaming counterpart of ``response.py``: it consumes
chat/completions stream *chunk dicts* (already parsed off the wire) and yields
Responses *event dicts* in the exact shape the local SSE builder frames (each
carries its ``type`` and a monotonic ``sequence_number``; the route frames them,
so this stays a pure, testable translator like PR-1's ``request.py``).

Event ``type`` strings and payload shapes match the local streaming contract
(``src/streaming_utils.py``) wherever they overlap; the one place the reference
converter wins is the tool-call arg-delta events
(``response.function_call_arguments.delta``/``.done``), which the local Claude-SDK
path never emits but Codex, the bridge consumer, requires (OpenAI-standard).

Fail-closed: a streamed shape with no faithful Responses representation (a
refusal, an ``n>1`` chunk, a non-string arguments fragment) raises
:class:`BridgeCapabilityError`; an upstream runtime *error* chunk is the defined
terminal ``response.failed`` (not a raise).
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Iterator, Optional

from .errors import BridgeCapabilityError
from .response import (
    _RECOGNIZED_FINISH,
    _finish_to_status,
    _new_call_id,
    _new_msg_id,
    _new_reasoning_id,
    _ns_for,
    _usage_to_responses,
)


def _upstream_error_to_responses(err: Any) -> dict:
    """Map an upstream chat ``error`` payload to a Responses ``error`` object."""
    if isinstance(err, dict):
        code = err.get("code") or err.get("type") or "upstream_error"
        message = err.get("message") or "upstream error"
        return {"code": str(code), "message": str(message)}
    return {"code": "upstream_error", "message": str(err)}


class _StreamState:
    """Per-stream bookkeeping that turns chat delta chunks into ordered Responses
    events. Item lifecycles never nest: opening a message closes any open
    reasoning + tools; opening a tool closes any open reasoning + text."""

    def __init__(
        self,
        *,
        response_id: str,
        model: str,
        created_at: int,
        metadata: Optional[dict],
        emit_reasoning: bool,
        length_as_completed: bool,
        namespace_map: Optional[dict[str, str]],
    ) -> None:
        self.response_id = response_id
        self.model = model
        self.created_at = created_at
        self.metadata = metadata or {}
        self.emit_reasoning = emit_reasoning
        self.length_as_completed = length_as_completed
        self.namespace_map = namespace_map
        self._seq = 0
        self._next_oi = 0
        self.output: list[tuple[int, dict]] = []
        self.usage: Optional[dict] = None
        self.finish: Optional[str] = None
        # Only a RECOGNIZED upstream finish authorizes a terminal completed/
        # incomplete; iterator exhaustion without one is a truncated stream.
        self.terminal_seen: bool = False
        self.reasoning: Optional[dict] = None  # {id, oi, buf}
        self.text: Optional[dict] = None  # {id, oi, buf}
        self.tools: dict[Any, dict] = {}  # upstream index -> tool state
        self.tool_order: list[Any] = []

    # -- primitives ---------------------------------------------------------

    def _event(self, event_type: str, **payload: Any) -> dict:
        seq = self._seq
        self._seq += 1
        return {"type": event_type, **payload, "sequence_number": seq}

    def _claim_oi(self) -> int:
        oi = self._next_oi
        self._next_oi += 1
        return oi

    def _lean_object(
        self,
        *,
        status: str,
        output: list[dict],
        usage: Optional[dict],
        incomplete_details: Optional[dict] = None,
        error: Optional[dict] = None,
    ) -> dict:
        obj: dict[str, Any] = {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": output,
            "metadata": self.metadata,
        }
        if usage is not None:
            obj["usage"] = usage
        if incomplete_details is not None:
            obj["incomplete_details"] = incomplete_details
        if error is not None:
            obj["error"] = error
        return obj

    def _sorted_output(self) -> list[dict]:
        return [item for _oi, item in sorted(self.output, key=lambda pair: pair[0])]

    # -- preamble -----------------------------------------------------------

    def created_event(self) -> dict:
        return self._event(
            "response.created",
            response=self._lean_object(status="in_progress", output=[], usage=None),
        )

    def in_progress_event(self) -> dict:
        return self._event(
            "response.in_progress",
            response=self._lean_object(status="in_progress", output=[], usage=None),
        )

    # -- reasoning item -----------------------------------------------------

    def _handle_reasoning_delta(self, fragment: str) -> list[dict]:
        # Reasoning must PRECEDE any text/tool output. This is a validation
        # invariant, not permission to discard violations: a reasoning fragment
        # arriving after text/tool output has started cannot be represented
        # faithfully here (it would reorder before emitted output), so refuse
        # rather than silently drop it. Real-runtime certification may later
        # graduate a broader accepted ordering.
        if self.text is not None or self.tools:
            raise BridgeCapabilityError(
                "reasoning delta arrived after text/tool output started; this "
                "ordering has no faithful Responses representation here and is "
                "refused rather than silently dropped"
            )
        events: list[dict] = []
        if self.reasoning is None:
            rid = _new_reasoning_id()
            oi = self._claim_oi()
            self.reasoning = {"id": rid, "oi": oi, "buf": []}
            events.append(
                self._event(
                    "response.output_item.added",
                    output_index=oi,
                    item={
                        "id": rid,
                        "type": "reasoning",
                        "status": "in_progress",
                        "summary": [],
                    },
                )
            )
            events.append(
                self._event(
                    "response.reasoning_summary_part.added",
                    item_id=rid,
                    output_index=oi,
                    summary_index=0,
                    part={"type": "summary_text", "text": ""},
                )
            )
        rid = self.reasoning["id"]
        oi = self.reasoning["oi"]
        self.reasoning["buf"].append(fragment)
        events.append(
            self._event(
                "response.reasoning_summary_text.delta",
                item_id=rid,
                output_index=oi,
                summary_index=0,
                delta=fragment,
            )
        )
        events.append(
            self._event(
                "response.reasoning_text.delta",
                item_id=rid,
                output_index=oi,
                content_index=0,
                delta=fragment,
            )
        )
        return events

    def _close_reasoning(self, status: str = "completed") -> list[dict]:
        if self.reasoning is None:
            return []
        rid = self.reasoning["id"]
        oi = self.reasoning["oi"]
        full = "".join(self.reasoning["buf"])
        item = {
            "id": rid,
            "type": "reasoning",
            "status": status,
            "summary": [{"type": "summary_text", "text": full}],
            "content": [{"type": "reasoning_text", "text": full}],
        }
        self.output.append((oi, item))
        self.reasoning = None
        return [
            self._event(
                "response.reasoning_summary_text.done",
                item_id=rid,
                output_index=oi,
                summary_index=0,
                text=full,
            ),
            self._event(
                "response.reasoning_text.done",
                item_id=rid,
                output_index=oi,
                content_index=0,
                text=full,
            ),
            self._event(
                "response.reasoning_summary_part.done",
                item_id=rid,
                output_index=oi,
                summary_index=0,
                part={"type": "summary_text", "text": full},
            ),
            self._event("response.output_item.done", output_index=oi, item=item),
        ]

    # -- message item -------------------------------------------------------

    def _open_message(self) -> list[dict]:
        mid = _new_msg_id()
        oi = self._claim_oi()
        self.text = {"id": mid, "oi": oi, "buf": []}
        return [
            self._event(
                "response.output_item.added",
                output_index=oi,
                item={
                    "id": mid,
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            ),
            self._event(
                "response.content_part.added",
                item_id=mid,
                output_index=oi,
                content_index=0,
                part={"type": "output_text", "text": "", "annotations": []},
            ),
        ]

    def _handle_text_delta(self, fragment: str) -> list[dict]:
        events: list[dict] = []
        if self.text is None:
            events += self._close_reasoning("completed")
            events += self._close_all_tools("completed")
            events += self._open_message()
        self.text["buf"].append(fragment)
        events.append(
            self._event(
                "response.output_text.delta",
                item_id=self.text["id"],
                output_index=self.text["oi"],
                content_index=0,
                delta=fragment,
                logprobs=[],
            )
        )
        return events

    def _close_text(self, status: str = "completed") -> list[dict]:
        if self.text is None:
            return []
        mid = self.text["id"]
        oi = self.text["oi"]
        full = "".join(self.text["buf"])
        item = {
            "id": mid,
            "type": "message",
            "role": "assistant",
            "status": status,
            "content": [{"type": "output_text", "text": full, "annotations": []}],
        }
        self.output.append((oi, item))
        self.text = None
        return [
            self._event(
                "response.output_text.done",
                item_id=mid,
                output_index=oi,
                content_index=0,
                text=full,
                logprobs=[],
            ),
            self._event(
                "response.content_part.done",
                item_id=mid,
                output_index=oi,
                content_index=0,
                part={"type": "output_text", "text": full, "annotations": []},
            ),
            self._event("response.output_item.done", output_index=oi, item=item),
        ]

    # -- tool-call items ----------------------------------------------------

    def _open_tool(
        self, index: Any, upstream_id: Optional[str], name: str
    ) -> list[dict]:
        call_id = upstream_id or _new_call_id()
        oi = self._claim_oi()
        fc_id = f"fc_{call_id}"
        ns = _ns_for(name, self.namespace_map) if name else None
        self.tools[index] = {
            "fc_id": fc_id,
            "oi": oi,
            "call_id": call_id,
            "upstream_id": upstream_id,
            "name": name or "",
            "namespace": ns,
            "buf": [],
        }
        self.tool_order.append(index)
        item: dict[str, Any] = {
            "id": fc_id,
            "type": "function_call",
            "status": "in_progress",
            "call_id": call_id,
            "name": name or "",
            "arguments": "",
        }
        if ns:
            item["namespace"] = ns
        return [self._event("response.output_item.added", output_index=oi, item=item)]

    def _handle_tool_deltas(self, tool_calls: Any) -> list[dict]:
        if not isinstance(tool_calls, list):
            raise BridgeCapabilityError("streamed 'tool_calls' must be a list")
        events: list[dict] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                raise BridgeCapabilityError("streamed tool_call must be an object")
            # ``index`` is the state-identity key; a present malformed value must
            # fail through the capability-error path, never alias into 0 or raise
            # a raw TypeError when used as a dict key.
            index = tc.get("index", 0)
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise BridgeCapabilityError(
                    "streamed tool_call 'index' must be a non-negative integer, got "
                    f"{index!r}"
                )
            if "type" in tc and tc["type"] is not None and tc["type"] != "function":
                # A present tool-call kind other than 'function' would be
                # reinterpreted as a function call; refuse (omission stays
                # provider-compatible).
                raise BridgeCapabilityError(
                    f"streamed tool_call 'type' {tc['type']!r} is not 'function'; "
                    "refusing rather than reinterpreting it as a function call"
                )
            fn = tc.get("function")
            if fn is None:
                fn = {}
            elif not isinstance(fn, dict):
                raise BridgeCapabilityError(
                    "streamed tool_call 'function' must be an object, got "
                    f"{type(fn).__name__}"
                )
            # Present-vs-missing identity: a MISSING name may stay pending (it can
            # arrive on a later fragment); a PRESENT malformed one fails now. A
            # MISSING id keeps the documented synthesize-once compatibility; a
            # PRESENT malformed id is refused, never repaired into a synthesized id.
            name = None
            if "name" in fn and fn["name"] is not None:
                if not isinstance(fn["name"], str) or not fn["name"]:
                    raise BridgeCapabilityError(
                        "streamed tool_call 'function.name' must be a non-empty "
                        f"string when present, got {fn['name']!r}"
                    )
                name = fn["name"]
            if "id" in tc and tc["id"] is not None:
                if not isinstance(tc["id"], str) or not tc["id"]:
                    raise BridgeCapabilityError(
                        "streamed tool_call 'id' must be a non-empty string when "
                        f"present, got {tc['id']!r}"
                    )
                upstream_id = tc["id"]
            else:
                upstream_id = None
            existing = self.tools.get(index)
            # A non-empty id on an existing index that differs from the stored
            # upstream id means a NEW call reusing the slot: close the first so
            # its args aren't corrupted by the second (compare the real upstream
            # id, never the synthesized emitted call_id).
            is_new = bool(
                existing is not None
                and upstream_id
                and existing["upstream_id"]
                and upstream_id != existing["upstream_id"]
            )
            if existing is None or is_new:
                if is_new:
                    events += self._close_tool(index, "completed")
                events += self._close_reasoning("completed")
                events += self._close_text("completed")
                events += self._open_tool(index, upstream_id, name or "")
                existing = self.tools[index]
            else:
                if upstream_id and not existing["upstream_id"]:
                    existing["upstream_id"] = upstream_id
                if name and not existing["name"]:
                    # Name arrived on a later fragment: resolve its namespace now
                    # so the done event + terminal output can stamp it.
                    existing["name"] = name
                    existing["namespace"] = _ns_for(name, self.namespace_map)
            args_frag = fn.get("arguments")
            if args_frag is None:
                continue
            if not isinstance(args_frag, str):
                raise BridgeCapabilityError(
                    "streamed tool_call arguments fragment must be a string, got "
                    f"{type(args_frag).__name__}"
                )
            if args_frag:
                existing["buf"].append(args_frag)
                events.append(
                    self._event(
                        "response.function_call_arguments.delta",
                        item_id=existing["fc_id"],
                        output_index=existing["oi"],
                        delta=args_frag,
                    )
                )
        return events

    def _close_tool(self, index: Any, status: str) -> list[dict]:
        tool = self.tools.pop(index, None)
        if tool is None:
            return []
        if index in self.tool_order:
            self.tool_order.remove(index)
        if not tool["name"]:
            # A tool item cannot be finalized without a function name to route to
            # -- a name may legitimately arrive on a later fragment, but if none
            # ever did, refuse before emitting output_item.done rather than
            # emitting name:"".
            raise BridgeCapabilityError(
                "streamed tool_call was never given a function name; refusing "
                "rather than closing an unnamed tool call"
            )
        full = "".join(tool["buf"])
        item: dict[str, Any] = {
            "id": tool["fc_id"],
            "type": "function_call",
            "status": status,
            "call_id": tool["call_id"],
            "name": tool["name"],
            "arguments": full,
        }
        if tool["namespace"]:
            item["namespace"] = tool["namespace"]
        self.output.append((tool["oi"], item))
        return [
            self._event(
                "response.function_call_arguments.done",
                item_id=tool["fc_id"],
                output_index=tool["oi"],
                name=tool["name"],
                arguments=full,
            ),
            self._event(
                "response.output_item.done", output_index=tool["oi"], item=item
            ),
        ]

    def _close_all_tools(self, status: str) -> list[dict]:
        events: list[dict] = []
        for index in list(self.tool_order):
            events += self._close_tool(index, status)
        return events

    # -- per-chunk feed -----------------------------------------------------

    def feed(self, chunk: dict) -> list[dict]:
        # Presence-sensitive throughout: a specific valid shape (absent, None, or
        # the exact empty collection) is distinguished from a wrong-type value,
        # which is refused -- a false-y malformed shape must not masquerade as a
        # valid absence and silently shorten the Responses stream.
        if not isinstance(chunk, dict):
            raise BridgeCapabilityError("chat stream chunk must be an object")
        # Usage can arrive on a trailing chunk with empty/absent choices
        # (stream_options.include_usage); stash it for the terminal event.
        if chunk.get("usage") is not None:
            self.usage = _usage_to_responses(chunk["usage"])
        choices = chunk.get("choices")
        if choices is None:
            return []
        if not isinstance(choices, list):
            raise BridgeCapabilityError(
                "chat stream chunk 'choices' must be a list, got "
                f"{type(choices).__name__}"
            )
        if not choices:
            return []  # the valid trailing usage-only shape
        # Terminal evidence is LATCHED: once a recognized finish was observed, the
        # response's semantic output and terminal classification are immutable.
        # Only a usage-bearing trailer with empty/absent choices may follow (that
        # is handled above); a later non-empty choice -- more content/tool args, or
        # a second finish_reason that would reclassify the turn -- is refused rather
        # than allowed to mutate an already-finished response.
        if self.terminal_seen:
            raise BridgeCapabilityError(
                "chat stream produced a non-empty choice after a recognized "
                "terminal finish_reason; the response is already finished and its "
                "output/classification are immutable -- refusing rather than "
                "mutating a completed turn"
            )
        if len(choices) > 1:
            raise BridgeCapabilityError(
                "chat stream chunk carries multiple choices (n>1); Responses "
                "assumes one, refusing rather than interleaving alternatives"
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise BridgeCapabilityError("chat stream choice must be an object")
        finish = choice.get("finish_reason")
        if finish is not None:
            if finish not in _RECOGNIZED_FINISH:
                raise BridgeCapabilityError(
                    f"unrecognized chat finish_reason {finish!r}; refusing rather "
                    "than treating it as a terminal success"
                )
            self.finish = finish
            self.terminal_seen = True
        delta = choice.get("delta")
        if delta is None:
            return []
        if not isinstance(delta, dict):
            raise BridgeCapabilityError(
                f"chat stream 'delta' must be an object, got {type(delta).__name__}"
            )

        events: list[dict] = []
        if self.emit_reasoning and delta.get("reasoning_content") is not None:
            reasoning = delta["reasoning_content"]
            if not isinstance(reasoning, str):
                raise BridgeCapabilityError(
                    "streamed 'reasoning_content' must be a string, got "
                    f"{type(reasoning).__name__}"
                )
            if reasoning:
                events += self._handle_reasoning_delta(reasoning)
        if delta.get("refusal") is not None:
            raise BridgeCapabilityError(
                "streamed 'refusal' has no local Responses representation; "
                "refusing rather than dropping it"
            )
        if delta.get("content") is not None:
            content = delta["content"]
            if not isinstance(content, str):
                raise BridgeCapabilityError(
                    "streamed message 'content' delta must be a string, got "
                    f"{type(content).__name__}"
                )
            if content:
                events += self._handle_text_delta(content)
        if delta.get("tool_calls") is not None:
            events += self._handle_tool_deltas(delta["tool_calls"])
        return events

    def failed_event(self, err: Any) -> dict:
        return self._event(
            "response.failed",
            response=self._lean_object(
                status="failed",
                output=self._sorted_output(),
                usage=self.usage,
                error=_upstream_error_to_responses(err),
            ),
        )

    def incomplete_stream_failed(self) -> dict:
        """Terminal for a chat stream that ended with NO recognized finish.

        Only positive terminal evidence from the upstream protocol authorizes a
        ``response.completed``; iterator exhaustion mid-response is a truncation.
        Partial open items are NOT closed as completed (their ``output_item.done``
        is never emitted) -- only already-completed items appear in the failed
        object's output.
        """
        return self._event(
            "response.failed",
            response=self._lean_object(
                status="failed",
                output=self._sorted_output(),
                usage=self.usage,
                error={
                    "code": "incomplete_upstream_stream",
                    "message": (
                        "chat/completions stream closed before a terminal "
                        "finish_reason"
                    ),
                },
            ),
        )

    def finalize(self) -> list[dict]:
        status, incomplete = _finish_to_status(self.finish, self.length_as_completed)
        item_status = "incomplete" if status == "incomplete" else "completed"
        events: list[dict] = []
        events += self._close_reasoning("completed")
        events += self._close_text(item_status)
        events += self._close_all_tools(item_status)
        obj = self._lean_object(
            status=status,
            output=self._sorted_output(),
            usage=self.usage,
            incomplete_details=incomplete,
        )
        events.append(
            self._event(
                (
                    "response.incomplete"
                    if status == "incomplete"
                    else "response.completed"
                ),
                response=obj,
            )
        )
        return events


def chat_stream_to_responses_events(
    chunks: Iterable[dict],
    *,
    response_id: str,
    model: str = "",
    created_at: Optional[int] = None,
    metadata: Optional[dict] = None,
    emit_reasoning: bool = True,
    length_as_completed: bool = False,
    namespace_map: Optional[dict[str, str]] = None,
) -> Iterator[dict]:
    """Translate a chat/completions chunk stream to Responses event dicts.

    Yields ``response.created`` + ``response.in_progress``, then per-item
    lifecycle events, then a terminal event: ``response.completed``/
    ``.incomplete`` ONLY when a recognized upstream ``finish_reason`` was
    observed; ``response.failed`` on an upstream error chunk OR when the stream
    ends with no recognized terminal (a truncated stream is never fabricated into
    success). Each event dict carries its ``type`` and a monotonic
    ``sequence_number``; the route frames them as SSE. Abandoning the generator
    early (client disconnect / ``gen.close()``) simply stops -- no terminal event
    is synthesized then.
    """
    state = _StreamState(
        response_id=response_id,
        model=model,
        created_at=created_at if created_at is not None else int(time.time()),
        metadata=metadata,
        emit_reasoning=emit_reasoning,
        length_as_completed=length_as_completed,
        namespace_map=namespace_map,
    )
    yield state.created_event()
    yield state.in_progress_event()
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise BridgeCapabilityError("chat stream chunk must be an object")
        err = chunk.get("error")
        if err is not None:
            yield state.failed_event(err)
            return
        for event in state.feed(chunk):
            yield event
    # Only a recognized upstream terminal finish authorizes response.completed.
    # Iterator exhaustion without one is a truncated stream -> failed, never a
    # fabricated success. (Consumer-side cancellation via gen.close() stops the
    # loop before this point, so no terminal is synthesized then.)
    if state.terminal_seen:
        for event in state.finalize():
            yield event
    else:
        yield state.incomplete_stream_failed()
