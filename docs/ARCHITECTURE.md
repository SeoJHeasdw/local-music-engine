# 아키텍처와 데이터 계약

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

ACE 서버는 이미 응답하는 서버가 있으면 그대로 쓰고(외부 소유), 없으면 main이
`scripts/start_ace_api.sh`를 별도 프로세스 그룹으로 띄워 소유한다. 앱 종료(⌘Q, SIGINT,
SIGTERM) 때 CLI가 취소 상태를 저장할 시간을 준 뒤 소유한 서버만 끈다. 생성 작업은 앱에서
한 번에 하나이며 시작 요청도 직렬화한다. 평가와 메모는 생성 중에도 즉시 `project.json`에
저장한다. 설정은 앱 데이터 폴더의 `settings.json`에, 마지막으로 연 곡은 `state.json`에
저장한다. 두 파일의 변경은 각각 직렬화하며 고유 임시 파일·fsync·원자 교체를 사용한다.

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
