# 아키텍처와 데이터 계약

## 엔진 선택

앱과 CLI의 기본 엔진은 ACE-Step 1.5 XL turbo다. 엔진 이름과 기본 주소·모델은 `engines.py` 한 곳에 있다.
`ace-step`은 `127.0.0.1:18001`의 `acestep-v15-xl-turbo`(LM `acestep-5Hz-lm-4B`), `minimax-music3`는
`127.0.0.1:18002`의 `mlx-community/MiniMax-Music3-bf16`이다. 엔진·주소·모델을 적지 않은 요청은 ACE 값을
쓰고, 서버가 올린 DiT·LM이 요청과 다르면 제출 전에 거부한다. LM이 곡을 계획하는 요청(`thinking`, text2music)은
서버가 요청한 LM을 올려 두었거나 LM을 내려 두었다고(`llm_initialized: false`) 밝힐 때만 제출한다. ACE는 Apple
Silicon에서 cover 뒤 LM을 내리고, 다음 계획 요청 때 요청의 LM을 다시 올리며 못 올리면 그 요청을 실패로 끝낸다.
`engine` 필드가 없는 저장된 요청은 ACE 요청이다. ACE 서버의 수명·구간 수정은 아래 '현재 구현 범위'에 있다.

## Music 3(이름으로 고를 때만)

Music 3는 `--engine minimax-music3`를 명시할 때만 쓴다. 모델과 실행 환경은 삭제했고
`scripts/bootstrap_music3.sh`로 다시 준비한다. 앱은 이 서버를 띄우지 않으므로 `scripts/start_music3_api.sh`를
직접 켠다. 요청은 `Music3Client` → 인증된 `127.0.0.1:18002` task API(`.runtime/music3-api-key`, 0600)로 가고,
서버의 엔진·모델·준비 상태가 요청과 같아야 제출한다.

- text2music만 지원한다. cover·repaint·파형 참조는 CLI가 거부한다. 상한은 300초이며 실제 길이를 기록한다.
- 제작 규칙과 songPlan은 모델 학습 형식의 caption(`Global Metadata / Vocal Details / Arrangement`의 13개 항목)으로
  옮긴다. 프리셋의 `music3Schema`와 규칙의 `music3Fields`를 요청에 동결하고, ACE 전용 LM·thinking·CFG 설정은
  보내지 않는다. 매핑은 `music3.py`에 있다.
- 묶음은 만든 엔진에서만 재개한다. Music 3 묶음은 `--engine minimax-music3`를 명시해야 재개되고, 곡 화면은
  이어 만들 수 없다고 표시한다. Music 3로 만든 음원의 재생·평가·선택·export는 그대로 쓴다.
- 취소는 로컬 CLI 작업을 멈추며 이미 제출한 추론은 서버가 끝까지 계산할 수 있다. timeout이나 서버 불통·HTTP
  거절 뒤에는 같은 묶음의 남은 seed를 제출하지 않고 따로 기록한다(`stopReason`, `unsubmittedSeeds`).

## 현재 구현 범위

```text
Electron renderer ─ 제한된 preload IPC ─ Electron main ─┬─ music-engine CLI (JSON 한 개 출력)
 (내 곡·새 곡·곡 화면·설정)                             ├─ ACE health 감시(켜고 끄지 않는다)
                                                        └─ music-artifact:// 토큰 스트리밍·파형
music-engine CLI ─ workflow.py ─ ProjectStore + qc.py
                 ├ execution.py (추론·다운로드·QC와 짧은 기록 트랜잭션)
                 ├ jobs.py      (최초 요청 스냅샷·재개 계보·중단 복구)
                 ├ engines.py   (엔진 이름과 기본 주소·모델)
                 ├ ace_runtime.py (일이 올 때 ACE를 켜고, 차선이 비면 ACE가 스스로 꺼진다)
                 ├ views.py      (status·library: 앱과 에이전트가 읽는 뷰)
                 ├ assistant.py  (피드백 → 수정안: 규칙 또는 loopback LLM)
                 └ drafting.py   (설명 → 제목·스타일·가사 초안)
                        ↓
               AceStepClient → ACE-Step REST API (loopback) → v0.1.8 native MLX

music-engine serve ─ service/ (127.0.0.1:47833, arkAgent 엔진 계약 v0, 러너 스레드 하나)
```

