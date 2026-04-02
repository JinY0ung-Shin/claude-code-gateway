# OpenCode Backend Integration Design

## Summary

OpenCode(`anomalyco/opencode`)를 두 번째 백엔드로 추가한다. OpenCode의 HTTP API(`opencode serve`)를 사이드카로 운영하고, `prompt_async` + SSE 이벤트 버스를 통해 비동기 스트리밍 완성을 구현한다.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| 통신 방식 | HTTP API (Approach A) | `opencode serve`는 80+ REST 엔드포인트 제공. subprocess보다 효율적이고 세션 재사용 가능 |
| 스트리밍 | `prompt_async` + SSE `GET /event` | 논블로킹. sync `POST /message`는 완료까지 블로킹되므로 부적합 |
| 세션 관리 | OpenCode 내장 세션 사용 + 게이트웨이 SessionManager 매핑 | OpenCode가 SQLite 기반 영속 세션을 제공하므로 이중 관리 불필요 |
| 서버 라이프사이클 | 외부 관리 (사용자가 별도로 `opencode serve` 실행) | 프로세스 관리 복잡도를 줄이고, Docker compose로 분리 가능 |
| 모델 선택 | 요청 시 `model` 필드로 오버라이드 | OpenCode가 멀티 프로바이더 지원하므로 활용 |
| 권한 처리 | 세션 생성 시 `permission: [{ permission: "*", pattern: "*", action: "allow" }]` | 게이트웨이는 자동화 용도이므로 매번 승인 불필요 |
| Bun 의존성 | 런타임 요구사항으로 문서화 | Python 패키지로 해결 불가. Docker 이미지에 Bun 포함 |

## Architecture

### Before (Claude only)
```
Client → FastAPI Gateway → BackendRegistry.get("claude")
                                   ↓
                           ClaudeCodeCLI.run_completion()
                                   ↓
                           claude_agent_sdk.query() → AsyncIterator[Dict]
                                   ↓
                           streaming_utils → SSE chunks → Client
```

### After (Claude + OpenCode)
```
Client → FastAPI Gateway → resolve_model("opencode/claude-sonnet")
                                   ↓
                           BackendRegistry.get("opencode")
                                   ↓
                           OpenCodeClient.run_completion()
                                   ↓
                           ┌─ POST /session (if new)
                           ├─ POST /session/{id}/prompt_async (fire, 204)
                           ├─ GET /event (SSE, filter by sessionID)
                           │    ├─ message.part.delta → yield text chunk
                           │    ├─ message.part.updated (type=tool) → yield tool event
                           │    ├─ session.error → yield error
                           │    └─ session.idle → break (turn complete)
                           └─ yield final result dict
                                   ↓
                           streaming_utils → SSE chunks → Client
```

## Model Resolution

```
"opencode/claude-sonnet"  → backend="opencode", provider_model="claude-sonnet"
"opencode/gpt-4o"         → backend="opencode", provider_model="gpt-4o"
"opencode/gemini-pro"     → backend="opencode", provider_model="gemini-pro"
```

`opencode/` 프리픽스로 라우팅. provider_model은 OpenCode의 `providerID/modelID` 형식으로 매핑된다.

OpenCode가 지원하는 모델 목록은 서버 시작 시 `GET /provider` 엔드포인트에서 동적으로 조회한다.

### 동적 모델 목록 캐싱

`supported_models()`는 `GET /provider`에서 조회한 결과를 메모리에 캐시한다.

- **초기 로딩**: `register()` 시점에 `GET /provider`를 동기적으로 호출(`httpx.Client`)하여 캐시를 프리페치한다.
- **`supported_models()`는 sync 메서드**: 캐시된 리스트를 즉시 반환한다. HTTP 호출 없음 (BackendClient Protocol이 sync를 요구).
- **캐시 TTL**: 5분. `_models_cache_expires` 타임스탬프로 관리.
- **캐시 갱신**: `run_completion()` 진입 시 TTL 만료 여부를 확인하고, 만료 시 async로 `GET /provider` 재조회하여 캐시 + descriptor 갱신.
- **`BackendDescriptor.models`**: 등록 시점의 스냅샷. `/v1/models` 응답은 이 정적 목록 기반. 캐시 갱신 시 descriptor를 재등록하여 동기화. (기존 스냅샷을 보유한 코드에는 영향 없음 — `available_models()`는 매번 fresh 조회)
- **서버 재시작**: OpenCode 서버가 다른 프로바이더 설정으로 재시작되면, 다음 캐시 만료 시점에 자동 반영.

