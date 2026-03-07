---
name: dev-server
description: 개발 서버 관리 - 시작, 재시작, 상태 확인, 로그 조회. FastAPI + uvicorn 개발 워크플로우.
argument-hint: "[start|stop|restart|status|logs|test]"
allowed-tools: Bash, Read, Grep
---

# Dev Server Management

개발 서버(FastAPI + uvicorn)를 관리합니다.

## Commands

`$ARGUMENTS`에 따라 아래 동작을 수행합니다. 인자 없으면 `status`로 동작합니다.

### `start`
서버를 백그라운드로 시작합니다.
```bash
cd /home/jinyoung/claude-code-openai-wrapper && uv run uvicorn src.main:app --reload --host 0.0.0.0 --port 8000 &
```
- PID를 `/tmp/claude-wrapper.pid`에 저장
- 시작 후 2초 대기, health check로 정상 기동 확인

### `stop`
실행 중인 서버를 종료합니다.
```bash
# PID 파일 또는 프로세스 검색으로 종료
kill $(cat /tmp/claude-wrapper.pid 2>/dev/null) 2>/dev/null || pkill -f "uvicorn src.main:app" 2>/dev/null
```

### `restart`
`stop` 후 `start`를 순서대로 실행합니다.

### `status`
서버 상태를 확인합니다.
1. `pgrep -fa "uvicorn src.main:app"` 로 프로세스 확인
2. `curl -s http://localhost:8000/health` 로 health check
3. 결과를 간결하게 보고 (PID, 포트, 응답 상태)

### `logs`
서버 로그를 확인합니다. uvicorn이 stdout에 출력하므로:
- 실행 중이면 최근 로그 tail
- `journalctl` 또는 프로세스 출력 확인

### `test`
서버가 실행 중인지 확인 후, 주요 엔드포인트에 빠른 요청을 보냅니다.
```bash
# Health check
curl -s http://localhost:8000/health | python3 -m json.tool

# Models list
curl -s http://localhost:8000/v1/models | python3 -m json.tool

# Auth status
curl -s http://localhost:8000/auth/status | python3 -m json.tool
```

## Project Context

- **Entry point**: `src.main:app`
- **Default port**: 8000 (`.env`의 `PORT` 참조)
- **Host**: 0.0.0.0 (`CLAUDE_WRAPPER_HOST` 참조)
- **Config**: `.env` 파일에서 환경변수 로드
- **Docker**: `docker-compose.yml` 로도 실행 가능

## Rules

1. 서버 시작 시 반드시 `--reload` 플래그 포함 (개발 모드)
2. 포트 충돌 시 기존 프로세스를 먼저 확인하고 사용자에게 알림
3. 명령 실행 결과를 간결하게 한국어로 보고
