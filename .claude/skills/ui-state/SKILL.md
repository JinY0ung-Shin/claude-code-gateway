---
name: ui-state
description: Read and control the a2a-agent frontend UI state. Use when you need to check what the user sees on screen (dashboard filters, selected items, active view) or send UI commands (navigate back, clear filters, set keyword search).
allowed-tools: Bash(curl *)
---

# a2a-agent UI State API

The a2a-agent exposes two endpoints for reading and writing frontend UI state.
Base URL is set via the `A2A_AGENT_URL` environment variable (default: `http://localhost:8000`).

## Read UI State

Query the current frontend state (dashboard filters, selected row, active view, etc.):

```bash
curl -s "${A2A_AGENT_URL:-http://localhost:8000}/api/ui-state?thread_id=${THREAD_ID}" | python3 -m json.tool
```

Response shape:
```json
{
  "thread_id": "...",
  "ui_state": {
    "activeView": "dashboard" | "detail",
    "filters": { ... },
    "selectedRow": null | { ... },
    ...
  }
}
```

Read UI state **before** deciding which command to send so you act on current information.

## Send UI Commands

Post a command to change the frontend:

```bash
curl -s -X POST "${A2A_AGENT_URL:-http://localhost:8000}/api/ui-commands?thread_id=${THREAD_ID}" \
  -H "Content-Type: application/json" \
  -d '{"action": "<ACTION>", "params": { ... }}'
```

Response shape:
```json
{
  "status": "queued",
  "command_id": "uuid"
}
```

### Available actions (MVP)

| Action | Params | Effect |
|--------|--------|--------|
| `go_back` | `{}` | Detail View -> Dashboard |
| `clear_filters` | `{}` | Clear all active filters |
| `set_keyword` | `{"keyword": "search term"}` | Set keyword search |

## Workflow

1. Read UI state to understand current view
2. Decide the appropriate action based on user intent
3. Send the command
4. Read UI state again to confirm the change took effect