## SSE Event → run_completion Yield 매핑

OpenCode SSE 이벤트를 `streaming_utils.py`가 기대하는 **정확한 dict 구조**로 변환해야 한다.
`streaming_utils`는 Claude SDK의 `_convert_message()`가 생성하는 내부 포맷에 의존한다.
event_bridge는 이 포맷을 정확히 재현해야 기존 파이프라인이 변경 없이 동작한다.

### Text Streaming (token-level)

`extract_stream_event_delta()` (`streaming_utils.py:385-407`)가 기대하는 구조:

```python
# OpenCode: message.part.delta (field="text", delta="Hello")
# → yield:
{
    "type": "stream_event",
    "event": {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "Hello"}
    }
}
```

Thinking 블록 시작/종료도 동일 패턴:
```python
# OpenCode: message.part.updated (type="reasoning", first appearance)
# → yield:
{
    "type": "stream_event",
    "event": {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "thinking"}
    }
}

# OpenCode: message.part.delta (reasoning delta)
# → yield:
{
    "type": "stream_event",
    "event": {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "thinking_delta", "thinking": "..."}
    }
}
```

### Tool Use Streaming

`ToolUseAccumulator.process_stream_event()` (`streaming_utils.py:654-717`)가 기대하는 3단계 시퀀스:

```python
# 1. Tool start (OpenCode: message.part.updated, type="tool", status="pending")
{
    "type": "stream_event",
    "event": {
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "tool_use", "id": "call_abc", "name": "bash"}
    }
}

# 2. Tool input (OpenCode: message.part.updated, type="tool", status="running")
{
    "type": "stream_event",
    "event": {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": "{\"command\":\"ls\"}"}
    }
}

# 3. Tool stop (OpenCode: message.part.updated, type="tool", status="completed")
{
    "type": "stream_event",
    "event": {
        "type": "content_block_stop",
        "index": 1
    }
}
```

### Assistant & Result Messages

```python
# OpenCode: message.updated (assistant message completed)
# → yield:
{
    "type": "assistant",
    "content": [{"type": "text", "text": "full response text"}],
    "usage": {"input_tokens": 100, "output_tokens": 50}
}

# OpenCode: session.idle (turn complete, after final message.updated)
# → yield:
{
    "type": "result",
    "subtype": "success",
    "result": "full response text",
    "usage": {"input_tokens": 100, "output_tokens": 50}
}

# OpenCode: session.error
# → yield:
{
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "error_message": "error description"
}
```

### Index Tracking

event_bridge는 내부적으로 `_block_index` 카운터를 유지한다.
새로운 content block이 시작될 때마다 증가시켜 `ToolUseAccumulator`의 `(parent_id, index)` 키가 정확히 동작하도록 한다.

## Changes by File

### New: `src/backends/opencode/`

```
src/backends/opencode/
├── __init__.py            # register(), OPENCODE_DESCRIPTOR, _opencode_resolve()
├── client.py              # OpenCodeClient (BackendClient protocol 구현)
├── constants.py           # OPENCODE_BASE_URL, OPENCODE_PASSWORD, 기본 설정
├── auth.py                # OpenCodeAuthProvider (서버 연결 + Basic Auth 검증)
├── event_dispatcher.py    # 싱글톤 SSE 연결 + 세션별 Queue 멀티플렉싱
└── event_bridge.py        # 개별 이벤트 → streaming_utils 호환 dict 변환기
```

### `src/backends/opencode/__init__.py`

1. `_opencode_resolve(model)` — `opencode/` 프리픽스 파싱
2. `OPENCODE_DESCRIPTOR` — 동적 모델 목록 (`GET /provider`에서 조회)
3. `register()` — descriptor + client 등록. 서버 연결 실패 시 descriptor만 등록

### `src/backends/opencode/client.py`

`OpenCodeClient` 클래스:

- **`__init__(base_url, password=None)`** — httpx.AsyncClient 생성, Basic Auth 설정
- **`name`** → `"opencode"`
- **`owned_by`** → `"anomaly"`
- **`supported_models()`** → `GET /provider`에서 조회한 모델 목록 캐시 (TTL 5분, 아래 참조)
- **`resolve(model)`** → `_opencode_resolve` 위임
- **`build_options(request, resolved, overrides)`** → prompt, model, system_prompt 추출
- **`get_auth_provider()`** → `OpenCodeAuthProvider()` 반환
- **`run_completion(...)`** — 핵심 메서드 (아래 상세)
- **`parse_message(messages)`** — 마지막 assistant text 추출
- **`estimate_token_usage(prompt, completion)`** — 문자 기반 추정 (Claude와 동일)
- **`verify()`** — `GET /global/health` 호출, `healthy: true` 확인

