# local-music-engine

Apple Silicon Mac에서 곡을 만들고, 들어 보고, 가사와 편곡을 다듬는 로컬 제작 엔진과 데스크톱 앱이다.
기본 생성 엔진은 **MiniMax Music 3 · native MLX · MXFP8**이다. 설명에서 제목·스타일·가사
초안을 준비하고, 감성 힙합·신나는 팝 제작 규칙을 골라 새 곡을 만든다. 한국어와 영어 가사를
지원한다. 발음과 음악적 완성도는 생성한 음원을 직접 들어 확인한다.

원본과 모든 버전을 보존한다. 요청·생성·자동 검사·사람 평가·최종본 선택의 정본은
`project.json`이다. 자동 검사 통과와 사람 청취 승인은 별도이며, 새 음원은 `unreviewed`다.
기존 ACE로 만든 곡도 재생·비교·평가·선택·내보낼 수 있다.

## 준비

- Apple Silicon Mac, [uv](https://docs.astral.sh/uv/), Node.js, Git
- [FFmpeg](https://ffmpeg.org/): 로컬 받아쓰기, true peak·음량 측정
- 선택: Ollama 또는 OpenAI 호환 loopback LLM. 자유로운 가사 초안과 피드백 해석에 사용

```bash
chmod +x scripts/*.sh app.sh
./scripts/bootstrap_music3.sh  # 독립 Python 3.12 환경과 고정 판본의 약 13GB Music 3 모델 준비
./app.sh                      # 프로젝트 Python·Electron 환경 준비, 앱 빌드 및 실행
```

환경은 루트 `.venv`, Music 3의 `.runtime/minimax-music3/.venv`, 품질 검사의
`.runtime/quality/.venv`로 분리한다. 과거 ACE 환경과 모델은 삭제하지 않는다.
음원·가사·프로젝트·모델·로그는 Git에 넣지 않는다. 생성과 품질 검사에서 음원·가사를 외부 API에
업로드하지 않는다. 최초 모델 다운로드에는 인터넷이 필요하다.

모델은 `mlx-community/MiniMax-Music3-mxfp8`의 고정 revision을 쓴다. 커뮤니티 MLX 구현으로
8B 구조 모델과 음향 합성 구성요소를 함께 실행한다. 8B 모델 규모와 MXFP8 가중치 정밀도는
서로 다른 개념이며, 모델 파일 크기는 실행 중 최대 메모리와 같지 않다.
[판본과 라이선스](docs/DECISIONS.md), [전환과 실측](docs/HANDOFF.md)을 참고한다.

앱은 `scripts/start_music3_api.sh`로 `127.0.0.1:18002` 서버를 켜고, 자기가 켠 서버만 종료한다.
이미 켜 둔 같은 Music 3 서버는 외부 소유로 사용한다. 다른 엔진이나 모델이 응답하면 준비된
서버로 인정하지 않는다. 한 번에 한 곡을 계산하며 기본 실행에는 ACE·PyTorch가 필요하지 않다.

큰 로컬 AI 도우미를 부를 때 앱은 자기가 켠 음악 엔진을 잠시 끄고, 답을 받으면 다시 켠다.
외부에서 켠 서버는 임의로 종료하지 않는다. 곡을 만드는 중에는 도우미를 호출하지 않는다.
CLI에서 큰 도우미를 부를 때는 엔진을 직접 끈다.

인증 키는 `.runtime/music3-api-key`의 사용자 전용 파일(0600)로 main·CLI·서버가 공유한다.
별도 키는 `MUSIC_ENGINE_MUSIC3_API_KEY`, 키 파일은 `MUSIC_ENGINE_MUSIC3_API_KEY_FILE`로 지정한다.
renderer에는 키를 전달하지 않는다. 서버는 인증된 health·생성·진행 조회와 opaque ID의 완료
WAV 다운로드만 제공하며, 브라우저 Origin·임의 파일 경로·명령·모델 관리 API는 거부한다.

## 앱

| 화면 | 하는 일 |
| --- | --- |
| 내 곡 | 기존·새 곡 목록, 다른 곡 폴더 열기, 버전·사람 평가 확인 |
| 새 곡 만들기 | 설명과 언어 → 가사·스타일 초안 → 제작 규칙·전개 계획 → 전체 곡 생성 |
| 작업실 | 파형·구간 반복·버전 비교, 가사·편곡을 바탕으로 새 전체 버전, 검사·평가·최종본·WAV 내보내기 |
| 설정 | Music 3 상태·시작·종료·기록, AI 도우미, 저장 위치와 생성 기본값 |

기존 설정은 최초 실행 때 Music 3로 옮기고 원본을 백업한다. 저장 위치·AI 도우미·내보내기
위치와 기존 곡 데이터는 보존한다. 요청 길이 상한은 **300초**다. 길이는 상한 요청이며 모델이
끝 토큰을 먼저 내면 실제 음원이 더 짧아질 수 있다. 실제 길이는 artifact에 기록하고 무음을
덧붙여 성공한 것처럼 표시하지 않는다.

현재 공개 구현은 텍스트·가사로 **전체 곡 생성**을 지원한다. 음원 참조 커버·음색 복사·선택
구간 repaint는 지원하지 않는다. 특정 버전을 바탕으로 새로 만들면 그 버전의 **가사·스타일·
제작 규칙**을 사용한다. 기존 파형을 모델에 전달하지 않는다. 지원하지 않는 편집을 전체 생성으로
몰래 바꾸지 않는다. 기존 ACE 요청도 Music 3 요청으로 재해석하지 않는다.

## 제작 규칙과 곡 계획

감성 힙합은 84 BPM·A minor·4/4, 신나는 팝은 120 BPM·C Major·4/4를 제안한다. 직접 입력한
음악 정보가 우선한다. 이 값은 제작 안내이며 실제 음원의 엄격한 보장이 아니다.

박자 유지, 코드 반복, 단출한 악기 구성, 또렷한 보컬, 깨끗한 음색, 단순한 구성의 여섯 규칙을
개별 선택할 수 있다. **구간별 전개 만들기**와 **가사 호흡 나누기**는 추가 옵션이다. 구간의 상대
강약과 호흡을 안내하며, 긴 줄은 기존 띄어쓰기·문장부호에서만 나눠 단어와 순서를 보존한다.
원문·준비본·계획·변경 내용은 따로 저장한다. 연주곡에는 보컬·호흡 규칙을 적용하지 않는다.
**기억에 남는 선율**과 **보컬 감정 살리기**도 선택할 수 있다. 감성 힙합 프리셋은 편안한
싱잉랩 구절에서 노래하는 후렴으로, 신나는 팝은 밝은 선율과 탄력 있는 프레이징으로 안내한다.
선율 규칙은 도입에서 후렴의 주제를 예고하고, 후렴의 리듬·선율 동기와 응답 구절을 반복하도록 안내한다.

Music 3에는 안내를 **Global Metadata / Vocal Details / Arrangement**의 구조화된 음악 설명으로
보낸다. 구간의 표현 지시는 편곡 설명에 옮기고 가사에는 `[Verse]`, `[Chorus]` 같은 단순한 태그를
쓴다. ACE 전용 LM·샘플링 옵션은 전달하지 않는다.
현재 caption v3는 숫자 시간표 대신 실제 가사 구간의 음악적 역할을 전달한다. 추정 시간·마디·
음절 수는 기록과 미리보기에 남긴다. 지시의 효과는 후보별로 확인하며 음악성을 보증하지 않는다.
[Music 3 입력 계약](https://huggingface.co/MiniMaxAI/MiniMax-Music3)을 따른다.

AI 도우미가 없으면 로컬 규칙으로 수정 가능한 초안을 제공한다. 초안을 위해 ACE 모델을 올리거나
Music 3에 없는 작사 API를 호출하지 않는다. 자유로운 작사는 선택한 로컬 도우미를 쓸 수 있다.

## CLI

앱과 CLI는 같은 저장·생성·검사 흐름을 쓴다. 명령마다 JSON 한 개를 출력한다.

```bash
uv run music-engine production-rules
uv run music-engine draft --query "늦은 밤 감성 힙합" --duration 30 --vocal-language en
uv run music-engine init projects/my-song --title "내 노래" --lyrics-file lyrics.txt \
  --style "emotional hip hop, warm piano, clear male melodic rap" --duration 60
uv run music-engine generate projects/my-song --seeds 101
uv run music-engine status projects/my-song
uv run music-engine library --dir projects
```

`init`·`revise`·`draft`의 `--production-rules-json`으로 화면과 같은 규칙을 전달한다.
기본 생성은 Music 3와 `--quality auto --quality-attempts 4`다. 버전마다 첫 생성·재시도를 합쳐
최대 네 번이며 검사 불확실성만으로 다시 만들지 않는다. 모든 시도의 입력과 예산을 첫 batch에
고정하고, 명시적인 재개도 예산을 늘리지 않는다. 자동 추천은 사람의 최종본 선택과 분리한다.
[자동 품질 흐름과 한계](docs/AUTO-QUALITY.md)를 참고한다.

```bash
uv run music-engine generate projects/my-song --seeds 101 --quality-attempts 2
uv run music-engine generate projects/my-song --seeds 102 --quality audio
uv run music-engine generate projects/my-song --source-candidate-id <candidate-id> \
  --seeds 103 --style "fuller chorus, clear lead vocal"
uv run music-engine revise projects/my-song --title "새 제목" --duration 120
uv run music-engine resume projects/my-song --job-id <batch-job-id>
uv run music-engine recover projects/my-song
uv run music-engine review projects/my-song <candidate-id> --status listened --note "후렴 좋음"
uv run music-engine select projects/my-song <candidate-id>
uv run music-engine undo-selection projects/my-song
uv run music-engine export projects/my-song --output ~/Music/my-song.wav
```

`--source-candidate-id`는 그 버전의 입력으로 새 전체 곡을 만든다. 새 생성은 과거 결과를
묵시적으로 재사용하지 않는다. `resume`만 같은 계보·요청 fingerprint·파일 존재·크기·SHA-256이
모두 일치하는 완료 결과를 재사용한다. export는 프로젝트 밖의 새 파일 이름만 허용한다.

과거 ACE 환경은 명시적인 `--engine ace-step`에서만 사용한다. 과거 요청·음원·이력을 보존하기
위한 호환 경로다. 과거 실측 실행기는 `scripts/smoke_real_engine.py`와
`scripts/compare_production.py`로 남는다.

## 데이터와 검사

- `project.json`은 같은 디렉터리의 임시 파일을 fsync한 뒤 원자적으로 교체한다.
- 완료 artifact는 파일 존재·크기·SHA-256과 실제 sample 메타데이터로 검증한다.
- 새 생성·후처리·export는 새 artifact다. 원본과 과거 revision을 덮지 않는다.
- 새 후보는 자동 검사 결과와 관계없이 사람 청취 `unreviewed`다.
- renderer는 main이 확인한 곡·버전 ID만 쓰며 임의 파일 접근·명령 실행 API를 받지 않는다.
- 엔진과 도우미는 loopback만 허용하고 환경 프록시·HTTP 리다이렉트를 따르지 않는다.
- 기존 `local-tts-engine`의 환경·모델·개인 음성을 사용하거나 변경하지 않는다.

첫 보컬 검사에서 품질 환경에 MLX Whisper·UMXHQ 가중치 약 1.65GB를 준비한다. 미리 준비하려면
`scripts/bootstrap_quality.sh`를 실행한다. 분석 실패 시 음원을 보존하고 불확실성을 표시한다.
연주곡은 받아쓰기 모델을 올리지 않는다. PCM·음량·가사 비교는 기술적 결함을 찾는 도구이며
선율의 매력이나 음악성을 자동 승인하지 않는다.

```bash
uv sync --python 3.12.12 --group dev
uv run python -m compileall -q src tests
uv run pytest
npm run check:app
npm run test:app
npm run build:app
```

실제 추론은 모델 없는 계약·회귀 검사와 따로 실행한다. 서버를 켜고 새 출력 폴더를 지정한다.
아래 검사는 영어 30초·한국어 30초·새 2절이 있는 영어 60초를 각각 한 번 생성하고, 자동 검사·
export·검증된 명시적 재개·원문 보존·사람 미청취 상태를 확인한다.

```bash
./scripts/start_music3_api.sh
# 다른 터미널에서
uv run python scripts/smoke_music3.py --output-dir .runtime/music3-smoke-new
```

좋다고 평가한 기존 후보를 기준으로 비교하려면 아래 실행기를 사용한다. 기준 파일의 존재·크기·
SHA-256을 확인해 새 프로젝트에 **재생용으로 명시적으로 복사**하고, 같은 가사·요청 길이·BPM·
조성·seed로 두 Music 3 후보를 각각 한 번 새로 생성한다. 원본 파형은 모델에 보내지 않는다.
작업실의 비교 목록에서 같은 위치를 번갈아 듣고 평가할 수 있다. 보고서의 음악 기준 판정은
직접 듣기 전까지 `pending_human_listening`이며 자동 가사 검사 결과로 승인하지 않는다.

```bash
uv run python scripts/compare_music3_quality.py \
  --baseline-project projects/reference-song --baseline-candidate candidate_id \
  --project-dir projects/music3-comparison-new --output-dir .runtime/music3-comparison-new
```

## 문서

- [아키텍처와 데이터 계약](docs/ARCHITECTURE.md)
- [품질 검사와 청취 계약](docs/QUALITY.md)
- [자동 품질 관리·재시도·실제 검증](docs/AUTO-QUALITY.md)
- [모델·라이선스 결정](docs/DECISIONS.md)
- [현재 상태와 전환 기록](docs/HANDOFF.md)
- [초기 설계 계획](PLAN.md), [과거 ACE 실측](docs/P0-VALIDATION.md)