`workflow.py`가 응용 계층이다. Electron main은 IPC를 CLI 명령에 연결할 뿐 생성·검사·해석
규칙을 다시 구현하지 않는다. 같은 CLI를 사람이나 코딩 에이전트가 터미널에서 그대로 쓸 수 있다.

renderer는 실제 경로를 받지 않는다. 곡은 main이 목록으로 보여 준 폴더에서 만든 `songId`로,
버전은 `candidate_…` id로 가리키고, main이 id를 자기 표에서 경로로 바꾼다. Finder 열기도
main이 그 표의 경로에만 `shell.showItemInFolder`를 쓴다. 음원은 경로마다 고정된 무작위
token URL로만 전달되고 Range 요청을 스트림으로 응답한다.

곡을 변경하는 IPC는 `songId`를 필수로 받는다. main의 `SongSession`과 renderer의 요청 기록은
곡 열기 순서와 요청 당시 문맥을 고정한다. 오래된 열기 응답·진행 조회·수정안은 새 곡 화면에
적용하지 않으며, 비동기 준비 후 다른 곡이 열렸으면 새 생성과 내보내기를 시작하지 않는다.
초안은 필드별 직접 수정 이력을 확인해 도우미를 기다리며 쓴 내용을 보존한다.

ACE는 일이 있을 때만 켜져 있다(`ace_runtime.py`). 생성·커버·구간 수정·재개와 `draft --engine ace-step`은
CLI의 `ManagedAceClient`로 ACE를 부른다. 첫 요청 때 주소에 응답하는 서버가 없으면 `scripts/start_ace_api.sh`를
새 세션으로 띄우고(`MUSIC_ENGINE_ACE_STOP_WHEN_IDLE=1`, 기록은 `.runtime/logs/ace-server.log`) 모델이 올라올 때까지
기다린다. 이렇게 켠 서버가 요청한 LM을 올리지 못했으면 기록의 LM 줄을 붙여 바로 실패한다. 이 요청은 생성
차선을 쥔 채 일어난다. 그 서버의 런처는 1초마다 차선을 보고, 두 번 연속 비어 있으면
`ace-start.lock`을 잡고 다시 확인한 뒤 SIGTERM으로 스스로 끈다(모델을 올리는 중이면 20초 뒤 강제 종료). 시작도
같은 잠금 안에서 하므로 꺼지는 서버를 다시 쓰거나 서버 둘을 동시에 올리지 않는다. 생성이 강제 종료돼도 차선이
풀리므로 서버가 남지 않는다. 손으로 켠 서버(그 환경 변수가 없는 서버)는 그대로 쓰고 끄지 않는다.

앱은 ACE를 켜거나 끄지 않고 health만 본다(쉬는 중·켜는 중·켜짐·확인 필요·설치 필요). 생성 버튼은 엔진이 쉬는
중에도 누를 수 있고, 첫 요청이 모델을 기다리는 동안 작업 막대는 "음악 엔진을 켜는 중"이다. 앱 종료(⌘Q, SIGINT,
SIGTERM) 때 CLI가 취소 상태를 저장할 시간을 준다. 생성 작업은 앱에서
한 번에 하나이며 시작 요청도 직렬화한다. 평가와 메모는 생성 중에도 즉시 `project.json`에
저장한다. 설정은 앱 데이터 폴더의 `settings.json`에, 마지막으로 연 곡은 `state.json`에
저장한다. 두 파일의 변경은 각각 직렬화하며 고유 임시 파일·fsync·원자 교체를 사용한다.

생성 시작과 AI 도우미 호출은 같은 메모리 잠금으로 직렬화한다(`handoff.ts`). 생성은 Ollama 모델 해제를
성공 응답과 잔류 모델 목록으로 확인한 뒤에만 CLI를 띄운다. 도우미는 음악 엔진이 응답하지 않을 때만 부르며,
방금 끝난 생성의 서버가 꺼지기를 10초까지 기다리고 그래도 떠 있으면(손으로 켠 서버) 부르지 않는다.

