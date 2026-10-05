# 아키텍처와 데이터 계약

## 2026-10-05 Music 3 전환

기본 생성은 `Music3Client` → 인증된 `127.0.0.1:18002` task API → 고정 판본의
`mlx-audio`와 `mlx-community/MiniMax-Music3-bf16`으로 실행한다(구조 BF16 → 음색 FP32 단계별 적재). 프로젝트 `.venv`와 추론용
`.runtime/minimax-music3/.venv`, 검사 `.runtime/quality/.venv`는 분리한다.

Electron main은 `scripts/start_music3_api.sh`의 프로세스 그룹을 소유하고, 모델 적재 전·후의
health를 구분한다. 같은 Music 3 엔진·모델·생성 능력이 확인돼야 준비 상태로 표시한다. 외부
소유 서버는 임의로 종료하지 않는다. 인증 키는 `.runtime/music3-api-key`의 0600 파일로 공유한다.
renderer에는 키·임의 파일 경로·명령 실행 능력을 주지 않는다.

서버는 인증된 health·생성·조회·opaque 완료 artifact 다운로드만 제공한다. 모델 적재와 계산은
하나의 MLX worker에서 실행한다. 실제 모델과 런타임 revision은 요청 adapterInfo·원격 결과에
남긴다. mock 계약 검사와 실제 모델 추론은 구분한다.

제작 규칙과 songPlan은 Global Metadata / Vocal Details / Arrangement로 매핑한다. 표현 지시는
caption에 옮기고 태그와 실제 가사는 독립 줄로 나눠 모델의 가사 유실을 막는다. 원문·단어 순서와
과거 요청은 보존하고 실제 준비본·변경 사항은 새 요청에 기록한다. Music 3에는 ACE 전용 LM·
thinking·CFG 설정을 전달하지 않는다. caption v4는 모델 학습 형식의 13개 항목으로 렌더링한다.
프리셋의 `music3Schema`와 규칙의 `music3Fields`를 요청에 동결하며, 이전 snapshot에 이 항목이 없으면
그 snapshot의 기존 문장(`music3Caption` 또는 ACE caption)을 규칙별 기본 항목에 넣는다. 시간·마디·
음절 수 추정은 계획에 보존하되 모델에는 실제 가사 구간의 역할·선율·표현을 안내한다.
상한은 300초이며 EOS에 따른 실제 길이를 기록하고
무음 padding으로 길이를 맞추지 않는다.

현재 공개 구현은 text2music만 지원한다. cover·repaint·파형 참조는 새 CLI와 앱에서 차단한다.
기존 후보를 바탕으로 만들기는 가사·스타일·규칙으로 새 전체 곡을 생성하는 동작이다.
과거 engine 없는 ACE 요청도 legacy로 판별하며, 명시적인 `--engine ace-step` 없이 Music 3로
재해석해서 재개하지 않는다. 기존 음원·평가·선택·export는 계속 사용할 수 있다.

기본 초안은 한국어·영어 규칙 기반의 수정 가능한 예시 가사다. 선택한 로컬 도우미가 있으면
자유롭게 작사할 수 있다. Music 3에 없는 작사 API를 호출하거나 ACE를 기본 적재하지 않는다.

작업 수명·원자 저장·artifact 검증·자동 검사와 사람 청취 분리 계약은 아래와 같이 유지한다.
현재 Music 3의 취소는 로컬 CLI 작업을 취소하며 이미 제출한 원격 추론은 계속 계산할 수 있다.
timeout 이후에는 같은 묶음의 추가 seed 제출을 중단하고 미제출 seed를 별도로 기록한다.

## 과거 ACE 구현과 유지되는 데이터 계약

아래 ACE 서버·구간 수정·내장 초안 설명은 명시적인 legacy 경로의 계약이며,
앱 기본 실행이나 Music 3 요청에 적용하지 않는다.

## 현재 구현 범위

