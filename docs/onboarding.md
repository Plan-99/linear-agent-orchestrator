# 팀 온보딩 가이드

Linear 이슈를 에이전트(Codex/OMX/Claude Code)에게 자동으로 전달하고, 검증된
결과만 Done으로 처리하는 orchestrator를 팀원 각자가 자기 머신에서 운영하기
위한 가이드입니다.

핵심 원칙 세 가지만 기억하세요.

1. **각자 자기 daemon과 명시적 assignee** — 개발자마다 자기 머신에서 daemon을
   돌리고, OAuth app actor와 별개로 지정한 사람에게 할당된 이슈만 처리됩니다.
2. **대상 repo마다 Linear 프로젝트 1개** — daemon 하나는 repo 하나만
   마운트하고 프로젝트 하나만 폴링합니다. 섞으면 다른 repo용 이슈가 엉뚱한
   repo에서 실행됩니다.
3. **에이전트는 제안만, 결정은 사람이** — 자동 push/PR/merge는 없습니다.
   성공한 작업은 로컬 `lao/<이슈>-<key>` 브랜치에만 커밋되고, 검토·머지는
   사람이 합니다.

## 1. 사전 준비

- Git, Docker와 Docker Compose
- `flock` 명령 (`util-linux` 패키지) — 동시에 두 launcher가 업데이트하거나 daemon을
  시작하지 못하게 하는 host lock에 사용합니다.
- host에서 사용할 backend CLI에 **먼저 로그인**해 두세요
  (`codex login`, 또는 Claude Code `claude` 로그인). bootstrap이 이
  인증 파일을 전용 사본으로 복사해 갑니다.
