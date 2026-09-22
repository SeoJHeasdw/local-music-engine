# local-music-engine

Apple Silicon Mac에서 한국어 곡을 만들고, 들어 보고, 말로 고치는 로컬 제작 엔진과 데스크톱 앱이다.
음악 용어를 몰라도 된다. "비 오는 밤 혼자 걷는 잔잔한 발라드"처럼 설명하면 초안이 채워지고,
"후렴 가사가 웅얼거려요"처럼 들은 그대로 적으면 도우미가 엔진이 알아듣는 스타일 태그·구간·
변화 정도로 바꿔 보여 준다. 실행 전에 무엇이 바뀌는지 태그 단위로 확인하고 고칠 수 있다.

원본 버전은 덮어쓰지 않는다. 구간 수정은 새 수정본이 되고, 원본과 같은 위치를 번갈아 들을 수
있다. 요청 원문, 실행한 수정안, 결과 버전, 사람 평가가 모두 `project.json`에 남는다. 자동 검사
통과는 발음이나 음악성 승인을 뜻하지 않는다.

생성 중에도 평가·메모가 즉시 저장된다. 앱이나 CLI가 강제로 종료되면 중단 상태를 구분하고,
작업실의 **이어서 만들기**에서 처음 요청한 가사·스타일·길이로 복구한다. 그 뒤 곡 설정을
바꿨어도 진행 중이던 작업의 입력은 달라지지 않는다. 자동 검사의 무음·최대 음량 경고는 시간
구간을 표시하며, 버튼으로 그 부분을 반복해 들을 수 있다.

엔진은 ACE-Step 1.5(native MLX)이며 M4 Max에서 전체 생성과 구간 repaint를 실제 검증했다.
보컬 분리·한국어 ASR은 아직 구현 전이다.

## 준비

