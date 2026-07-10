# Linear Agent Orchestrator

Linear의 지정 프로젝트를 polling하고, **지정된 담당자에게 할당된 Todo 이슈만**
Codex, OMX, 또는 Claude Code에 전달하는 단일-worker orchestrator입니다.

- webhook과 Linear native agent session을 사용하지 않습니다.
- 한 daemon은 하나의 고정된 Git 저장소만 `/workspace/project`에 마운트합니다.
- 이슈 본문은 작업 저장소나 host 경로를 바꿀 수 없습니다.
- agent process가 종료 코드 0을 반환한 것만으로는 완료로 간주하지 않습니다.
- agent가 구조화된 검증 결과를 제출한 경우에만 Linear 이슈를 Done으로 바꿉니다.
- push, PR 생성/merge, 이슈 삭제는 자동화하지 않습니다.

상세 안전 계약은 [collaboration-ready worker contract](docs/collaboration-ready-worker.md)를
참고하세요.

## 권장 실행 구조

Docker/Compose가 외부 보안 경계입니다. 컨테이너 내부 agent는 기본적으로 권한 확인과
sandbox를 우회할 수 있지만, 컨테이너에는 다음 제한이 적용됩니다.

- host 사용자 UID/GID로 non-root 실행
- Linux capability 전체 제거 및 `no-new-privileges`
- 정확히 하나의 대상 저장소만 read-write mount
- Docker socket 미마운트
- host의 live Codex/Claude home 대신 `.runtime/agent-home`의 전용 credential copy 사용

agent는 orchestrator와 같은 컨테이너에서 실행되므로 이 컨테이너 안에서 신뢰할 수 없는
명령을 실행해서는 안 됩니다. 현재 네트워크 egress 제한과 agent process의 별도 격리는
구현 범위 밖입니다.

## 빠른 시작

### 1. 프로젝트 로컬 설정 생성

```bash
cd linear-agent-orchestrator
./scripts/bootstrap.sh
```

이 명령은 외부 env 파일 없이 현재 폴더 아래에 다음을 준비합니다.

- `.env`: ignored secret/config 파일
- `.runtime/data`: SQLite crash-recovery/idempotency 상태
- `.runtime/work`: 이슈별 입력 및 실행 파일
- `.runtime/artifacts`: 구조화 결과와 artifact
- `.runtime/agent-home`: agent credential/config 전용 copy

SQLite는 작업 queue를 대신하지 않습니다. 작업 대상은 항상 Linear의 현재
`project + Todo + assignee` 상태로 결정되며, SQLite는 동일 실행의 복구와 중복 방지만
담당합니다.

### 2. `.env` 설정

최소 설정 예시는 다음과 같습니다.

```dotenv
LINEAR_API_KEY=lin_api_...
LINEAR_TEAM_ID=...
LINEAR_PROJECT_NAME=TEST

# 비워 두면 LINEAR_API_KEY 소유자(API viewer)를 사용합니다.
# LINEAR_ASSIGNEE_ID=...

# agent가 실제로 수정할 신뢰된 Git checkout의 절대 host 경로입니다.
LAO_PROJECT_DIR=/absolute/host/path/to/target-repository

RUNNER_COMMAND_TEMPLATE='/usr/local/bin/lao-agent-runner {prompt_path} {result_path}'
AGENT_BACKEND=omx
AGENT_MODEL=gpt-5.6-sol
# Reasoning effort (비우면 backend 기본값).
# omx/codex: minimal|low|medium|high|xhigh, claude: low|medium|high|xhigh|max
AGENT_EFFORT=high
AGENT_BYPASS_PERMISSIONS=true
```

`LAO_PROJECT_DIR`을 생략하면 orchestrator 폴더 자체가 대상이 됩니다. 대상 경로는
**writable한 Git 저장소**여야 합니다.

