# Contributing

- 로컬 작업은 저장소 루트에서 시작하고, README와 `scripts/`의 계약을 우선합니다.
- 최초 설정은 `./scripts/bootstrap.sh`로 수행합니다.
- bootstrap은 `.env`, `.runtime/data`, `.runtime/work`, `.runtime/artifacts`, `.runtime/agent-home`을 준비합니다.
- 실제 Linear 실행 전 `.env`의 `LINEAR_API_KEY`, `LINEAR_TEAM_ID` 또는 프로젝트 값을 채우세요.
- 테스트는 기본적으로 `./scripts/test.sh`를 실행합니다.
- 특정 pytest 인자를 넘길 때는 `./scripts/test.sh <pytest-args>` 형식을 사용합니다.
- 빠른 로컬 확인이 필요하면 `pytest -q`로 단위 테스트를 실행합니다.
- 이슈 본문은 `Task`, `Acceptance Criteria`, `Validation` 섹션을 포함해야 합니다.
- `Task`에는 변경 범위와 금지 사항을 명확히 적습니다.
- `Acceptance Criteria`에는 완료로 인정할 관찰 가능한 조건을 나열합니다.
- `Validation`에는 실행할 명령, 기대 exit code, 증거로 남길 출력을 적습니다.
- 자동화는 `completed` 구조화 결과와 성공한 validation이 있을 때만 Done 처리합니다.
- 커밋은 작고 검토 가능한 단위로 유지하고, 자동 push/PR은 하지 않습니다.
- orchestrator 커밋 메시지는 `lao: <ISSUE-ID> <issue title>` 형식을 따릅니다.
- 사람이 직접 커밋할 때도 이슈 ID를 포함하고 명령형 요약을 사용하세요.