#### `run_completion` 파라미터 매핑

| BackendClient 파라미터 | OpenCode API 매핑 | 비고 |
|------------------------|-------------------|------|
| `prompt` | `POST /session/{id}/prompt_async` body의 `parts: [{type: "text", text: prompt}]` | 필수 |
| `system_prompt` | `POST /session/{id}/prompt_async` body의 `system` 필드 | Optional |
| `model` | `POST /session/{id}/prompt_async` body의 `model: {providerID, modelID}` | `provider_model`을 `/`로 분리하여 매핑 |
| `stream` | 항상 prompt_async + SSE 사용. `stream=False` 시에도 내부적으로 SSE를 소비하되, 중간 `stream_event` dict를 yield하지 않고 최종 `assistant` + `result` dict만 yield | |
| `max_turns` | OpenCode에 직접 대응 없음. event_bridge에서 assistant turn 카운트로 소프트 제한 | 초과 시 `POST /session/{id}/abort` |
| `allowed_tools` | 세션 생성 시 `permission` ruleset으로 매핑. 지정 도구만 `allow`, 나머지 `reject` | |
| `disallowed_tools` | 세션 생성 시 해당 도구를 `reject` 패턴으로 추가 | |
| `session_id` | 게이트웨이 session → OpenCode session 매핑 (신규 생성) | |
| `resume` | 기존 `provider_session_id`로 OpenCode 세션에 메시지 추가 전송 | |
| `permission_mode` | 세션 `permission` ruleset 구성에 반영 (`bypassPermissions` → all-allow) | |
| `output_format` | `POST /session/{id}/prompt_async` body의 `format` 필드 | `{"type": "json_schema", "schema": ...}` |
| `mcp_servers` | 무시 (OpenCode 서버 자체 MCP 설정 사용) | |
| `task_budget` | 무시 (OpenCode에 토큰 예산 개념 없음) | |

#### `run_completion` 실행 흐름

```
1. session_id/resume로 OpenCode 세션 결정
   ├─ resume 있음 → provider_session_id로 기존 OpenCode 세션 사용
   └─ session_id 있음 → 새 OpenCode 세션 생성 (POST /session)
       └─ permission ruleset, allowed/disallowed_tools 반영

2. EventDispatcher.subscribe(opencode_session_id) → asyncio.Queue 획득
   (SSE 연결은 이미 확립된 상태)

3. POST /session/{id}/prompt_async (parts, model, system, format)
   (204 반환, 비동기 처리 시작)

4. Queue에서 이벤트 소비:
   ├─ message.part.delta → event_bridge.translate() → yield stream_event dict
   ├─ message.part.updated (tool) → event_bridge.translate() → yield stream_event dict
   ├─ permission.asked → POST /permission/{id}/reply (auto-approve)
   ├─ session.error → yield error result dict → break
   ├─ max_turns 초과 → POST /session/{id}/abort → yield error result dict → break
   ├─ timeout 초과 → POST /session/{id}/abort → yield error result dict → break
   └─ session.idle → yield final result dict → break

5. EventDispatcher.unsubscribe(opencode_session_id)
```

#### httpx.AsyncClient 라이프사이클

`OpenCodeClient`는 `__init__`에서 `httpx.AsyncClient`를 생성한다. 정리는:
- FastAPI `shutdown` 이벤트에서 `await client.aclose()` 호출
- `register()` 함수가 `atexit` 핸들러 대신 `main.py`의 `@app.on_event("shutdown")`에 정리 콜백 등록
- 이 방식은 connection pooling을 활용하면서도 리소스 누수를 방지

### `src/backends/opencode/event_dispatcher.py`

**싱글톤 `EventDispatcher`** — 글로벌 SSE 이벤트 버스에 단일 연결을 유지하고, 세션별 asyncio.Queue로 이벤트를 멀티플렉싱한다.

동시 N개 요청이 각각 SSE 연결을 여는 대신, 1개 연결로 모든 세션 이벤트를 수신하여 분배한다.