기본값(`AGENT_WORKTREE_ISOLATION=true`)에서는 run마다 HEAD에서 분기한 전용 git
worktree에서 작업하고, 의미적으로 성공한 run만 로컬 `lao/<issue>-<key>` 브랜치에
커밋됩니다(push 없음). base 저장소는 항상 clean하게 유지되며, 사람이 브랜치를
리뷰해 머지하거나 버리면 됩니다. 실패한 run의 변경은 `.runtime/work/worktrees/`에
조사용으로 남습니다. `false`로 끄면 기존 고정-디렉토리 모드로 돌아가며, 이때는
run 전 clean worktree가 강제되고 남은 변경이 다음 실행을 fail-closed로 차단합니다.

협업 시 각 개발자는 자신의 API key를 사용하고 `LINEAR_ASSIGNEE_ID`를 비워 두거나 자신의
Linear user ID로 설정하면 됩니다. 미할당 이슈와 다른 사람에게 할당된 이슈는 가져오지
않습니다. 단, 같은 assignee/database를 공유하지 않는 daemon 두 개를 동시에 실행하는
구성은 지원하지 않습니다.

### 3. agent 인증 준비

`bootstrap.sh`는 실행 시점의 다음 파일이 존재하면 `.runtime/agent-home`으로 **한 번만**
복사합니다.

- `${CODEX_HOME:-$HOME/.codex}/auth.json`
- `${CODEX_HOME:-$HOME/.codex}/config.toml`
- `$HOME/.claude/.credentials.json`
- `$HOME/.claude.json`

먼저 host에서 사용할 CLI에 로그인한 뒤 bootstrap을 실행하세요. host 인증을 갱신한 경우
daemon을 중지하고 해당 전용 copy를 삭제한 다음 `./scripts/bootstrap.sh`를 다시 실행해야
합니다. live host credential 파일은 컨테이너에 직접 mount되지 않습니다.

### 4. 빌드와 사전 점검

```bash
./scripts/test.sh
./scripts/compose.sh run --rm orchestrator startup-check
```

`startup-check`는 Linear 설정/assignee, runner, 선택한 backend CLI와 인증, OMX setup,
고정 저장소 상태를 검사합니다. Linear에 이슈를 생성하거나 상태를 바꾸지는 않습니다.

secret 없이 로컬 wiring만 확인하려면 다음을 사용합니다.

```bash
./scripts/compose.sh run --rm orchestrator startup-check --allow-missing-secrets
```

### 5. daemon 실행

foreground 실행:

```bash
./scripts/daemon.sh
```

`Ctrl-C` 또는 SIGTERM을 받으면 유휴(polling 대기) 중에는 약 1초 안에 종료하고,
agent 실행 중에는 해당 run을 정리한 뒤 종료합니다(실행 중인 agent를 중간에 죽이지
않기 위한 의도된 동작입니다). 별도 terminal에서
실행 상태를 확인하려면 명령을 한 줄로 입력하세요.

```bash
docker ps --filter 'label=com.docker.compose.project=linear-agent-orchestrator' --format 'table {{.ID}}\t{{.Names}}\t{{.Status}}'
CONTAINER=$(docker ps -q --filter 'label=com.docker.compose.project=linear-agent-orchestrator' | head -1)
docker logs -f --tail 100 "$CONTAINER"
```

여러 줄 명령을 사용할 때 `\` 뒤에는 공백을 넣지 마세요. `scripts/daemon.sh`는 foreground
프로세스이므로 terminal/session manager가 종료되면 컨테이너도 SIGTERM을 받을 수 있습니다.

## backend와 모델 변경

`.env`의 다음 값만 바꾼 뒤 daemon을 다시 시작합니다.

```dotenv
# OMX
AGENT_BACKEND=omx
AGENT_MODEL=gpt-5.6-sol
AGENT_EFFORT=high          # minimal|low|medium|high|xhigh

# Codex CLI
# AGENT_BACKEND=codex
# AGENT_MODEL=gpt-5.6-sol
# AGENT_EFFORT=high        # minimal|low|medium|high|xhigh