```text
Electron renderer ─ 제한된 preload IPC ─ Electron main ─┬─ music-engine CLI (JSON 한 개 출력)
 (내 곡·새 곡·작업실·설정)                              ├─ ACE 서버 프로세스 소유·health 감시
                                                        └─ music-artifact:// 토큰 스트리밍·파형
music-engine CLI ─ workflow.py ─ ProjectStore + qc.py
                 ├ execution.py (추론·다운로드·QC와 짧은 기록 트랜잭션)
                 ├ jobs.py      (최초 요청 스냅샷·재개 계보·중단 복구)
                 ├ views.py      (status·library: 앱과 에이전트가 읽는 뷰)
                 ├ assistant.py  (피드백 → 수정안: 규칙 또는 loopback LLM)
                 └ drafting.py   (설명 → 제목·스타일·가사 초안)
                        ↓
               AceStepClient → ACE-Step REST API (loopback) → v0.1.8 native MLX
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

ACE 서버는 이미 응답하는 서버가 있으면 그대로 쓰고(외부 소유), 없으면 main이
`scripts/start_ace_api.sh`를 별도 프로세스 그룹으로 띄워 소유한다. 앱 종료(⌘Q, SIGINT,
SIGTERM) 때 CLI가 취소 상태를 저장할 시간을 준 뒤 소유한 서버만 끈다. 생성 작업은 앱에서
한 번에 하나이며 시작 요청도 직렬화한다. 평가와 메모는 생성 중에도 즉시 `project.json`에
저장한다. 설정은 앱 데이터 폴더의 `settings.json`에, 마지막으로 연 곡은 `state.json`에
저장한다. 두 파일의 변경은 각각 직렬화하며 고유 임시 파일·fsync·원자 교체를 사용한다.

엔진 시작 준비 전체와 AI 도우미 호출은 같은 메모리 소유권 잠금으로 직렬화한다. 사용자 종료는
아직 실행 전인 시작 요청도 취소하고 도우미 종료 후 자동 재시작을 억제한다. Ollama 모델 해제는
성공 응답과 잔류 모델 목록을 확인한 뒤에만 ACE 시작을 허용한다.

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

`project.json`은 같은 폴더의 임시 파일을 닫고 `fsync`한 뒤 `os.replace`로 반영한다. 프로젝트
변경은 advisory file lock으로 직렬화한다. artifact 경로는 프로젝트 밖으로 나갈 수 없다.
완료 artifact의 파일 존재·크기·SHA-256을 다시 확인하지 못하면 선택·export·이어하기에 쓰지
않는다.

잠금은 둘이다. `.generation.lock`은 생성·구간 수정·재개 프로세스의 수명 동안 유지하며
동일 프로젝트의 두 번째 생성 요청을 즉시 거부한다. `.project.lock`은 매번 최신 정본을
읽고 수정·저장하는 짧은 트랜잭션에만 사용한다. 추론, 다운로드, PCM QC는 정본 잠금 밖에서
실행하므로 사람의 메모와 수정이 오래 대기하거나 다음 진행률 저장에 덮이지 않는다.
프로세스 강제 종료 시 OS가 생성 잠금을 해제한다. 잠금 파일은 inode를 유지하도록 삭제하지 않는다.

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
추가 추론하지 않는다. 원격 timeout은 진행 중인 추론이 남을 수 있어 추가 자동 제출을 멈춘다.

`quality-check`와 `audio-finish`는 별도 하위 job이다. 후처리는 원본을 읽어 새 artifact·candidate·
revision을 만들고, 게시 전에 fsync한 임시 파일의 inode·byte 수·SHA-256 소유 정보를 정본에 쓴다.
복구는 성공 저장 전의 소유 출력만 제거하며, 성공 저장 뒤에는 게시한 WAV를 남기고 임시 링크만
정리한다. `recommendedCandidateId`는 자동 추천이며 사람이 정한 `selectedCandidateId`와
`humanReview`를 바꾸지 않는다. 원본·재시도는 화면의 자동 생성 이력에서 접근할 수 있다.

로컬 품질 모델은 `.runtime/quality/.venv`에서 실행하는 수명 제한 subprocess에만 적재한다.
모델 적재 전 `analysis.lock`을 획득해 여러 검사기가 메모리를 동시에 점유하지 않으며, 호출한
프로세스가 사라지면 검사기도 종료한다. 음원·가사는 외부 음성 API에 전송하지 않는다.
구간 수정은 입력과 길이를 변경하지 않고 전체 수정 음원을 한 번 검사·정리한다. 자동으로
repaint를 반복하지 않으며 선택 범위는 후처리 후보에도 남긴다.

text-to-music는 JSON 요청, repaint는 공식 서버의 absolute-path 거부 정책 때문에 multipart
업로드를 사용한다. repaint는 원본과 같은 길이의 새 artifact를 만들며 자동 채택하지 않는다.
사용자가 고른 범위는 `editRange`, 실제로 모델에 제공한 전체 곡은 `contextRange`로 분리한다.
실측상 선택 범위 밖도 바뀌므로 두 기록은 같은 범위라고 가정하지 않는다.

ACE의 `instruction`은 DiT 작업 템플릿(`Repaint the mask area based on the given
conditions:`)이다. 자유 문장을 넣으면 템플릿을 덮어쓴다. 그래서 요청은 이번 repaint에만 쓰는
캡션(`--style`, request의 `prompt`)과 `repaint_mode=balanced` + `repaint_strength`
(살짝 0.25 · 보통 0.5 · 많이 0.8)로 전한다. `--instruction`은 의도한 실험용으로만 남겼다.
새 버전으로 고치는 수정안은 프로젝트 스타일을 `inputs-change`로 바꾼 뒤 새 batch를 만든다.
구간 수정은 부모 버전의 고정된 가사·스타일·음악 설정 및 원본 파일의 실제 길이를 사용한다.
`repaint --lyrics`는 이번 요청에만 적용한다. 전체 수정안도 `generate --source-candidate-id`로
대상 버전의 입력을 먼저 복원하고 수정안을 적용하므로 다른 버전의 가사가 섞이지 않는다.

export는 새 내부 artifact를 만들며 외부 출력은 프로젝트 밖의 존재하지 않는 파일만 허용한다.
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

`drafting.py`는 설명에서 제목·스타일·가사 초안을 만든다. LLM이 있으면 LLM이 한글 가사까지
쓰고, 없으면 한국어 핵심어를 영어 태그로 바꿔 ACE `/v1/create_sample`에 영어로 묻는다. 한글이
아닌 가사는 버리고 알린다. 초안은 BPM·조성을 채우지 않는다(생성 때 ACE가 정한다).