관리 서버는 비공개 인증 키를 `.runtime/ace-api-key`에 원자적으로 게시한다(권한 0600).
CLI·Electron main·서버가 같은 키를 읽고 renderer에는 전달하지 않는다. upstream ASGI 앱을
`MusicApiGuard`로 감싸 인증된 `/health`, `/release_task`, `/query_result`, `/v1/create_sample`,
`/v1/audio`만 제공한다. 브라우저 Origin 요청과 나머지 API는 upstream 처리 전에 거부한다.
ACE의 설치된 checkout은 수정하지 않으며 기존 외부 서버는 해당 서버의 정책을 유지한다.

## 프로젝트 정본

각 작업 프로젝트는 `project.json`과 하위 파일을 가진다. 스키마 버전은 현재 `1`이다.

| 기록 | 현재 필드와 계약 |
| --- | --- |
| Project | 원문/정규화 가사, 스타일, 목표 길이, 구조, 선택 후보 |
| GenerationRequest | ACE adapter 판본, 서버가 보고한 모델, 전체 파라미터, canonical fingerprint |
| Artifact | 프로젝트 상대 경로, SHA-256, byte 수, 실제 frame/sample rate/channel/길이 |
| Candidate | 요청·artifact·finding 참조, 사람 검수, 부모 후보, 선택/문맥 범위 |
| Finding | 검사명, severity, 관측값과 임계값, 신뢰도, 실제 시간 범위 |
| Job | 상태·단계·진행·오류·원격 task ID·시작/종료 시각 |
| Revision | 선택/되돌리기/export/입력 변경(`inputs-change`)의 이전·이후 상태와 이전 revision 참조 |
| Feedback | 청취자 원문, 대상 후보, 범위, 실행한 수정안(plan) 전체, 그 수정안이 만든 job |

`inputs-change`는 제목·스타일·가사·길이·BPM 등을 바꾼 기록이다. 과거 request는 자기
파라미터를 고정했으므로 이미 만든 후보의 출처는 바뀌지 않는다. `feedback`은 선택 필드이며
없는 프로젝트도 그대로 열린다. 후보 → artifact → 만든 job(또는 부모 batch) → feedback으로
"어떤 말이 어떤 버전이 되었는지"를 따라간다.

`project.json`은 같은 폴더의 임시 파일을 닫고 `fsync`한 뒤 `os.replace`로 반영한다. 들여쓰지 않은
JSON으로 쓴다(들여쓰면 후보가 많은 곡의 정본이 4.8MB에서 12.7MB가 되고 저장이 세 배 걸린다). 프로젝트
변경은 advisory file lock으로 직렬화하며, `project.json`이 없는 경로에는 폴더나 잠금 파일을 만들지 않고
거부한다. artifact 경로는 프로젝트 밖으로 나갈 수 없다. `inspect`는 대체된 청취 사본(`retired`, 파일을 일부러
지운 기록)을 검증하지 않고 `retiredArtifacts`로 따로 보여 준다.
완료 artifact의 파일 존재·크기·SHA-256을 다시 확인하지 못하면 선택·export·이어하기에 쓰지
않는다.

