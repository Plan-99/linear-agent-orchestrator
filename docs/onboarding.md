# 팀 온보딩 가이드

Linear 이슈를 에이전트(Codex/OMX/Claude Code)에게 자동으로 전달하고, 검증된
결과만 Done으로 처리하는 orchestrator를 팀원 각자가 자기 머신에서 운영하기
위한 가이드입니다.

핵심 원칙 세 가지만 기억하세요.

1. **각자 자기 daemon** — 개발자마다 자신의 Linear API key로 자기 머신에서
   daemon을 돌리고, 자신에게 할당된 이슈만 처리됩니다.
2. **대상 repo마다 Linear 프로젝트 1개** — daemon 하나는 repo 하나만
   마운트하고 프로젝트 하나만 폴링합니다. 섞으면 다른 repo용 이슈가 엉뚱한
   repo에서 실행됩니다.
3. **에이전트는 제안만, 결정은 사람이** — 자동 push/PR/merge는 없습니다.
   성공한 작업은 로컬 `lao/<이슈>-<key>` 브랜치에만 커밋되고, 검토·머지는
   사람이 합니다.

## 1. 사전 준비

- Docker와 Docker Compose
- host에서 사용할 backend CLI에 **먼저 로그인**해 두세요
  (`codex login`, 또는 Claude Code `claude` 로그인). bootstrap이 이
  인증 파일을 전용 사본으로 복사해 갑니다.