- 권장 인증은 Linear OAuth2 Client Credentials입니다. Linear 관리자가 Settings →
  API → OAuth Applications에서 private 앱 `LAO Agent Orchestrator`를 만들고
  [**Client Credentials tokens**](https://linear.app/developers/oauth-2-0-authentication#client-credentials-tokens)를
  활성화합니다. scope는 LAO가 `read,write`로
  고정해 요청하며, 앱 상세 페이지의 team access에 daemon이 사용할 팀을 포함합니다.
- 관리자는 client ID와 client secret을 승인된 secret manager로 운영자에게
  전달합니다. 이 값은 채팅, 이슈, 로그 또는 Git에 올리지 않습니다.
- OAuth 앱을 아직 준비할 수 없다면 [Linear API 설정](https://linear.app/settings/api)에서
  자신의 Personal API key를 발급해 legacy fallback으로 사용할 수 있습니다.

## 2. 설치

```bash
git clone https://github.com/Plan-99/linear-agent-orchestrator.git
cd linear-agent-orchestrator
./run.sh
```

`run.sh`가 팀원이 평소 사용하는 유일한 시작 명령입니다. 최초 실행은 `.env`를
예시에서 만들고 필수 설정이 비어 있으면 daemon을 시작하지 않습니다. 3번에 따라
`.env`를 채운 뒤 `./run.sh`를 다시 실행하세요.

내부 bootstrap 단계는 `.runtime/` 디렉토리들과 `.env`(예시에서 복사), agent
credential 사본(`.runtime/agent-home`)을 준비합니다. host 원본이 사본보다
mtime 기준으로 더 최신이면 실행할 때마다 자동으로 재복사되므로, host에서
재로그인한 뒤에는 `./run.sh`만 다시 돌리면 됩니다. 단, 컨테이너 쪽 CLI가 사본을
덮어써 사본 mtime이 host보다 최신이 된 경우(예: 이미 회전된 token으로 갱신
실패, GitHub #3)에는 자동 갱신이 동작하지 않으니
`./run.sh --refresh-credentials`로 강제 재복사하세요.

시작할 때 `run.sh`는 현재 branch의 설정된 upstream만 fast-forward 방식으로
갱신하고, 새 launcher를 한 번 재실행합니다. 자동으로 받아 실행하는 코드이므로
운영 전에 `git remote -v`와 branch upstream이 조직에서 신뢰하는 저장소인지
확인하세요. branch를 바꾸거나 commit/push하지 않으며 reset, stash, rebase로 로컬
변경을 숨기지 않습니다.

공식 `scripts/compose.sh`와 기본 runtime 설정에서는 이 checkout의
`.runtime/data/state.sqlite3`가 authoritative 상태 DB입니다. 다른
`LAO_PROJECT_DIR`을 선택해도 DB, work, artifact는 orchestrator의 `.runtime/` 아래에
남습니다. 명시적 DB/runner 경로 override에는 target 내부 경로를 사용하지 마세요.
`LAO_RUNTIME_DIR`은 dev-container 경로 변환을 위한 내부 plumbing이므로 `.env`에서
설정하지 마세요.

## 3. `.env` 설정

권장 OAuth app actor 설정:

```dotenv
LINEAR_AUTH_MODE=oauth_client_credentials
LINEAR_API_KEY=                  # OAuth와 동시에 설정하지 않음
LINEAR_OAUTH_CLIENT_ID=<client-id>
LINEAR_OAUTH_CLIENT_SECRET=<client-secret>
LINEAR_TEAM_ID=DEV                # 팀 key 또는 UUID
LINEAR_PROJECT_NAME=<프로젝트명>   # 이 daemon이 폴링할 프로젝트
# 앱 actor와 별개인 실제 사람 담당자의 Linear user UUID — OAuth에서 필수
LINEAR_ASSIGNEE_ID=<human-user-id>

# 에이전트가 실제로 수정할 신뢰된 Git checkout의 절대 host 경로
LAO_PROJECT_DIR=/absolute/path/to/target-repo

# 신뢰된 이슈가 대상 Docker container를 직접 개발해야 할 때만 true
# (host root와 동등한 권한)
LAO_DOCKER_HOST=false

AGENT_BACKEND=omx                 # omx | codex | claude
AGENT_MODEL=gpt-5.6-sol
AGENT_EFFORT=high
```

전체 항목과 기본값은 [.env.example](../.env.example)을 참고하세요.
`LAO_DOCKER_HOST=true`이면 agent가 host Docker daemon으로 sibling project
container를 build/run할 수 있습니다. launcher는 base repo와 isolated worktree를
host-identical path로 제공하므로 상대 Compose bind mount도 동작합니다. 이 모드는
LAO credential과 host 전체에 접근 가능한 full-host trust mode이므로 신뢰된
이슈에서만 사용하세요. GUI는 sibling container가 `DISPLAY`/`XAUTHORITY`와
이 모드에서 worktree에 쓰는 sibling container는 host numeric UID:GID로 실행하고, 종료 전에
반드시 정리하세요. root-owned 파일이나 남은 bind mount는 LAO의 commit/worktree cleanup을
막을 수 있습니다.
`/tmp/.X11-unix`를 받아 host 창을 여는 구조입니다. 설정 예시는
[README의 대상 Docker 컨테이너 개발과 GUI](../README.md#대상-docker-컨테이너-개발과-gui)를
참고하세요.

`LINEAR_AUTH_MODE`를 생략하면 기존 호환을 위해 `api_key`로 동작합니다. 한 daemon에는
credential set을 정확히 하나만 설정하세요. OAuth client ID/secret과
`LINEAR_API_KEY`를 동시에 채우면 actor 혼동을 막기 위해 startup이 실패합니다.
OAuth 모드에서는 app viewer를 사람 담당자로 오인하지 않도록
`LINEAR_ASSIGNEE_ID`를 비울 수 없습니다.

OAuth app을 준비할 수 없거나 긴급 롤백하는 경우에만 개인 API key 모드를 사용합니다.

```dotenv
LINEAR_AUTH_MODE=api_key
LINEAR_API_KEY=lin_api_...
LINEAR_OAUTH_CLIENT_ID=
LINEAR_OAUTH_CLIENT_SECRET=
# 비우면 API key 소유자를 사용하며, 필요하면 사람 UUID를 명시합니다.
# LINEAR_ASSIGNEE_ID=
```

Client Credentials access token은 30일 유효하고 refresh token이 없습니다. LAO는 token을
파일이나 DB에 쓰지 않고 메모리에만 보관하며 만료 5분 전에 다시 발급합니다. GraphQL
요청이 `401`이면 cached token을 폐기하고 한 번만 재발급·재시도합니다. client secret을
회전하면 기존 Client Credentials token도 무효화됩니다. `.env`를 갱신하고 daemon을
recreate해 새 secret으로 token을 다시 발급하세요.
client secret과 access token은 `RUNNER_ALLOWED_ENV`에 절대 추가하지 않습니다.

OAuth secret을 입력한 뒤에는 파일 권한과 Git ignore 상태를 확인합니다.

```bash
chmod 600 .env
git check-ignore .env
```

## 4. 대상 repo 준비

`LAO_PROJECT_DIR`이 가리킬 저장소의 조건:

- LAO의 `.runtime/data`, `.runtime/work`, `.runtime/artifacts`를 대상 repo 안으로
  만들거나 옮기지 마세요. 대상 repo는 agent가 수정할 작업 checkout일 뿐입니다.
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

## 5. 자동 업데이트와 검증

일상적인 검증과 daemon 실행은 하나의 명령으로 처리합니다.

```bash
./run.sh
```

`run.sh`는 `.env`의 활성 backend에 필요한 공식 npm stable 버전을 확인합니다.
`codex`는 Codex, `claude`는 Claude Code, `omx`는 OMX와 Codex를 함께 갱신합니다.
새 소스나 버전은 별도의 candidate 이미지에서 CLI 버전/help, 전체 pytest, 실제
`.env`의 read-only startup check를 통과해야만 last-known-good 이미지로
승격됩니다. 검증된 source SHA, image ID와 세 도구 버전은
`.runtime/update/verified.env`에 기록되며, 실패한 candidate는 이 상태를 덮어쓰지
않습니다.

이미지 태그와 Compose project는 physical checkout 경로별 namespace를 사용하므로
같은 host의 다른 LAO checkout과 충돌하지 않습니다. 승격 직전 Git SHA/clean
worktree를 다시 확인하며, 중단된 승격은 previous 이미지에서 복구합니다.

fallback은 현재 checkout의 Compose launch contract로 검증됩니다. Compose나
entrypoint의 breaking change는 기존 verified 이미지와 하위 호환되게 전환해야 하며,
호환성 또는 startup 검사가 실패하면 daemon을 시작하지 않습니다.
다음 수동 명령은 개발과 장애 분석용입니다.

```bash
./scripts/test.sh                                          # 전체 테스트
./scripts/compose.sh run --rm orchestrator startup-check   # 설정/인증/repo 점검
```

`startup-check`가 실패하면 오류 목록이 그대로 출력됩니다. Linear를 변경하지
않는 읽기 전용 점검이므로 안심하고 반복 실행하세요.
공식 `scripts/compose.sh`는 container의 `STATE_DB_PATH`를 이 checkout의
`.runtime/data/state.sqlite3`에 연결합니다. Compose 밖의 raw `daemon`, `state-init`,
`process-once`는 host에 맞는 `STATE_DB_PATH` 또는 `--db-path`가 필요하며, 둘 다
없으면 exit code 2로 종료하고 `data/`를 만들지 않습니다.
`AGENT_INTERACTIVE=true`인데 tmux 밖에서 점검하면 "requires
AGENT_CONSOLE=true" 오류가 나는데, 이는 정상입니다(6번 참고) —
`-e AGENT_CONSOLE=true`를 붙여 점검하면 됩니다.

## 6. daemon 실행과 fallback

```bash
# foreground 터미널에서 실행하면 checkout별 host tmux에 자동 진입합니다
./run.sh
```

- 기본 `AGENT_CONSOLE=true`, `AGENT_CONSOLE_AUTO_TMUX=true`,
  `AGENT_INTERACTIVE=true` 조합에서는 별도로 `tmux new`를 실행할 필요가 없습니다.
- stdin/stdout이 TTY가 아닌 service·redirect 실행은 outer tmux를 자동 생성하지
  않습니다. 이 경우 `console-attach.sh`의 수동 attach 경로를 사용하거나 명시적으로
  headless 설정을 선택해야 합니다.
- Git working tree가 dirty이거나 upstream 없음/divergence/Git·npm 장애가 있으면
  업데이트를 건너뛰고 기존 검증 이미지로 실행합니다. 최초 실행이라 검증 이미지가
  없으면 중단합니다.
- `.env` 설정이나 인증 startup check가 실패하면 오래된 이미지로 우회하지 않고
  daemon 시작을 중단합니다.
- 실행 중인 orchestrator service의 daemon을 감지하면 이를 종료하거나 두 번째
  daemon을 시작하지 않고 상태만 알립니다.
- window 0 = daemon, 이슈를 집어갈 때마다 window 1+ 에 `lao:<run_id>`
  콘솔이 열립니다.
- `Ctrl-b d`는 daemon을 유지한 채 tmux에서 detach하고, 종료는 daemon window의
  `Ctrl-C`입니다 — 유휴 중엔 즉시, 실행 중인 run이 있으면 마무리 후
  종료됩니다.
- `run.sh`는 foreground를 유지합니다. terminal/tmux를 종료하면 container도
  SIGTERM을 받을 수 있습니다.
- daemon CLI 인자는 `./run.sh -- --max-iterations 1`처럼 `--` 뒤에 전달합니다.

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

Codex에서 어느 저장소를 작업 중이든 LAO 형식 이슈를 만들려면 checkout에 포함된
skill을 현재 Codex user-scope discovery 위치에 한 번 설치하세요.

```bash
cd /path/to/linear-agent-orchestrator
./scripts/install-linear-create-issue-skill.sh
```

새 Codex 세션에서 `$linear-create-issue`를 호출할 수 있습니다. skill은 live 생성 전
offline preview를 의무화하고, 명시적 생성 요청당 mutation을 한 번만 실행합니다.
기본 `.env` 또는 별도 credential 파일은 group/other가 읽을 수 없는 `0600` 권한이어야
합니다.

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

알림은 orchestrator가 직접 보내지 않고 **Linear 네이티브 알림과 Slack 연동**으로
처리합니다. 현재 운영 수용 기준은 다음과 같습니다.

- LAO 앱 actor가 이슈에 진행/결과 댓글을 남기고 상태를 변경합니다.
- 운영자가 Linear를 보고 있지 않을 때, 개인 전역 알림 설정에 따라 Linear 앱 또는
  Slack 알림이 도착합니다.
- 이 기본 경로는 `In Review` 전용이 아닙니다. 선택한 전역 알림 유형에 따라 다른 댓글,
  할당, 상태 변경도 함께 알릴 수 있습니다.
- `In Review` 전용 Custom View 알림과 특정 Slack 채널 게시는 선택 사항이며, 기본 운영
  완료 조건으로 간주하지 않습니다.

### 기본 운영 정책

1. **워크스페이스 연동 (관리자 1회)** — Linear Settings → Integrations →
   Slack을 연결합니다. 이미 연결되어 있다면 건너뜁니다.
2. **개인 전역 알림 (각 운영자)** — Linear 개인 설정 → Notifications에서 필요한
   댓글/상태 변경 알림과 Slack 전달을 켭니다. 실제 항목과 전달 범위는 워크스페이스 및
   개인 설정을 따릅니다.
3. **검증** — 새 Todo 테스트 이슈 하나를 LAO가 처리하게 한 뒤 다음을 확인합니다.
   - 이슈 Activity의 댓글과 상태 변경 actor가 `LAO Agent Orchestrator` 앱입니다.
   - 진행 댓글, 결과 댓글, `In Progress` 및 최종 상태 전환이 기록됩니다.
   - 운영자가 Linear를 보고 있지 않은 상태에서 Linear 앱 또는 Slack 알림이 하나 이상
     도착합니다.

이 검증에서 Linear 앱 알림이나 Linear 봇 개인 DM이 도착하면 **개인 전역 알림 경로**가
동작한 것입니다. 특정 Custom View에 연결한 Slack 채널 메시지의 성공 증거는 아닙니다.

OAuth app actor는 알림과 audit에서 사람의 동작과 LAO의 동작을 구분합니다. 개인 API
key로 LAO를 실행하면 상태 변경 actor가 key 소유자 자신으로 기록되어 Linear의
[self-action 알림 억제 정책](https://linear.app/docs/custom-views)이 적용될 수 있습니다.
Client Credentials token은 mutation actor를 LAO 앱으로 기록하므로 이 혼동을 피합니다.

### 선택 사항: In Review 전용 알림

전제: 실패/timeout run과 interactive run의 result 제출은 기본 설정으로 이미
In Review로 이동합니다. 성공(Done)까지 같은 리뷰 큐로 모으려면 `.env`에서
Done 역할도 같은 상태로 매핑하세요(게이트 검증은 그대로 유지됩니다).

```dotenv
LINEAR_ATTENTION_STATE_NAME="In Review"
LINEAR_DONE_STATE_NAME="In Review"
```

개인 DM을 좁혀 받으려면 `Status = In Review`와 본인 assignee 조건의 Custom View를
만들고, bell 메뉴에서 **이슈가 View에 추가될 때** 개인 notification을 구독합니다.
Linear 개인 설정 → Notifications → Slack 전달도 함께 켭니다.

특정 팀 Slack 채널로 보내려면 View의 `Configure custom view Slack notifications`에서
대상 workspace/channel과 `Issue added to view`를 별도로 선택합니다. 다음 항목을 모두
확인해야 채널 경로가 성공한 것입니다.

1. 설정 이후 생성한 새 이슈가 View 밖(`Todo`)에서 안(`In Review`)으로 전환됩니다.
2. 설정한 **Slack 채널에 실제 메시지**가 도착합니다.
3. Linear Inbox나 개인 Slack DM은 채널 메시지를 대신하는 성공 증거로 사용하지 않습니다.

채널 경로는 개인 구독과 독립적이므로, 도착하지 않으면 View의 이벤트 선택, Slack
workspace/channel, private channel 권한을 먼저 확인합니다. 공유 채널용 View는 평가
대상이 명확하도록 동적인 `Assignee = Me`보다 명시적인 assignee 조건을 권장합니다.
이 선택적 채널 검증이 실패해도 위의 개인 전역 알림 수용 기준이 충족되면 기본 운영은
가능합니다.

OAuth 장애 시 롤백 절차:

1. daemon을 중지합니다.
2. `.env`에서 `LINEAR_AUTH_MODE=api_key`로 바꾸고 OAuth client ID/secret을 비웁니다.
3. 자신의 `LINEAR_API_KEY`를 설정하고, 필요에 따라 `LINEAR_ASSIGNEE_ID`를 유지합니다.
4. `./scripts/compose.sh run --rm orchestrator startup-check`를 통과시킵니다.
5. `./run.sh`로 다시 시작합니다.

API key 모드에서는 actor가 다시 사람으로 바뀌므로 self-action 기반 View DM은 오지 않는
것이 정상입니다. 운영 복구와 Slack 알림 복구를 같은 것으로 간주하지 마세요.

## 10. 하지 말 것

| 금지 | 이유 |
|---|---|
| In Review 이슈를 Todo로 재큐잉 | Done된 이슈는 영구 종결이라 무시되고, 실패 이슈의 무편집 재큐잉은 fail-in-doubt만 남습니다. 후속 작업은 **새 이슈**로. |
| 에이전트에게 Linear credential 전달 (`LINEAR_API_KEY`, OAuth client secret/token을 `RUNNER_ALLOWED_ENV`에 추가) | 에이전트가 자기 이슈를 스스로 Done으로 만들 수 있게 되어 Done gate가 무력화됩니다. |
| 같은 assignee로 daemon 2개 동시 실행 | 머신 간 claim 조정이 없어 중복 실행됩니다. |
| orchestrator checkout의 `.runtime/data/state.sqlite3` 임의 삭제·이동 | authoritative 복구/중복 방지 기록이 사라지거나 여러 DB로 갈라집니다. 의도적 reset일 때만. |
| daemon을 tmux/session manager 밖에서 방치 실행 | 터미널이 닫히면 컨테이너도 SIGTERM을 받습니다. |
| **호스트에서** 대상 repo에 `git worktree prune` 실행 | run worktree의 컨테이너 경로(`/workspace/...`)가 호스트에서는 해석되지 않아, 살아있는 run의 worktree 등록까지 파괴될 수 있습니다. worktree 정리는 orchestrator가 컨테이너 안에서 자동으로 합니다. |

## 11. 자주 겪는 문제

- **이슈를 안 집어감** — 순서대로 확인: ① 프로젝트/팀이 `.env`와 일치?
  ② 자신에게 할당 + Todo 상태? ③ 3섹션이 전부 채워짐? (clarification
  댓글이 달렸는지 확인) ④ 이미 처리된 적 있는 이슈? (Done/failed 이슈는
  재사용 불가)
- **startup-check에서 backend 인증 실패** — 대부분 host token rotation으로
  `.runtime/agent-home` 사본이 무효화된 경우입니다(GitHub #3). host에서
  재로그인 후 `./run.sh --refresh-credentials`를 실행하세요.
  반복 재발한다면 근본 해법은 **컨테이너 전용 독립 grant**입니다: daemon을
  중지하고 사본(`rm .runtime/agent-home/.claude/.credentials.json`)을 지운
  뒤, 컨테이너 안에서 로그인을 1회 수행합니다
  (`./scripts/compose.sh run --rm --entrypoint claude orchestrator` 실행 후
  `/login`). 이후 host와 컨테이너가 서로 다른 grant를 쓰므로 refresh token
  rotation 충돌이 원천적으로 사라집니다.
- **결과 브랜치에 로그 파일이 섞임** — 대상 repo `.gitignore`에 agent
  런타임 산출물 등록(4번 참고).
- **실패한 run 조사** — Linear 실패 댓글 + `.runtime/work/worktrees/`에
  보존된 worktree + `.runtime/logs/console/<run_id>/output.log`를 보세요.

## 더 읽을 것

- [README](../README.md) — 전체 기능/설정 레퍼런스
- [collaboration-ready-worker.md](collaboration-ready-worker.md) — 안전·완료 계약
- [agent-console.md](agent-console.md) — tmux 콘솔 구조와 보안 경계
- [architecture.md](architecture.md) — 모듈 구조와 처리 흐름