잠금은 둘이다. `.generation.lock`은 생성·구간 수정·재개 프로세스의 수명 동안 유지하며
동일 프로젝트의 두 번째 생성 요청을 거부한다. `library`·`status`는 실행 중인지 보려고 이 잠금(과 export
잠금)을 한순간 잡으므로, 시작하는 실행은 2초까지 다시 시도한 뒤에야 거부한다. `.project.lock`은 매번 최신 정본을
읽고 수정·저장하는 짧은 트랜잭션에만 사용한다. 추론, 다운로드, PCM QC는 정본 잠금 밖에서
실행하므로 사람의 메모와 수정이 오래 대기하거나 다음 진행률 저장에 덮이지 않는다. 엔진 진행률은 1초마다
묻지만 단계가 바뀌거나 앱에 보이는 정수 %가 올랐을 때만 저장한다.
프로세스 강제 종료 시 OS가 생성 잠금을 해제한다. 잠금 파일은 inode를 유지하도록 삭제하지 않는다.
생성·구간 수정·커버·재개는 `.generation.lock`보다 먼저 기기 공용 생성 차선을 공유로 잡는다
([작업 서비스](#작업-서비스)). `recover`는 정본만 고치므로 차선을 잡지 않는다.

## 작업 서비스

`music-engine serve`는 앱 없이 `127.0.0.1:47833`에서 arkAgent의 엔진 계약(`arkAgent/docs/ENGINE-CONTRACT.md`, v0)을
제공한다. 표준 라이브러리 HTTP 서버이며 의존성을 더하지 않는다. 코드는 `src/local_music_engine/service/`
(`server.py` API, `jobs.py` 작업 기록, `runner.py` 대기열, `recipes.py` 레시피)에 있다. 지금 레시피는 계약 점검
`selftest`뿐이다. 서비스 프로세스는 모델 런타임을 올리지 않고 ACE 서버도 부르지 않는다.

| 메서드·경로 | 내용 |
| --- | --- |
| `GET /api/health` | `{ok, engine: "music", version, contract: 0, busy, busyBy?, resident, currentJob, outputDir}` |
| `GET /api/recipes` | `{recipes: [{name, label, paramsSchema, needsReview}]}`. 스키마는 JSON Schema 2020-12 |
| `POST /api/jobs` | `{recipe, params}`를 검증해 대기열에 넣고 201로 작업 기록을 돌려준다. 입력 오류는 400(기록이 생기지 않는다), 종료 중이면 409 |
| `GET /api/jobs/{id}` · `GET /api/jobs?limit=100` | 작업 기록, 최근 작업(최신이 먼저, 최대 500) |
| `POST /api/jobs/{id}/cancel` | 대기 작업은 바로 `cancelled`, 실행 중이면 `cancelling` 뒤 `cancelled` |
| `POST /api/jobs/{id}/assets/{assetId}/review` | `{status}`: `pending`·`approved`·`rejected`. 사람의 판단과 그때 파일의 `reviewedSha256`을 기록한다 |
| `POST /api/jobs/{id}/findings/{findingId}/review` | `{status}`: `pending`·`cleared`. 사람이 그 자리를 확인했는지 기록한다 |

Host가 `127.0.0.1`·`localhost`가 아니거나 Origin이 같은 출처가 아니거나 다른 사이트의 Fetch Metadata가
붙은 요청은 403이다. Origin 없는 로컬 CLI·스크립트는 받는다. 오류 본문은 `{detail}`이다.

`health.busy`는 이 엔진이 지금 기기 메모리를 쥐고 있는지다. 일 때문이든 상주 모델 때문이든 참이다. 순서대로 본다.

1. 서비스가 실행 중인 작업이 있으면 `currentJob`이 그 ID다.
2. 기록 없이 생성 차선을 쥔 쪽이 있으면 `busyBy: "lane:generation.lock"`이다. CLI나 앱(CLI를 띄운다)의
   생성·구간 수정·커버·재개다.
3. 일이 없는데 기본 주소(`127.0.0.1:18001`, `:18002`)에 음악 모델 서버가 떠 있으면 `busyBy: "ace-step:18001"`처럼
   적고 `resident: true`다. 작업이 없어도 그 서버는 모델 메모리(ACE XL turbo 약 26GB)를 쥐고 있다. 일이 시작되면
   `resident`는 거짓으로 돌아간다. 생성이 켠 ACE는 차선이 비면 스스로 꺼지므로 이 상태는 손으로 켠 서버이거나,
   생성이 끝나고 꺼지기까지의 1–3초뿐이다. 계약의 "일이 없으면 모델을 쥐지 않는다"를 어기는 상태라 숨기지 않고
   알린다. 다른 주소에 손으로 띄운 서버는 보지 못한다. 생성이 켠 서버는 주소와 관계없이 그동안 차선을 쥐므로 2번으로 보인다.

생성 차선은 사용자·기기의 `~/Library/Caches/local-music-engine/generation.lock`(Mac, `MUSIC_ENGINE_LANE_LOCK`로 바꾼다)이다.
CLI의 생성 경로는 공유로, 서비스 작업은 배타로 잡는다. 그래서 CLI 생성끼리는 전처럼 함께 돌고, 서비스 작업은
어느 CLI 생성과도 겹치지 않는다. 서비스 작업은 차선을 얻을 때까지 `queued`로 기다린다. 서비스가 차선을 쥔 동안
CLI 생성은 2초 기다린 뒤(health가 잠깐 잡아 보는 것을 넘기기 위해) `LaneBusy`로 거절되고 앱은 이유를 알린다.
잠금은 프로세스가 죽으면 OS가 푼다.

작업 기록은 `<출력 폴더>/jobs/<작업 ID>/job.json`(기본 `output/`)이고 곡의 `project.json`과 별개다. 형식은 계약을
따른다: `id`, `recipe`, `title`, `params`(검증·정규화한 값), `state`, `createdAt`·`startedAt`·`finishedAt`,
`stages[{name, label, state, startedAt, progress, detail, seconds, peakMemoryBytes}]`, `assets[]`, `findings[]`,
`error`, `errorCode`, `logTail`. `dir`은 읽을 때 붙인다. 저장은 같은 폴더의 임시 파일을 fsync한 뒤 원자적으로 교체한다.
한 출력 폴더는 서비스 하나만 쓴다(`.service.lock`). 두 번째 서비스는 시작하지 못한다.

- 상태는 `queued → running → done | failed | cancelling → cancelled`, `queued → cancelled`다. 멈춤 기능이 없어 `paused`는
  쓰지 않는다.
- 서비스가 SIGINT·SIGTERM으로 멈추면 실행·대기 작업을 `failed` · `interrupted`로 닫고, 이미 취소 요청을 받은
  작업만 `cancelled`로 닫는다. 다시 시작할 때 남은 `queued`·`running`도 같은 규칙으로 닫으며 다시 실행하지 않는다.
  끝난 `selftest` 기록과 폴더는 이때 지운다.
- `errorCode`: 레시피가 실행 중 드러낸 입력 문제는 `invalid_input`, Python `MemoryError`는 `out_of_memory`, 나머지는
  `engine_error`다.
- 에셋의 `review`는 사람만 바꾼다. 승인·거절할 때 파일의 SHA-256을 `reviewedSha256`에 묶고, 그 뒤 파일이 바뀌거나
  사라지면 기록을 읽을 때 `pending`으로 보여 준다. 확인 항목(`findings`)의 `review`는 에셋 판단과 따로다.
- `selftest`(`steps` 1–10 기본 2, `stepSeconds` 0–30 기본 0.5, `failAt`)는 서비스 프로세스 안에서 단계마다 기다리며
  진행률을 쓴다. 단계 프로세스가 없어 `peakMemoryBytes`는 `null`이다. 0.5초 점검음 `selftest/check.wav` 하나와
  표본 확인 항목 하나를 남긴다.

## 작업 상태

```text
queued → running → succeeded | partial | failed | cancelling → cancelled
queued ─────────→ failed | cancelled
queued | running | cancelling ── 실행 프로세스 소멸 확인 ──→ interrupted
```

종료 상태를 다시 `running`이나 `succeeded`로 쓰는 전이는 거부한다. 후보 묶음 안에서 개별
seed가 실패하면 다음 seed를 계속하고 묶음은 `partial`이 된다. 새 `generate`는 같은 입력과
seed라도 새 작업이다. 새 batch는 시작할 때 **모든 seed의 요청**을 `frozenPayloads`에 고정한다.
아직 서버에 제출하지 않은 seed도 동일한 가사·스타일·길이·모델·음악 설정으로 이어진다.
`resume --job-id`는 특정 batch를 선택하고 `resumeOfJobId`로 새 시도를 연결한다. 재사용은
그 계보에 속한 성공 job의 후보, 같은 요청 fingerprint, 실제 파일의 존재·크기·SHA-256을
모두 확인한다. 다른 생성의 같은 seed를 가져오지 않는다. 완료 후보 참조와 원격 task ID는
즉시 저장한다. 새 후보와 재사용 후보는 CLI 결과에서 따로 구분한다.

`status`는 기록을 바꾸지 않고 생성 잠금 소유자가 없는 진행 작업을 `interrupted`로 보여준다.
`recover` 또는 다음 생성·재개가 이 상태를 정본에 기록한다. 이전 schema 1도 열린다. batch
스냅샷이 없으면 저장된 하위 요청에서 최초 입력을 복원하며, 요청까지 없으면 현재 입력으로
추측하지 않고 새 생성을 안내한다. 재개는 현재 프로젝트의 다음 생성 기본값을 바꾸지 않는다.

공식 v0.1.8 REST API에는 실행 중 task 취소 endpoint가 없다. 앱의 취소와 CLI의 `Ctrl-C`는
SIGINT로 CLI를 멈춰 batch와 진행 중이던 하위 job을 모두 `cancelled`로 남긴다. 다만 이미
제출된 곡 하나는 서버가 끝까지 계산할 수 있고, 앱은 취소 확인 창에서 이를 알린다.
`resume`은 원격 작업에 다시 접속하지 않으며 누락·실패한 seed를 명시적으로 새로 제출한다.

## 생성과 repaint

앱과 CLI의 기본 생성은 [자동 품질 흐름](AUTO-QUALITY.md)을 사용한다. 요청 버전마다 최초 생성과
재시도를 합쳐 최대 4회다. 모든 시도의 입력·seed·예산을 첫 batch에 고정하며, 명시적 재개도 그
예산을 늘리지 않는다. 가사 받아쓰기가 불확실하거나 로컬 검사기가 실패하면 가사 문제를 추측해
추가 추론하지 않는다. 실패한 검사기는 그 항목만 `unknown`으로 남기고, 이미 측정한 소리 결함과 그
재생성 근거는 지우지 않는다. 원격 timeout은 진행 중인 추론이 남을 수 있어 추가 자동 제출을 멈추고, 서버 불통
(응답 도중 끊긴 연결 포함)·HTTP 거절·모델 불일치도 같다. 이 판단은 오류 종류(`ace_adapter.stops_submissions`)로
한다. 진행 상황 조회가 끊기면 세 번까지 다시 묻는다.

`quality-check`, `audio-finish`, `listening-copy`는 별도 하위 job이다. `listening-copy`는 정리본의 마스터링한
듣기용 사본(`listening-audio`, `sourceArtifactId`가 버전 음원)을 만들며 버전과 그 계보는 바꾸지 않는다. 실패해도
batch는 성공으로 끝나고 앱은 버전 음원을 재생한다([듣기용 사본](AUTO-QUALITY.md#듣기용-사본마스터링)). 후처리는 원본을 읽어 새 artifact·candidate·
revision을 만들고, 게시 전에 fsync한 임시 파일의 inode·byte 수·SHA-256 소유 정보를 정본에 쓴다.
복구는 성공 저장 전의 소유 출력만 제거하며, 성공 저장 뒤에는 게시한 WAV를 남기고 임시 링크만
정리한다. 후처리가 실패하면 검증된 원본을 추천하고 `quality.finishingError`를 남기며 batch는
`partial`로 끝난다. 그 batch를 재개하면 생성·검사 없이 후처리만 다시 한다.
`recommendedCandidateId`는 자동 추천이며 사람이 정한 `selectedCandidateId`와
`humanReview`를 바꾸지 않는다. 원본·재시도는 화면의 자동 생성 이력에서 접근할 수 있다.

로컬 품질 모델은 `.runtime/quality/.venv`에서 실행하는 수명 제한 subprocess에만 적재한다.
모델 적재 전 `analysis.lock`을 획득해 여러 검사기가 메모리를 동시에 점유하지 않으며, 호출한
프로세스가 사라지면 검사기도 종료한다. 음원·가사는 외부 음성 API에 전송하지 않는다.
구간 수정은 입력과 길이를 변경하지 않고 전체 수정 음원을 한 번 검사·정리한다. 부모 버전이 요청한
BPM과 다르게 연주되면 `frozen_repaint`가 앞뒤 원곡에서 잰 템포를 요청에 고정한다(`playedTempo`). 사용자가 요청한
구간 수정은 자동으로 반복하지 않으며 선택 범위는 후처리 후보에도 남긴다. 생성 batch 안의 가사 누락
구간 수리(`--repair-attempts`로 켤 때만)는 정리본을 부모로 하는 `repaint-candidate` 하위 job(`qualitySlot`·`repairAttempt`)이며,
채택 여부는 그 후보의 `quality.repair`에 남는다([가사 누락 구간 수리](AUTO-QUALITY.md#가사-누락-구간-수리)).

text-to-music는 JSON 요청, repaint는 공식 서버의 absolute-path 거부 정책 때문에 multipart
업로드를 사용한다. repaint는 원본과 같은 길이의 새 artifact를 만들며 자동 채택하지 않는다.
사용자가 고른 범위는 `editRange`, 실제로 모델에 제공한 전체 곡은 `contextRange`로 분리한다.
엔진은 수정 뒤 전곡의 음량을 다시 맞추므로, 내려받은 결과는 artifact로 기록하기 전에
`edit_splice.py`가 바뀐 구간 밖을 부모 버전의 샘플로 되돌린다. 실제로 바뀐 구간과 적용 여부는
artifact의 `splice`에 남는다([구간 수정](AUTO-QUALITY.md#구간-수정)).

ACE의 `instruction`은 DiT 작업 템플릿(`Repaint the mask area based on the given
conditions:`)이다. 자유 문장을 넣으면 템플릿을 덮어쓴다. 그래서 요청은 이번 repaint에만 쓰는
캡션(`--style`, request의 `prompt`)과 `repaint_mode=balanced` + `repaint_strength`
(살짝 0.25 · 보통 0.5 · 많이 0.8)로 전한다. `--instruction`은 의도한 실험용으로만 남겼다.
새 버전으로 고치는 수정안은 프로젝트 스타일을 `inputs-change`로 바꾼 뒤 새 batch를 만든다.
구간 수정은 부모 버전의 고정된 가사·스타일·음악 설정 및 원본 파일의 실제 길이를 사용한다.
`repaint --lyrics`는 이번 요청에만 적용한다. 전체 수정안도 `generate --source-candidate-id`로
대상 버전의 입력을 먼저 복원하고 수정안을 적용하므로 다른 버전의 가사가 섞이지 않는다.

export는 새 내부 artifact를 만들며 외부 출력은 프로젝트 밖의 존재하지 않는 파일만 허용한다. 검증된 듣기용
사본이 있으면 그것을, `--unmastered`면 버전 음원을 복사한다.
임시 파일을 hard link로 게시해 검사 후 이름을 선점하는 경합에서도 기존 파일을 덮지 않는다.
선택 되돌리기도 복원할 파일의 크기와 SHA-256을 검증한다.

내보내기는 별도 `.export.lock`으로 실행 소유권을 표시하고 복사 동안 정본 잠금을 점유하지 않는다.
게시 전 임시 파일과 출력 파일의 소유 정보를 정본에 기록한다. 중단 복구는 파일 신원과 내용이
해당 작업의 기록과 맞는 미완료 출력만 정리한다. 최종 저장 오류는 메모리에서 바꾼 상태가 아닌
현재 정본을 다시 읽어 처리하므로 이미 완료한 artifact와 revision을 취소 상태로 바꾸지 않는다.
안전하지 않거나 읽을 수 없는 후보 파일은 그 후보만 무효로 표시하고 다른 후보·재개를 보존한다.

## 수정 도우미와 초안

`assistant.py`의 수정안은 한 가지 계약을 따른다: `action`(repaint|regenerate), 바뀐
캡션 전체, 태그 단위 변경 목록, 범위, 강도, 버전 수, 사람이 읽을 요약과 주의점. 기본 규칙은
오프라인 사전(23가지 표현)이고, 설정하면 loopback LLM(Ollama 기본 API 또는 OpenAI 호환)이
같은 스키마로 답한다. LLM 응답은 실행 가능한 값인지 검증하고, 실패하면 규칙으로 해석한 뒤 그
사실을 주의점에 적는다. Ollama 호출은 `keep_alive: 0`으로 답한 뒤 모델을 내려 음악 엔진과
통합 메모리를 다투지 않게 한다. 캡션이 문장형이면 규칙은 태그를 덧붙이기만 한다.

`drafting.py`는 설명에서 제목·스타일·가사 초안을 만든다. LLM 도우미가 있으면 LLM이 가사까지 쓰고, 없으면
로컬 규칙이 수정 가능한 예시 초안을 만들며 음악 서버를 부르지 않는다. CLI의 `draft --engine ace-step`만
한국어 핵심어를 영어 태그로 바꿔 켜져 있는 ACE의 `/v1/create_sample`에 묻고, 요청한 언어가 아닌 가사는 버리고
알린다.