- [Linear 설정](https://linear.app/settings/api)에서 **자신의 Personal API
  key**를 발급받으세요. 팀 공용 key를 쓰면 안 됩니다(누가 어떤 이슈를
  집어가는지 assignee로 구분되기 때문).

## 2. 설치

```bash
git clone https://github.com/Plan-99/linear-agent-orchestrator.git
cd linear-agent-orchestrator
./scripts/bootstrap.sh
```

`bootstrap.sh`는 `.runtime/` 디렉토리들과 `.env`(예시에서 복사), agent
credential 사본(`.runtime/agent-home`)을 준비합니다. host에서 인증을
갱신했다면 daemon을 멈추고 해당 사본을 지운 뒤 bootstrap을 다시 실행해야
합니다(사본은 자동 갱신되지 않습니다).

## 3. `.env` 설정

최소 설정:

```dotenv
LINEAR_API_KEY=lin_api_...        # 자신의 key
LINEAR_TEAM_ID=DEV                # 팀 key 또는 UUID
LINEAR_PROJECT_NAME=<프로젝트명>   # 이 daemon이 폴링할 프로젝트

# 비워 두면 API key 소유자(자기 자신)에게 할당된 이슈만 처리합니다.
# LINEAR_ASSIGNEE_ID=

# 에이전트가 실제로 수정할 신뢰된 Git checkout의 절대 host 경로
LAO_PROJECT_DIR=/absolute/path/to/target-repo

AGENT_BACKEND=omx                 # omx | codex | claude
AGENT_MODEL=gpt-5.6-sol
AGENT_EFFORT=high
```

전체 항목과 기본값은 [.env.example](../.env.example)을 참고하세요.

## 4. 대상 repo 준비

`LAO_PROJECT_DIR`이 가리킬 저장소의 조건:

- **writable한 git 저장소**이고 **커밋이 최소 1개** 있어야 합니다
  (run마다 HEAD에서 worktree를 분기하므로 빈 repo는 불가).
- **`.gitignore`에 agent 런타임 산출물을 등록**하세요. 성공 커밋이
  `git add -A`로 만들어지므로, 없으면 `.omx/` 로그와 `__pycache__/` 같은
  쓰레기가 결과 브랜치에 섞입니다.

  ```gitignore
  .omx/
  __pycache__/
  *.pyc
  ```

- (권장) repo 루트에 **`AGENTS.md`** 를 두고 상시 지침을 적으세요 —
  코드 스타일, 테스트 규칙, 그리고 후속 작업 발견 시
  `LAO_RESULT_PATH`와 같은 디렉토리에 `followup-issue.md`(3섹션 형식)로
  초안만 남기고 Linear에 직접 접근하지 말라는 규칙.
- Linear에 이 repo 전용 프로젝트를 만들고 `.env`의
  `LINEAR_PROJECT_NAME`을 맞추세요.

## 5. 검증

```bash
./scripts/test.sh                                          # 전체 테스트
./scripts/compose.sh run --rm orchestrator startup-check   # 설정/인증/repo 점검
```

`startup-check`가 실패하면 오류 목록이 그대로 출력됩니다. Linear를 변경하지
않는 읽기 전용 점검이므로 안심하고 반복 실행하세요.
`AGENT_INTERACTIVE=true`인데 tmux 밖에서 점검하면 "requires
AGENT_CONSOLE=true" 오류가 나는데, 이는 정상입니다(6번 참고) —
`-e AGENT_CONSOLE=true`를 붙여 점검하면 됩니다.

## 6. daemon 실행

```bash
# host tmux 안에서 실행하세요
tmux new -s lao
./scripts/daemon.sh
```

- **`AGENT_INTERACTIVE=true`라면 tmux 안 실행이 필수**입니다. tmux 밖에서는
  콘솔이 headless로 떨어져 기동이 거부됩니다.
- window 0 = daemon, 이슈를 집어갈 때마다 window 1+ 에 `lao:<run_id>`
  콘솔이 열립니다.
- 종료는 `Ctrl-C` — 유휴 중엔 즉시, 실행 중인 run이 있으면 마무리 후
  종료됩니다.

## 7. 이슈 작성

Linear에서 새 이슈를 만들 때 **"LAO agent task" 템플릿**을 사용하세요.
Task / Acceptance Criteria / Validation 3섹션이 미리 채워져 나옵니다.

- 세 섹션이 **모두 비어 있지 않아야** 실행됩니다. 누락되면 실행 없이
  clarification 댓글만 달리고 Todo에 남습니다.
- **자신에게 할당** + **Todo 상태**여야 자신의 daemon이 집어갑니다.
- Validation에는 에이전트가 실행할 수 있는 구체적 검증 명령을 적으세요
  (예: "저장소 루트에서 `python -m pytest -q` 실행, exit code 0").
- 본문의 코드 블록 안에 `#`로 시작하는 줄은 섹션 헤딩으로 오인될 수
  있으니 피하세요.

## 8. 처리 사이클

검증된 표준 사이클은 다음과 같습니다.

```
이슈 작성(템플릿, 본인 할당, Todo)
  → daemon이 claim, In Progress 전환, 콘솔 window 열림
  → 에이전트 작업 → result.json 제출 → 이슈가 In Review로 이동
  → (interactive 모드) TUI에서 대화·추가 요청 가능 — 세션은 열려 있음
  → 확인 끝나면 TUI exit → Done gate 통과 시 Done + lao/<이슈>-<key> 브랜치 커밋
  → 사람이 브랜치 diff 검토 → 대상 repo의 main에 머지(또는 폐기) → 브랜치 삭제
  → 후속 작업은 새 이슈로
```

리뷰 시 체크포인트:

- `git diff main...lao/<이슈>-<key>`로 변경 범위 확인 — 작업 파일 외의
  쓰레기가 섞였다면 통째 머지 대신 필요한 파일만 선별 반영하세요.
- Done gate는 에이전트의 **자기보고** validation을 신뢰합니다. 중요한
  변경이라면 검증 명령을 직접 재실행해 보세요.
- **머지를 건너뛰고 후속 이슈를 올리면 새 run은 이전 작업이 없는 HEAD에서
  시작합니다.** 이어지는 작업이라면 반드시 먼저 머지하세요.

## 9. Slack 알림 설정 (권장)

리뷰가 필요한 이슈(In Review 진입)만 자기 Slack DM으로 받는 구성입니다.
알림은 orchestrator가 아니라 **Linear 네이티브 Slack 연동**으로 처리합니다.

전제: 실패/timeout run과 interactive run의 result 제출은 기본 설정으로 이미
In Review로 이동합니다. 성공(Done)까지 같은 리뷰 큐로 모으려면 `.env`에서
Done 역할도 같은 상태로 매핑하세요(게이트 검증은 그대로 유지됩니다).

```dotenv
LINEAR_ATTENTION_STATE_NAME="In Review"
LINEAR_DONE_STATE_NAME="In Review"
```

설정은 두 계층입니다.

1. **워크스페이스 연동 (관리자 1회)** — Linear Settings → Integrations →
   Slack 연결. 이미 되어 있다면 건너뜁니다.
2. **개인 설정 (각자 1회, 약 1분)**:
   - Linear 개인 설정에서 **자기 Slack 계정 연결** (Notifications 또는
     Connected accounts; 뷰 구독 중 authorize 프롬프트가 뜨기도 합니다).
     DM 대상은 자기 계정을 연결한 사람에게만 보입니다.
   - `Status = In Review` + `Assignee = 자기 자신` 필터로 **자기 전용 커스텀
     뷰**를 만듭니다. 뷰 구독은 뷰의 고정 필터 기준으로 발송되므로 뷰 하나를
     팀이 공유할 수 없고, 사람마다 자기 뷰가 필요합니다.
   - 뷰 메뉴(⋯) → Slack 알림 구독 → **자기 개인 DM** 선택.

개인 알림 채널(개인 설정 → Notifications → Slack)을 켜는 방법도 있지만,
In Review만 골라 받을 수 없어 daemon이 바꾸는 모든 상태 전환마다 DM이
옵니다. 뷰 구독 방식을 권장합니다.

## 10. 하지 말 것

| 금지 | 이유 |
|---|---|
| In Review 이슈를 Todo로 재큐잉 | Done된 이슈는 영구 종결이라 무시되고, 실패 이슈의 무편집 재큐잉은 fail-in-doubt만 남습니다. 후속 작업은 **새 이슈**로. |
| 에이전트에게 `LINEAR_API_KEY` 전달 (`RUNNER_ALLOWED_ENV`에 추가) | 에이전트가 자기 이슈를 스스로 Done으로 만들 수 있게 되어 Done gate가 무력화됩니다. |
| 같은 assignee로 daemon 2개 동시 실행 | 머신 간 claim 조정이 없어 중복 실행됩니다. |
| `.runtime/data`의 SQLite 임의 삭제 | 복구/중복 방지 기록이 사라집니다. 의도적 reset일 때만. |
| daemon을 tmux/session manager 밖에서 방치 실행 | 터미널이 닫히면 컨테이너도 SIGTERM을 받습니다. |

## 11. 자주 겪는 문제

- **이슈를 안 집어감** — 순서대로 확인: ① 프로젝트/팀이 `.env`와 일치?
  ② 자신에게 할당 + Todo 상태? ③ 3섹션이 전부 채워짐? (clarification
  댓글이 달렸는지 확인) ④ 이미 처리된 적 있는 이슈? (Done/failed 이슈는
  재사용 불가)
- **startup-check에서 backend 인증 실패** — host에서 재로그인 후
  `.runtime/agent-home`의 해당 사본을 지우고 `./scripts/bootstrap.sh`
  재실행.
- **결과 브랜치에 로그 파일이 섞임** — 대상 repo `.gitignore`에 agent
  런타임 산출물 등록(4번 참고).
- **실패한 run 조사** — Linear 실패 댓글 + `.runtime/work/worktrees/`에
  보존된 worktree + `.runtime/logs/console/<run_id>/output.log`를 보세요.

## 더 읽을 것

- [README](../README.md) — 전체 기능/설정 레퍼런스
- [collaboration-ready-worker.md](collaboration-ready-worker.md) — 안전·완료 계약
- [agent-console.md](agent-console.md) — tmux 콘솔 구조와 보안 경계
- [architecture.md](architecture.md) — 모듈 구조와 처리 흐름
