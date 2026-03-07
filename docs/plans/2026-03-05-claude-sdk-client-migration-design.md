# ClaudeSDKClient Migration Design

## Summary

Migrate session-based requests from `query()` to `ClaudeSDKClient` for native session continuity, while keeping stateless requests on the existing `query()` path.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Transition scope | Incremental — session requests only | Preserves existing stateless behavior, minimizes risk |
| Client pool location | Integrated into SessionManager | Session and client share lifecycle; reuse existing TTL/cleanup |
| Message history | Keep in SessionManager | API compatibility (`GET /v1/sessions/{id}`) |

## Architecture

### Before
```
request → main.py → SessionManager.process_messages() → full history accumulation
                                                          ↓
                    claude_cli.run_completion() ← MessageAdapter (full history→prompt)
                          ↓
                      query() (new session every call)
```

### After
```
request → main.py
            ├─ no session_id → claude_cli.run_completion() → query() (unchanged)
            └─ session_id    → claude_cli.run_completion_with_client()
                                      ↓
                                SessionManager.get_or_create_session()
                                      ↓
                                Session.client (ClaudeSDKClient)
                                      ↓
                                client.query() + client.receive_response()
                                (SDK maintains context, only new message sent)
```

## Changes by File

### session_manager.py

1. Add `client: Optional[Any]` field to `Session` dataclass
2. Convert `_cleanup_expired_sessions()` to async — call `client.disconnect()` on expired sessions
3. Convert `shutdown()` to async — disconnect all clients
4. Add `asyncio.Lock` per session for concurrent request safety

### claude_cli.py

1. Add `run_completion_with_client()` method:
   - Takes an existing `ClaudeSDKClient` instance
   - Calls `client.query(prompt)` + iterates `client.receive_response()`
   - Shares message dict conversion logic with `run_completion()` via private helper
2. Add `create_client()` factory method:
   - Creates and connects a `ClaudeSDKClient` with appropriate options
   - Used by main.py on first session request

### main.py

1. In `generate_streaming_response()` and `chat_completion()`:
   - If `session_id` present: use `run_completion_with_client()` path
   - If absent: existing `run_completion()` path (unchanged)
2. Client initialization on first session request:
   - Create `ClaudeSDKClient` via `claude_cli.create_client()`
   - Store in `session.client`
   - Subsequent requests reuse the client

## Error Handling

- `client.query()` failure: disconnect client, set `session.client = None`, retry once with fresh client
- `disconnect()` exception: catch and ignore, remove reference from session
- Concurrent requests to same session: per-session `asyncio.Lock`

## Testing Strategy

| Test | File | What |
|------|------|------|
| `run_completion_with_client()` unit | `test_claude_cli_unit.py` | Mock ClaudeSDKClient, verify query+receive_response called |
| Session+client lifecycle | `test_session_manager_unit.py` | Expired session triggers disconnect, shutdown disconnects all |
| Routing logic | `test_session_complete.py` | session_id presence routes to correct path |
| Existing tests pass | all | Stateless path unchanged |