# Claude Code
# AGENT_BACKEND=claude
# AGENT_MODEL=claude-fable-5
# AGENT_EFFORT=high        # low|medium|high|xhigh|max
```

내장 wrapper는 prompt를 stdin으로 전달하고 두 번째 인자에 orchestrator가 소유한
`result.json` 경로를 제공합니다. `/bin/echo {prompt_path}` 같은 command는 wiring 확인에는
쓸 수 있지만 실제 작업 완료를 증명하지 못하므로 이슈를 Done으로 만들 수 없습니다.

custom `RUNNER_COMMAND_TEMPLATE`도 가능하지만 다음 책임은 custom runner가 직접 져야 합니다.

- shell 문자열 실행이 아닌 안전한 argv 사용
- 지정된 `{result_path}`에 정확한 구조화 결과 작성
- 실행 직전 clean/writable Git worktree 검사
- 필요한 backend 인증 및 권한 경계 보장

내장 per-run workspace preflight는 실행 파일 이름이 `lao-agent-runner`인 표준 wrapper에
적용됩니다.

## 이슈 처리 및 완료 계약

daemon의 순서는 다음과 같습니다.

1. 설정된 project의 Todo 중 설정된 assignee에게 할당된 유효 이슈를 선택합니다.
2. 실행 직전에 고정 저장소를 다시 검사하고, 격리 모드에서는 HEAD에서 분기한 run 전용
   git worktree를 만듭니다(legacy 모드에서는 clean worktree를 요구).
3. 이슈를 In Progress로 바꾸고 agent에 이슈 본문과 실행 context를 전달합니다.
4. agent가 `{result_path}`에 결과를 기록합니다.
5. transport와 semantic validation이 모두 성공한 경우에만 이슈를 Done으로 바꾸고,
   변경 사항을 로컬 `lao/<issue>-<key>` 브랜치로 커밋합니다(push 없음).

완료 결과 형식:

```json
{
  "version": 1,
  "status": "completed",
  "summary": "요청한 변경과 검증을 완료했습니다.",
  "validation": [
    {
      "description": "테스트",
      "command": "pytest -q",
      "exit_code": 0,
      "evidence": "136 passed"
    }
  ],
  "artifacts": ["relative/report.md"],
  "files_changed": ["relative/file.py"]
}
```

결과 파일 누락/손상, `failed`, `blocked`, `needs_clarification`, 빈 validation, 0이 아닌
validation exit code, backend timeout/실패는 모두 Done 전환을 막습니다. 실패 원인은 Linear
댓글에 기록되며 secret은 기록하지 않습니다.

## 사람 개입 단일 리뷰 큐 운영

실패, timeout, fail-in-doubt 실행은 failure 댓글이 기록된 뒤
`LINEAR_ATTENTION_STATE_NAME` 상태(기본값 `In Review`)로 이동합니다. 상태 전환은
best-effort이므로 Linear 전환 API 오류가 로컬 실패 기록이나 failure 댓글을 되돌리지
않습니다. workspace preflight 실패와 schema-invalid 이슈는 자동 재시도/수정 대기 의미를
유지하기 위해 Todo에 남습니다.

성공과 실패를 모두 같은 사람 검토 큐에 모으려면 `.env`에서 Done 역할도 같은 상태로
매핑합니다. Done 역할과 성공 finalization 게이트 자체는 바뀌지 않습니다.

```dotenv
LINEAR_ATTENTION_STATE_NAME="In Review"
LINEAR_DONE_STATE_NAME="In Review"
```

알림은 orchestrator가 아니라 **Linear 네이티브 Slack 연동**으로 구성합니다. Linear의
Slack integration에서 대상 workspace/channel을 연결하고, 이 팀의 이슈가 `In Review`에
진입할 때 해당 채널로 알리도록 notification rule을 설정하세요.

재작업 시에는 `In Review` 이슈를 Todo로 되돌리지 않습니다. execution key 특성상 이슈나
runner 설정을 수정하지 않은 재큐잉은 안전 복구가 불가능한 fail-in-doubt만 남깁니다.
리뷰에서 발견된 후속 작업은 원래 이슈를 재큐잉하지 말고 **새 Linear 이슈**로 엽니다.

## 안전한 로컬 명령

```bash
# 전체 재현 테스트
./scripts/test.sh

# 이슈 markdown 규격 검사 — Linear 미접속
./scripts/compose.sh run --rm orchestrator validate-issue docs/linear/test-issue-template.md --json

# SQLite schema 초기화/마이그레이션 — Linear 미접속
./scripts/compose.sh run --rm orchestrator state-init --json

