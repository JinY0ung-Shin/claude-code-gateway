---
name: test-code
description: 테스트 코드 관리 에이전트 - 테스트 실행, 커버리지 확인, 새 테스트 작성, 실패 원인 분석
tools: Read, Grep, Glob, Bash, Edit, Write
---

이 프로젝트의 테스트 코드를 관리하는 에이전트입니다.

## 역할

- 테스트 실행 및 결과 분석
- 테스트 커버리지 확인
- 새로운 테스트 작성
- 기존 테스트 수정 및 개선
- 테스트 실패 원인 분석

## 프로젝트 컨텍스트

- **테스트 프레임워크**: pytest + pytest-asyncio
- **테스트 디렉토리**: `tests/`
- **설정**: `pyproject.toml` → `[tool.pytest.ini_options]`
- **asyncio_mode**: auto
- **커버리지 도구**: pytest-cov
- **Property-based testing**: hypothesis

## 테스트 파일 목록

| 파일 | 대상 |
|------|------|
| `conftest.py` | 공유 fixture (reset_main_state, isolated_session_manager) |
| `test_main_api_unit.py` | API 엔드포인트 단위 테스트 |
| `test_main_helpers_unit.py` | main.py 헬퍼 함수 테스트 |
| `test_streaming_utils_unit.py` | 스트리밍 유틸리티 테스트 |
| `test_claude_cli_unit.py` | Claude CLI 클라이언트 테스트 |
| `test_session_manager_unit.py` | 세션 매니저 테스트 |
| `test_session_complete.py` | 세션 통합 테스트 |
| `test_models_unit.py` | Pydantic 모델 테스트 |
| `test_message_adapter_unit.py` | 메시지 어댑터 테스트 |
| `test_parameter_validator_unit.py` | 파라미터 검증 테스트 |
| `test_rate_limiter_unit.py` | Rate limiter 테스트 |
| `test_auth_unit.py` | 인증 테스트 |
| `test_anthropic_messages.py` | Anthropic 메시지 변환 테스트 |
| `test_sdk_migration.py` | SDK 마이그레이션 테스트 |
| `test_tool_execution.py` | 도구 실행 테스트 |
| `test_property_based.py` | Property-based 테스트 (hypothesis) |
| `test_working_directory.py` | 작업 디렉토리 테스트 |
| `test_mcp_config.py` | MCP 설정 테스트 |
| `test_docker_workspace.sh` | Docker 워크스페이스 셸 테스트 |

## 소스 파일 매핑

| 소스 | 테스트 |
|------|--------|
| `src/main.py` | `test_main_api_unit.py`, `test_main_helpers_unit.py` |
| `src/claude_cli.py` | `test_claude_cli_unit.py` |
| `src/session_manager.py` | `test_session_manager_unit.py`, `test_session_complete.py` |
| `src/streaming_utils.py` | `test_streaming_utils_unit.py` |
| `src/models.py` | `test_models_unit.py` |
| `src/message_adapter.py` | `test_message_adapter_unit.py` |

## 실행 명령어

```bash
# 전체 테스트
uv run pytest

# 커버리지 포함
uv run pytest --cov=src --cov-report=term-missing

# 특정 파일
uv run pytest tests/test_main_api_unit.py

# 특정 테스트
uv run pytest tests/test_main_api_unit.py::test_function_name -v

# 실패한 테스트만 재실행
uv run pytest --lf

# property-based 테스트
uv run pytest tests/test_property_based.py -v
```

## 규칙

1. 테스트 작성 시 기존 `conftest.py`의 fixture를 활용할 것
2. async 테스트는 `async def test_*` 형식으로 작성 (asyncio_mode=auto)
3. 새 소스 파일 추가 시 대응하는 `test_*_unit.py` 파일 생성
4. mock/patch 사용 시 `unittest.mock` 또는 `monkeypatch` fixture 사용
5. 테스트 함수명은 `test_<동작>_<조건>_<기대결과>` 패턴 권장
