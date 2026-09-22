# local-music-engine

Apple Silicon Mac에서 한국어 가사와 음악 지시로 곡 후보를 만들고, 원본을 보존한 채 비교·선택·
구간 repaint·WAV export·프로젝트 복원을 수행하는 로컬 제작 엔진이다.

현재 M4 Max에서 ACE-Step 1.5의 native MLX 전체 생성과 repaint를 실제 검증했고, CLI의 프로젝트
정본·후보 계보·QC·명시적 이어하기·선택/되돌리기·export가 동작한다. Electron 비교 UI는
실제 파형·동기 위치 전환·QC·청취 기록·선택·프로젝트 복원을 지원한다. 보컬 분리/한국어 ASR은
아직 구현 전이다. 자동 QC 통과는 발음이나 음악성 승인을 뜻하지 않는다.

## 준비

요구 사항:

- Apple Silicon Mac
- Python 3.12(스크립트가 uv로 3.12.12 설치)
- [uv](https://docs.astral.sh/uv/)
- Git

프로젝트 CLI 환경:

```bash
uv sync --python 3.12.12 --group dev
uv run pytest
```

ACE-Step 런타임은 프로젝트와 분리되어 `.runtime/`에 설치된다.

```bash
chmod +x scripts/*.sh
./scripts/bootstrap_ace.sh
./scripts/start_ace_api.sh
```

첫 서버 시작은 공식 핵심 모델 약 10GB와 선택 0.6B LM을 받는다. 공식 통합 snapshot에 1.7B LM도
포함되어 실측 모델 폴더는 약 11GB다. 서버는 `127.0.0.1:18001`에만 바인딩된다.

후보 비교 앱:

```bash
npm install
npm run check:app
npm run start:app
```

앱은 최근 프로젝트를 복원한다. renderer에는 임의 파일 경로나 shell API를 노출하지 않으며,
main이 검증한 후보의 임시 URL과 축약 파형만 전달한다.

## 사용

ACE 서버를 띄운 상태에서 다른 터미널로 실행한다.

```bash
uv run music-engine init projects/my-song \
  --title "내 노래" \
  --lyrics-file lyrics.txt \
  --style "Korean indie pop, clear female vocal" \
  --duration 120 \
  --structure "Verse-Chorus-Verse-Chorus-Bridge-Chorus"

uv run music-engine generate projects/my-song --seeds 101,102,103,104
uv run music-engine candidates projects/my-song
```

후보를 실제로 들은 뒤 사람의 판단을 따로 기록한다.

```bash
uv run music-engine review projects/my-song <candidate-id> \
  --status listened --rating 4 --note "후렴 발음 재확인 필요"
uv run music-engine select projects/my-song <candidate-id>
```

선택 후보의 구간 repaint는 새 후보를 만들며 자동 채택하지 않는다.

```bash
uv run music-engine repaint projects/my-song \
  --start 30 --end 42 \
  --instruction "Keep arrangement and improve Korean diction" \
  --seed 201

uv run music-engine candidates projects/my-song
uv run music-engine select projects/my-song <repaint-candidate-id>
uv run music-engine undo-selection projects/my-song
```

선택 결과를 WAV로 내보내고 다시 열어 무결성을 확인한다.

```bash
uv run music-engine export projects/my-song --output exports/my-song.wav
uv run music-engine inspect projects/my-song
```

중단/실패 묶음은 `resume`을 명시해야만 완료 artifact를 검증해 재사용한다. 새 `generate`는 같은
입력과 seed라도 새 작업이다.

```bash
uv run music-engine resume projects/my-song
```

## 데이터 경계

- `projects/`, `artifacts/`, `exports/`, `.runtime/`, 모델과 음원은 Git에서 제외된다.
- 원문과 모델 입력용 정규화 가사를 분리한다.
- artifact는 프로젝트 상대 경로·byte 수·SHA-256·실제 sample 메타데이터로 검증한다.
- repaint/export는 과거 파일을 덮지 않고 새 artifact와 revision으로 기록한다.
- 기존 `local-tts-engine`의 환경·모델·개인 음성은 사용하거나 변경하지 않는다.

## 문서

- [구현 계획](PLAN.md)
- [아키텍처와 데이터 계약](docs/ARCHITECTURE.md)
- [품질 검사와 청취 계약](docs/QUALITY.md)
- [모델·라이선스 결정](docs/DECISIONS.md)
- [P0 Apple Silicon 실측](docs/P0-VALIDATION.md)
- [현재 상태와 다음 작업](docs/HANDOFF.md)