```python
class EventDispatcher:
    """Singleton: 단일 SSE 연결 → 세션별 Queue 분배."""

    _instance: Optional["EventDispatcher"] = None

    def __init__(self, base_url: str, directory: str, auth: Optional[tuple]):
        self._base_url = base_url
        self._directory = directory       # OPENCODE_DIRECTORY 값
        self._auth = auth
        self._subscribers: Dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()
        self._sse_task: Optional[asyncio.Task] = None

    @classmethod
    def get_or_create(cls, base_url, directory, auth=None) -> "EventDispatcher":
        """register() 시점(서버 시작)에만 호출된다. 요청 처리 중에는 호출하지 않는다.
        서버 시작은 단일 스레드이므로 별도 lock이 불필요하다."""
        if cls._instance is None:
            cls._instance = cls(base_url, directory, auth)
        return cls._instance

    async def start(self):
        """SSE 리스너 태스크 시작. 이미 실행 중이면 무시."""
        if self._sse_task is None or self._sse_task.done():
            self._sse_task = asyncio.create_task(self._listen())

    async def subscribe(self, session_id: str) -> asyncio.Queue:
        """세션에 대한 이벤트 큐 등록. run_completion 시작 시 호출."""
        async with self._lock:
            q = asyncio.Queue(maxsize=1000)
            self._subscribers[session_id] = q
            return q

    async def unsubscribe(self, session_id: str):
        """세션 이벤트 큐 해제. run_completion 종료 시 호출."""
        async with self._lock:
            self._subscribers.pop(session_id, None)

    async def _listen(self):
        """GET /event?directory=... SSE 스트림에서 이벤트 수신 후 분배.

        Pseudocode:
            retries = 0
            while retries <= 1:
                try:
                    async with httpx_sse.aconnect_sse(client, "GET", url) as source:
                        retries = 0  # 연결 성공 시 리셋
                        async for event in source.aiter_sse():
                            data = json.loads(event.data)
                            sid = data.get("properties", {}).get("sessionID")
                            if sid and sid in self._subscribers:
                                try:
                                    self._subscribers[sid].put_nowait(data)
                                except asyncio.QueueFull:
                                    logger.warning("Event queue full for session %s", sid)
                except (httpx.ReadError, httpx.RemoteProtocolError):
                    retries += 1
                    if retries <= 1:
                        await asyncio.sleep(1)
                        continue
                    # 재연결 실패: 모든 subscriber에 에러 전파
                    error_event = {
                        "type": "result",
                        "subtype": "error_during_execution",
                        "is_error": True,
                        "error_message": "SSE connection lost and reconnect failed"
                    }
                    async with self._lock:
                        for q in self._subscribers.values():
                            try:
                                q.put_nowait(error_event)
                            except asyncio.QueueFull:
                                pass
                    break
        """
        ...

    async def close(self):
        """SSE 연결 종료. FastAPI shutdown 시 호출."""
        if self._sse_task:
            self._sse_task.cancel()
        self._subscribers.clear()
```

**`directory` 파라미터**: `OPENCODE_DIRECTORY` 환경변수 값을 사용한다. 이는 `opencode serve` 실행 시의 프로젝트 디렉토리와 일치해야 한다. OpenCode는 디렉토리별로 프로젝트 인스턴스를 분리하므로, 이 값이 일치하지 않으면 이벤트를 수신할 수 없다.

### `src/backends/opencode/event_bridge.py`

**`EventBridge`** — 개별 OpenCode 이벤트를 `streaming_utils` 호환 dict로 변환하는 per-turn stateful 변환기.

- **`translate(event)`** — 위 "SSE Event → run_completion Yield 매핑" 섹션의 규칙대로 변환
- **`_block_index`** — content block 인덱스 추적 (text=0부터, tool은 순차 증가)
- **`reset()`** — 새 턴 시작 시 인덱스 초기화

SSE 파싱은 `httpx-sse` 라이브러리를 사용한다 (확정). 수동 파싱은 multi-line data, id 필드 등 엣지 케이스에서 깨질 수 있다.

### `src/backends/opencode/auth.py`

`OpenCodeAuthProvider`:

- **`name`** → `"opencode"`
- **`validate()`** — `OPENCODE_BASE_URL` 환경변수 존재 여부 + health check
- **`build_env()`** — `OPENCODE_BASE_URL`, `OPENCODE_SERVER_PASSWORD`

### `src/backends/opencode/constants.py`