# bounded daemon wiring smoke — Linear secret 불필요, 무한 polling 없음
./scripts/compose.sh run --rm orchestrator daemon --allow-missing-secrets --max-iterations 0 --db-path /tmp/lao-smoke.sqlite3
```

다음 명령은 실제 Linear를 변경하므로 TEST 프로젝트에서 의도적으로만 사용하세요.

```bash
./scripts/compose.sh run --rm orchestrator create-test-issue --json
./scripts/compose.sh run --rm orchestrator process-once --json
```

## 대화형 agent 콘솔 (tmux)

기본값은 **`auto`**입니다(host tmux 안이면 활성화, 아니면 headless). `true`/`false`로
강제할 수 있습니다. 활성화되면 각 agent 실행이 컨테이너 안에서 tmux 세션으로 열리고,
운영자는 host에서 실시간으로 관찰(및 대화 모드에서는 입력)할 수 있습니다.

`./scripts/daemon.sh`를 host tmux 안에서 실행하면 새 이슈를 claim할 때마다
`lao:<run_id>` 이름의 **새 tmux window**가 자동으로 열립니다(window 0 = daemon,
window 1+ = agent run). 실행이 끝나면 해당 window도 종료되고, daemon은 window 0에서
계속 polling합니다.

```bash
# daemon과 run별 console window 자동 실행
./scripts/daemon.sh

