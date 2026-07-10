# linear_agent_orchestrator Architecture

## 모듈 역할
- `__init__`: 패키지 버전과 공개 심볼을 제공한다.
- `cli`: 설정 로드, 로깅 초기화, 검증/데몬/드라이런/단건 처리 CLI 명령을 연결한다.
- `config`: 환경 변수와 `.env`를 `AppConfig`로 병합하고 Linear/runner/workspace preflight를 검증한다.
- `console_schema`: 인터랙티브 콘솔의 run id, tmux 경로, lifecycle JSONL 스키마와 비밀 redaction을 정의한다.
- `daemon`: 신호 처리와 폴링 루프를 관리하고, 병렬 worker별 state/Linear/runner 컴포넌트를 실행한다.
- `issue_schema`: Linear 이슈 본문을 Markdown 섹션으로 파싱해 Task/Acceptance Criteria/Validation 필수 스키마를 검사한다.
- `linear_client`: stdlib GraphQL 클라이언트로 Todo 이슈 조회, workflow state 해석, 댓글 생성, 상태 전이를 수행한다.
- `logging`: JSON 또는 text root logger 설정과 추가 필드 직렬화를 담당한다.
- `runner`: 이슈 처리 오케스트레이션, shell runner 실행, 결과 JSON 검증, 댓글/상태 최종화를 담당한다.
- `state`: SQLite claim/lease, 실행 중복 방지, 댓글 idempotency, pending finalization, backoff 상태를 저장한다.

## 이슈 처리 흐름
1. 폴링: `daemon._poll_loop`가 stale lease를 정리한 뒤 `runner.process_next_issue_once`를 반복 호출한다.
2. 복구 우선: 새 Todo 조회 전에 `state`의 pending finalization과 active work를 재개해 재실행 없이 마무리한다.
3. Todo 조회: `linear_client.iter_todo_issues`가 설정된 team/project/assignee/queue state 기준으로 후보를 가져온다.
4. 스키마 게이트: `issue_schema` 검증 실패 이슈는 clarification 댓글만 남기고 실행 후보에서 제외한다.
5. 클레임: `state.acquire_claim`이 issue id와 execution key로 SQLite lease를 잡아 병렬 worker 중복 실행을 막는다.
6. 실행 준비: workflow state를 해석하고 이슈를 In Progress로 전이한 뒤 active work와 시작 댓글을 기록한다.
7. 실행: `ShellRunner`가 prompt/context/artifact/result 경로를 만들고 command template을 no-shell로 실행하며 lease를 갱신한다.
8. Done 게이트: transport exit code 0, `result.json` status `completed`, 검증 evidence 존재, 모든 검증 exit code 0일 때만 성공이다.
9. 최종화: 성공이면 결과 댓글을 idempotent하게 남기고 Linear Done 상태로 전이한 뒤 claim을 completed로 닫는다.
10. 실패/보류: runner 실패, 불충분한 result, 댓글/Done 오류는 실패 또는 pending 상태로 남겨 Done 전이를 막고 재시도 가능하게 한다.