```python
OPENCODE_BASE_URL = os.getenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
OPENCODE_PASSWORD = os.getenv("OPENCODE_SERVER_PASSWORD")
OPENCODE_USERNAME = os.getenv("OPENCODE_SERVER_USERNAME", "opencode")
OPENCODE_DIRECTORY = os.getenv("OPENCODE_DIRECTORY", os.getcwd())
OPENCODE_DEFAULT_AGENT = os.getenv("OPENCODE_DEFAULT_AGENT", "build")
OPENCODE_PERMISSION_MODE = "permissive"  # auto-approve all tool calls
```

### `src/backends/__init__.py`

`discover_backends()`에 OpenCode 등록 추가:

```python
def discover_backends(registry_cls=None):
    if registry_cls is None:
        registry_cls = BackendRegistry

    from src.backends.claude import register as register_claude
    register_claude(registry_cls=registry_cls)

    from src.backends.opencode import register as register_opencode
    register_opencode(registry_cls=registry_cls)
```

### `.env.example`

```
# OpenCode Backend (optional)
OPENCODE_BASE_URL=http://127.0.0.1:4096
OPENCODE_SERVER_PASSWORD=
OPENCODE_DIRECTORY=/path/to/project
OPENCODE_DEFAULT_AGENT=build
```

### `README.md`

OpenCode 백엔드 섹션 추가: 설정 방법, 모델 포맷, Docker 구성.

## Session Mapping

```
Gateway SessionManager          OpenCode Server
┌────────────────────┐         ┌──────────────────┐
│ Session             │         │ Session           │
│   id: "gw-abc123"  │────────→│   id: "ses..."    │
│   backend: "opencode"│        │   (SQLite 저장)    │
│   provider_session_id│        │                    │
│     = "ses..."      │         │                    │
└────────────────────┘         └──────────────────┘
```

게이트웨이 `Session.provider_session_id`에 OpenCode session ID를 저장. 이후 턴에서 동일 OpenCode 세션에 메시지 전송.

### 세션 생성 요청 상세

```python
# POST /session
{
    "title": f"gateway-{gateway_session_id}",
    "permission": [
        # permission_mode == "bypassPermissions" 또는 기본
        {"permission": "*", "pattern": "*", "action": "allow"}
    ],
    # allowed_tools가 지정된 경우:
    # [{"permission": "bash", "pattern": "*", "action": "allow"},
    #  {"permission": "*", "pattern": "*", "action": "reject"}]
}
```

- `model`은 세션이 아닌 **프롬프트 수준**에서 지정 (`POST /session/{id}/prompt_async`의 `model` 필드)
- `agent`도 프롬프트 수준에서 지정 (기본값: `OPENCODE_DEFAULT_AGENT`)
- 세션 정리: 게이트웨이 TTL 만료 시 `DELETE /session/{opencode_session_id}` 호출

## Permission Auto-Approval

OpenCode는 도구 실행 시 권한을 요구할 수 있다. 게이트웨이는 자동화 목적이므로:

1. 세션 생성 시 permissive ruleset 설정:
   ```json
   {
     "permission": [
       {"permission": "*", "pattern": "*", "action": "allow"}
     ]
   }
   ```
2. 만약 `permission.asked` 이벤트가 발생하면 즉시 `POST /permission/{id}/reply` with `reply: "always"` 응답

### 타이밍 보장

**SSE 연결은 반드시 `prompt_async` 호출 이전에 확립되어야 한다.**

```
1. EventDispatcher.subscribe(session_id)    ← Queue 등록
2. EventDispatcher가 이미 SSE 리스닝 중 확인   ← 연결 보장
3. POST /session/{id}/prompt_async          ← 프롬프트 전송
4. Queue에서 이벤트 소비                       ← permission.asked 포함
```

이 순서를 지키면 permission 이벤트 누락이 발생하지 않는다. EventDispatcher의 `_listen` 태스크는 `start()` 시점에 실행되므로, `run_completion` 진입 시점에는 이미 SSE 스트림이 활성 상태다.

만약 SSE 연결이 끊긴 상태에서 permission이 발생하면, OpenCode는 permission이 응답될 때까지 블로킹한다. SSE 재연결 후 pending permission 상태를 `GET /session/{id}` 또는 재수신되는 `permission.asked` 이벤트로 확인하고 응답한다.

## Token Usage

OpenCode의 `message.updated` 이벤트에는 assistant message 메타데이터가 포함된다. 여기에 토큰 사용량(`tokens` 필드)이 있는 경우 실제 값을 사용한다.