# live/종료 run 조회 또는 수동 attach
./scripts/console-attach.sh --list
./scripts/console-attach.sh --run <32-char-run-id>                # read-only
./scripts/console-attach.sh --run <32-char-run-id> --interactive  # 스크롤/마우스
./scripts/console-attach.sh --run <32-char-run-id> --write        # capability=true만 허용
```

tmux 밖에서 `auto`는 기존 headless 실행으로 돌아갑니다. `AGENT_CONSOLE=true`로 강제하면
pane 대신 `.runtime/logs/console/supervisor.log`에 수동 attach 명령을 남깁니다.
`.env`의 `LAO_RUNTIME_DIR`/`AGENT_CONSOLE_HOST_LOG_DIR`도 host supervisor와 Compose
bind mount에 동일하게 적용됩니다.

- **window 0 = daemon, window 1+ = agent run** (기본
  `AGENT_CONSOLE_OPEN_TARGET=window`). run마다 `lao:<run_id>` 이름의 별도 window가
  열리므로 OMX team 모드가 자기 window 안에서 pane을 자유롭게 분할할 수 있습니다.
  새 run이 열리면 자동으로 그 window로 전환됩니다(`AGENT_CONSOLE_FOCUS=false`로
  현재 위치 유지 가능).
  `split`으로 바꾸면 daemon window 안 sibling pane 방식이 되며, 이때 레이아웃은
  `AGENT_CONSOLE_LAYOUT`(기본 `main-vertical`)이 관리합니다.
- supervisor가 여는 window/pane은 기본 **interactive attach**입니다: 마우스 스크롤
  (copy-mode), 클릭으로 inner pane 선택이 동작합니다. 수동 attach 기본값은 여전히
  read-only이며 `--interactive`로 동일한 UX를 얻습니다.
- **직접 입력(타이핑)은 명시적 설정 metadata**(`AGENT_CONSOLE_DIRECT_INPUT`, 기본
  false)로만 결정되며 backend 이름으로 추론하지 않습니다. interactive attach에서
  타이핑한 키는 PTY에 도달하지만 one-shot `omx`/`codex`/`claude`는 prompt를
  stdin으로 한 번만 받으므로 이를 읽지 않습니다. PTY를 실제로 읽는
  interactive/custom harness는 어떤 backend든 opt-in(true)할 수 있습니다.
- 실행별 lifecycle JSONL과 output 로그는 `AGENT_CONSOLE_LOG_DIR`
  (기본 `/workspace/logs/console`)에 기록되며, Compose가 이를 `.runtime/logs/console`
  에 bind-mount하여 host 소유로 durable하게 남깁니다.
- tmux 제어 socket은 컨테이너-private이며 bind-mount되지 않습니다. host는 자신의
  `docker exec`로만 접근합니다(컨테이너는 host tmux/Docker socket을 보지 않음).

이벤트 스키마 계약과 host/container tmux 보안 리뷰는
[docs/agent-console.md](docs/agent-console.md)를 참고하세요.

## 병렬 워커

`MAX_PARALLEL_RUNS`(기본 1)로 한 daemon 안의 동시 이슈 워커 수를 늘릴 수 있습니다.

- 워커마다 별도 Linear polling/SQLite 연결과 고유 claim owner를 사용하며, lease가
  같은 이슈의 중복 실행을 막습니다.
- `AGENT_WORKTREE_ISOLATION=true`가 필수입니다(워커별 전용 worktree).
- 워커 수만큼 polling API 호출이 늘어나므로 `POLL_INTERVAL_SECONDS`는 15 이상을
  권장합니다.
- tmux 콘솔 사용 시 run pane이 워커 수만큼 열립니다(레이아웃은
  `AGENT_CONSOLE_LAYOUT`).

## 대화형 실행 (AGENT_INTERACTIVE)

`AGENT_INTERACTIVE=true`면 one-shot `exec` 대신 backend의 **대화형 TUI**가 run 전용
tmux 세션에서 열리고, 이슈 prompt가 첫 메시지로 주입됩니다. 콘솔 pane에서 직접
타이핑해 에이전트와 대화할 수 있습니다(`supports_direct_input`이 자동으로 true).

- 완료 판정(기본): 에이전트가 `{result_path}`에 유효한 구조화 결과를 쓰면 이슈를
  `LINEAR_ATTENTION_STATE_NAME`(기본 `In Review`)으로 전환하지만 세션은 닫지
  않습니다. 전환은 run당 한 번만 best-effort로 시도되며, 실패해도 대화는 계속됩니다.
  operator가 TUI에서 exit하여 프로세스가 종료된 뒤 orchestrator가 `result.json`을
  다시 로드/검증하고, Done 게이트를 통과하면 Done으로 전환합니다.
  `RUNNER_TIMEOUT_SECONDS`는 대화형 모드에서
  **idle timeout**으로 동작합니다 — 콘솔 출력 활동이 이어지는 동안 마감이 계속
  연장되고, 활동 없이 해당 시간이 지나면 종료됩니다. 활동과 무관한 hard cap은
  `RUNNER_MAX_LIFETIME_SECONDS`(기본 21600초 = 6시간)입니다.
- 이전 자동 종료 동작이 필요하면 `AGENT_INTERACTIVE_AUTOCLOSE=true`를 설정하세요.
  이때는 유효한 구조화 결과가 감지되는 즉시 orchestrator가 TUI 세션을 정리합니다.
- **Done 게이트는 불변입니다** — 대화 여부와 무관하게 유효한 validation 증거가 있는
  `completed` 결과만 Done으로 전환됩니다.
- `AGENT_CONSOLE`이 필요하며, 사람이 지켜보는 세션이므로 `MAX_PARALLEL_RUNS=1`
  권장입니다.

## 운영 제한

- 기본 설정에서 한 daemon은 한 저장소에서 이슈를 직렬 처리합니다
  (`MAX_PARALLEL_RUNS`로 동일 저장소 내 병렬 처리 가능).
- 동일 assignee를 사용하는 여러 machine 사이의 distributed claim은 없습니다.
- 자동 push/PR/merge/이슈 삭제는 없습니다.
- Docker socket과 issue-controlled bind mount는 지원하지 않습니다.
- credential copy는 자동 refresh되지 않습니다.
- SQLite를 삭제하면 복구/idempotency 기록도 사라지므로 의도적인 reset 외에는 삭제하지
  마세요.

## 주요 모듈

- `config.py`: env/dotenv 설정, runtime/backend/repository preflight
- `linear_client.py`: GraphQL transport, project/state/assignee scoped polling
- `issue_schema.py`: Task / Acceptance Criteria / Validation 이슈 규격
- `state.py`: SQLite claim, lease, replay, idempotency 상태
- `runner.py`: prompt/context/result materialization과 semantic Done gate
- `daemon.py`: serial polling, backoff, signal 처리
- `scripts/agent-runner.sh`: OMX/Codex/Claude backend adapter