- Apple Silicon Mac, [uv](https://docs.astral.sh/uv/), Node.js, Git
- 선택: [Ollama](https://ollama.com)와 한국어를 잘 쓰는 모델(예: `qwen3.6:27b`). 켜면 한글 가사
  초안과 자유 문장 피드백 해석을 맡는다.

```bash
chmod +x scripts/*.sh app.sh
./scripts/bootstrap_ace.sh   # ACE 런타임을 .runtime/에 설치. 첫 서버 시작 때 모델 약 11GB를 받는다
./app.sh                     # 엔진 Python·Electron 환경을 준비하고 앱을 연다
```

앱이 ACE 서버를 직접 켜고(`127.0.0.1:18001`에만 바인딩), 앱을 닫으면 앱이 켠 서버만 끈다.
이미 터미널에서 켠 서버가 있으면 그대로 쓴다.

## 앱

| 화면 | 하는 일 |
| --- | --- |
| 내 곡 | 곡 목록, 버전·수정·좋아요 수, Finder에서 곡 폴더 열기, 다른 폴더의 곡 열기 |
| 새 곡 만들기 | 설명 → 초안(제목·스타일·가사) → 스타일 칩으로 다듬기 → 길이·버전 수 → 만들기 |
| 작업실 | 버전 계보, 파형 재생·구간 선택, 원본↔수정본 비교, 고치기·평가·검사, 최종본, WAV 내보내기 |
| 설정 | 음악 엔진 켜기/끄기·기록, AI 도우미(규칙/Ollama/OpenAI 호환), 저장 위치, 기본값 |

작업실 단축키: `Space` 재생, `←/→` 5초(Shift 1초), `↑/↓` 버전 이동, `C` 원본↔수정본,
`L` 구간 반복, `F` 요청 입력, `Esc` 선택 해제. `⌘1–4` 화면 이동, `⌘N` 새 곡, `⌘,` 설정.

용어: **버전**은 같은 설정으로 만든 전체 곡 하나, **수정본**은 한 버전의 구간만 다시 만든 것,
**최종본**은 WAV로 내보낼 버전이다(언제든 바꿀 수 있다).

## CLI

앱의 모든 동작은 같은 CLI를 부른다. 명령마다 JSON 한 개를 출력한다.

```bash
uv run music-engine draft --query "비 오는 밤 혼자 걷는 잔잔한 발라드, 여자 목소리" --duration 90 \
  --assistant ollama --assistant-model qwen3.6:27b
uv run music-engine init projects/my-song --title "내 노래" --lyrics-file lyrics.txt \
  --style "Korean ballad, calm, female vocal, piano" --duration 120
uv run music-engine generate projects/my-song --seeds 101,102
uv run music-engine status projects/my-song        # 버전 계보·사용한 스타일·피드백·작업
uv run music-engine library --dir projects         # 곡 목록 (음원 해시 없이)
```

들은 뒤 사람의 판단은 따로 기록한다.

```bash
uv run music-engine review projects/my-song <candidate-id> --status approved --rating 4 --note "후렴 좋음"
uv run music-engine select projects/my-song <candidate-id>
uv run music-engine undo-selection projects/my-song
```

고치기는 수정안을 먼저 보고 실행한다. 수정안은 아무것도 바꾸지 않는다.

```bash
uv run music-engine plan projects/my-song <candidate-id> \
  --feedback "후렴 가사가 웅얼거려요" --start 30 --end 42 --strength medium
# 수정안의 stylePrompt로 그 구간만 다시 만든다. 부모 버전은 그대로 남는다
uv run music-engine repaint projects/my-song --candidate-id <candidate-id> \
  --start 30 --end 42 --seed 201 --style "<수정안 stylePrompt>" --strength medium \
  --feedback-json '{"text": "후렴 가사가 웅얼거려요", "candidateId": "<candidate-id>"}'
# 곡 전체를 바꾸는 수정안이면 스타일을 고친 뒤 새 버전을 만든다
uv run music-engine generate projects/my-song --seeds 301,302 --style "<수정안 stylePrompt>"
```

`--instruction`은 ACE의 작업 템플릿을 덮어쓰므로 요청 전달에 쓰지 않는다. 요청은 `--style`과
`--strength`(light·medium·strong)로 전한다.

```bash
uv run music-engine revise projects/my-song --title "새 제목" --duration 150   # 다음 버전부터 적용
uv run music-engine export projects/my-song --output ~/Music/my-song.wav
uv run music-engine inspect projects/my-song
uv run music-engine resume projects/my-song   # 멈춘 생성만, 검증된 버전은 재사용
uv run music-engine resume projects/my-song --job-id <batch-job-id>  # 특정 작업을 이어 만들기
uv run music-engine recover projects/my-song  # 실행 프로세스가 없는 작업을 interrupted로 기록
```

`resume`은 같은 작업의 재개 계보에 속한 완료 파일만 크기·SHA-256을 검사해 재사용한다.
시도마다 새 batch가 남으며 현재 프로젝트 입력을 과거 값으로 되돌리지 않는다. 원래 입력을
확인할 수 없는 아주 초기 프로젝트는 새 생성을 요청해야 한다.
`generate --source-candidate-id <candidate-id>`는 특정 버전의 가사·길이·음악 설정을 바탕으로
새 버전을 만든다. `repaint --lyrics`는 이번 수정본에만 적용한다.

WAV를 외부로 내보낼 때는 곡 폴더 밖의 새 파일 이름을 고른다. 기존 파일, 원본 음원,
`project.json`을 덮어쓰는 경로는 거부한다.

## 에이전트로 다루기

코딩 에이전트는 사람과 같은 루프를 CLI로 돈다: `status`로 버전과 사용한 스타일을 읽고, 사람이
남긴 평가·메모를 근거로 `plan`을 만들고, `repaint`/`generate`에 `--feedback-json`을 붙여 실행한
뒤, 결과를 사람에게 들어 보라고 넘긴다. 에이전트는 음악성을 판정하지 않는다. `review`의
`approved`/`rejected`는 사람이 들은 뒤에만 기록한다.

## 데이터 경계

- `projects/`, `artifacts/`, `exports/`, `.runtime/`, 모델과 음원은 Git에서 제외된다.
- artifact는 프로젝트 상대 경로·byte 수·SHA-256·실제 sample 메타데이터로 검증한다.
- repaint/export/입력 변경은 과거 파일과 기록을 덮지 않고 새 artifact와 revision으로 남는다.
- renderer는 파일 경로나 명령 실행 API를 받지 않는다. 곡·버전은 main이 목록으로 준 id로만 가리킨다.
- AI 도우미와 엔진 주소는 이 Mac 안의 loopback 주소만 허용한다. 가사가 외부로 나가지 않는다.
  Python의 환경 프록시를 사용하지 않으며 HTTP 리다이렉트를 따라가지 않는다.
- 기존 `local-tts-engine`의 환경·모델·개인 음성은 사용하거나 변경하지 않는다.

## 검사

```bash
uv sync --python 3.12.12 --group dev
uv run python -m compileall -q src tests
uv run pytest
npm run check:app
npm run test:app
npm run build:app
```

실제 추론 검사는 ACE 서버를 따로 켠 뒤 실행한다. 아래 명령은 새 폴더에 실제 음원을 만들며
생성 중 저장, 원래 입력 유지, 검증 후 재사용, repaint, export를 검사한다.

```bash
./scripts/start_ace_api.sh
# 다른 터미널에서 (아직 없는 출력 폴더를 지정)
uv run python scripts/smoke_real_engine.py --output-dir .runtime/smoke-001
```

## 문서

- [구현 계획](PLAN.md)
- [아키텍처와 데이터 계약](docs/ARCHITECTURE.md)
- [품질 검사와 청취 계약](docs/QUALITY.md)
- [모델·라이선스 결정](docs/DECISIONS.md)
- [P0 Apple Silicon 실측](docs/P0-VALIDATION.md)
- [현재 상태와 다음 작업](docs/HANDOFF.md)
- [Astra 개선과 실제 검증 기록](docs/VALIDATION-ASTRA.md)