```python
# message.updated 이벤트의 info 객체에서 추출:
info = event["properties"]["info"]
usage = {
    "input_tokens": info.get("tokens", {}).get("input", 0),
    "output_tokens": info.get("tokens", {}).get("output", 0),
}
```

실제 사용량이 없는 경우 `estimate_token_usage()`의 문자 기반 추정(~4자/토큰)으로 fallback한다. 이는 `streaming_utils.resolve_token_usage()`의 기존 fallback 체인과 동일하게 동작한다.

## Error Handling

| 상황 | 처리 |
|------|------|
| OpenCode 서버 미실행 | `verify()` 실패 → descriptor만 등록, 모델 요청 시 503 |
| SSE 연결 끊김 | EventDispatcher가 1회 재연결 시도 (1초 대기). 실패 시 모든 활성 subscriber Queue에 에러 이벤트 전송 |
| `session.error` 이벤트 | 에러 내용을 `{"type": "result", "subtype": "error_during_execution", ...}` dict으로 변환하여 yield |
| 프롬프트 전송 실패 (4xx) | BackendConfigError 발생 (HTTP status 전달) |
| 프롬프트 전송 실패 (5xx) | 에러 result dict yield |
| 타임아웃 | `constants.DEFAULT_TIMEOUT_MS` 적용, `asyncio.wait_for`로 Queue 소비 감시. 초과 시 `POST /session/{id}/abort` + 에러 yield |
| SSE 재연결 중 prompt 이미 처리됨 | OpenCode 세션은 서버 측에서 상태를 유지. 재연결 후 `GET /session/{id}/message`로 놓친 메시지 복구 가능 |

## Dependencies

### Python (추가)
- `httpx-sse` — SSE 클라이언트 파싱 (확정)

### System
- **Bun** 런타임 — OpenCode 서버 실행에 필요
- `opencode-ai` npm 패키지 (`bun install -g opencode-ai`)

### Docker
```yaml
# docker-compose.yml
services:
  opencode:
    image: oven/bun:latest
    command: ["bun", "x", "opencode-ai", "serve", "--port", "4096", "--hostname", "0.0.0.0"]
    environment:
      OPENCODE_SERVER_PASSWORD: ${OPENCODE_SERVER_PASSWORD}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    ports:
      - "4096:4096"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:4096/global/health"]
      interval: 10s
      timeout: 5s
      retries: 5

  gateway:
    # ... existing config
    environment:
      OPENCODE_BASE_URL: http://opencode:4096
      OPENCODE_SERVER_PASSWORD: ${OPENCODE_SERVER_PASSWORD}
      OPENCODE_DIRECTORY: /workspace
    depends_on:
      opencode:
        condition: service_healthy
```

## Testing

### Unit Tests
- `test_opencode_client.py` — httpx mock으로 모든 BackendClient 메서드 테스트
- `test_opencode_event_bridge.py` — SSE 이벤트 파싱 및 변환 테스트
- `test_opencode_auth.py` — 인증 검증 테스트
- `test_opencode_resolve.py` — 모델 해석 테스트

### Contract Tests
- 기존 `test_backend_contract.py` 패턴 확장 — OpenCode backend도 프로토콜 준수 검증

### Integration Tests (opt-in)
- `OPENCODE_INTEGRATION=1` 환경변수로 활성화
- 실제 `opencode serve` 인스턴스에 대해 세션 생성 → 메시지 → 스트리밍 → 정리 흐름 테스트

## Implementation Phases

### Phase 1: Core Client
- `constants.py`, `auth.py`, `client.py` 기본 구조
- `run_completion()`에서 동기 `POST /message` 사용 (스트리밍 없이 동작 확인)
- `verify()`, `parse_message()`, `estimate_token_usage()`
- 단위 테스트 + 계약 테스트

### Phase 2: SSE Streaming
- `event_bridge.py` 구현
- `run_completion()`을 `prompt_async` + SSE로 전환
- `message.part.delta` → 스트리밍 text yield
- 권한 자동 승인

### Phase 3: Session & Multi-turn
- 게이트웨이 session_id ↔ OpenCode session ID 매핑
- `/v1/responses`의 `previous_response_id` 지원
- 세션 정리 (TTL 만료 시 OpenCode 세션 삭제)

### Phase 4: Docker & CI
- docker-compose.yml 업데이트
- CI에서 OpenCode 서버 포함 통합 테스트
- README, .env.example 업데이트
