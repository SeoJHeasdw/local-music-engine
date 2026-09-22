# 아키텍처와 데이터 계약

## 현재 구현 범위

```text
Electron renderer → limited preload IPC → Electron main → music-engine CLI
                                                   ↓
music-engine CLI/API callers → workflow.py → ProjectStore + qc.py
                                  ↓
                         AceStepClient → ACE-Step REST API (loopback)
                                              ↓
                         ACE-Step v0.1.8 / native MLX workers
```

`workflow.py`는 CLI와 향후 FastAPI가 공유할 응용 계층이다. Electron main은 현재 타입이 제한된
IPC를 CLI 명령에 연결하므로 앱이 생성·검사 규칙을 다시 구현하지 않는다. renderer는 후보의 실제
경로를 받지 않으며 main이 검증된 WAV에 임시 token URL을 부여하고 축약 파형을 계산한다.
현재 ACE 서버는 공식 런타임이며 앱과 별도 프로세스다.
`scripts/start_ace_api.sh`는 loopback에만 바인딩한다.

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
| Revision | 선택/되돌리기/export의 이전·이후 상태와 이전 revision 참조 |

`project.json`은 같은 폴더의 임시 파일을 닫고 `fsync`한 뒤 `os.replace`로 반영한다. 프로젝트
변경은 advisory file lock으로 직렬화한다. artifact 경로는 프로젝트 밖으로 나갈 수 없다.
완료 artifact의 파일 존재·크기·SHA-256을 다시 확인하지 못하면 선택·export·이어하기에 쓰지
않는다.

## 작업 상태

```text
queued → running → succeeded | partial | failed | cancelling → cancelled
queued ─────────→ failed | cancelled
```

종료 상태를 다시 `running`이나 `succeeded`로 쓰는 전이는 거부한다. 후보 묶음 안에서 개별
seed가 실패하면 다음 seed를 계속하고 묶음은 `partial`이 된다. 새 `generate`는 같은 입력과
seed라도 새 작업이다. `resume`만 입력 fingerprint와 실제 artifact 검증을 거쳐 완료 후보를
재사용하고 실패 seed를 다시 실행한다.

공식 v0.1.8 REST API에는 실행 중 task 취소 endpoint가 없다. 현재 CLI의 `Ctrl-C`는 로컬
job을 `cancelled`로 남기지만 이미 제출된 공식 서버 task를 개별 중지하지 못한다. 앱 단계에서
서버 프로세스를 앱이 소유하고 작업자 프로세스 단위 중지를 추가하기 전까지의 알려진 제한이다.

## 생성과 repaint

text-to-music는 JSON 요청, repaint는 공식 서버의 absolute-path 거부 정책 때문에 multipart
업로드를 사용한다. repaint는 원본과 같은 길이의 새 artifact를 만들며 자동 채택하지 않는다.
사용자가 고른 범위는 `editRange`, 실제로 모델에 제공한 전체 곡은 `contextRange`로 분리한다.
실측상 선택 범위 밖도 바뀌므로 두 기록은 같은 범위라고 가정하지 않는다.
